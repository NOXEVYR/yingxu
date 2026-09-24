"""Verified, staged Windows updates. The independent helper never terminates a process."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
import zipfile

MAX_FILES = 20000
MAX_MEMBER_BYTES = 768 * 1024 * 1024
MAX_TOTAL_BYTES = 3 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
ROOT_FILES = {'README.md', 'RUNNING.md', 'LICENSE', 'AGENTS.md', 'API_CONTRACT.md',
              '.gitignore', '.gitattributes', 'server.py', 'macos_app.py', 'launcher.pyw',
              'start.vbs', 'Stop-YingXu.ps1', 'YingXu.exe', 'THIRD_PARTY_NOTICES.md',
              'RELEASE_MANIFEST.json', 'MIGRATION.md'}
CODE_SUFFIXES = {'.py', '.pyw', '.js', '.cjs', '.mjs', '.css', '.html', '.md', '.cs',
                 '.ps1', '.json', '.txt', '.svg', '.ico', '.icns', '.manifest', '.yaml',
                 '.woff', '.woff2', '.ttf', '.jsx'}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def safe_relative(name):
    if not isinstance(name, str) or not name or len(name)>1024 or '\\' in name or ':' in name or '\x00' in name:
        raise ValueError('更新包包含不安全路径')
    parts = name.split('/')
    if any(not p or p in {'.', '..'} or len(p)>255 or p[-1:] in {' ', '.'} or
           any(ord(c) < 32 or c in ':<>"|?*' for c in p) or re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)', p)
           for p in parts):
        raise ValueError('更新包包含不安全路径')
    return PurePosixPath(name)


def program_path(name):
    path = safe_relative(name)
    if name in ROOT_FILES:
        return True
    if path.parts[0] in {'yingxu', 'frontend', 'desktop', 'tests', 'docs'}:
        return len(path.parts)>1 and path.suffix in CODE_SUFFIXES
    if len(path.parts) > 1 and path.parts[0] == 'tools':
        return path.suffix in CODE_SUFFIXES or name == 'tools/canvas-editor/pnpm-lock.yaml'
    if path.parts[0] == 'runtime':
        # Runtime manifests are supplied by the public release, never directory mirrors.
        return len(path.parts) > 1 and (len(path.parts) == 2 or path.parts[1] in {'Lib', 'ffmpeg', 'webview2'})
    return False


def checked_path(root, relative=''):
    if not Path(root).is_absolute() or str(root).startswith(('\\\\', '//')):
        raise ValueError('更新根目录必须是本机绝对路径')
    root = Path(root).absolute()
    path = root / relative
    # Reject links in every existing ancestor, including junctions on Windows.
    for parent in (path, *path.parents):
        if os.path.lexists(parent):
            metadata = parent.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
                raise ValueError('更新路径不能包含链接或联接')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('更新路径越界')
    return path


def write_json(path, data):
    path = Path(path)
    checked_path(path.parent, path.name)
    temp = path.with_name(path.name + '.tmp')
    checked_path(temp.parent, temp.name)
    if temp.exists():
        # An interrupted JSON write is not authoritative; never follow a link.
        fingerprint(temp)
        temp.unlink()
    with temp.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


@contextlib.contextmanager
def installation_lock(path):
    path = Path(path)
    checked_path(path.parent, path.name)
    if path.exists(): fingerprint(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    try:
        if not stream.tell():
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise ValueError('另一个更新安装正在进行') from None
    try:
        yield
    finally:
        stream.close()


class ProcessGuard:
    """Open process handles once; PID reuse cannot change the process being awaited."""
    def __init__(self, pid, expected_image=None):
        if os.name != 'nt' or type(pid) is not int or pid <= 0:
            raise ValueError('自动安装只支持 Windows 原生窗口')
        from ctypes import wintypes
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.handle = self.api.OpenProcess(0x100000 | 0x1000, False, pid)
        if not self.handle:
            raise ValueError('无法确认需要退出的映序进程')
        if expected_image is not None:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if not self.api.QueryFullProcessImageNameW(self.handle, 0, buffer, ctypes.byref(length)) or Path(buffer.value).resolve() != Path(expected_image).resolve():
                self.close()
                raise ValueError('更新请求不是来自此安装的映序窗口')

    def exited(self):
        result = self.api.WaitForSingleObject(self.handle, 0)
        if result not in (0, 258):
            raise OSError('无法查询映序进程状态')
        return result == 0

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def read_json(path, limit=MAX_MANIFEST_BYTES):
    info = fingerprint(path)
    if info is None or info['bytes'] > limit:
        raise ValueError('更新记录过大')
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit or fingerprint(path) != info:
        raise ValueError('更新记录在读取期间变化')
    return json.loads(raw)


def fingerprint(path):
    path = checked_path(Path(path).parent, Path(path).name)
    if not os.path.lexists(path):
        return None
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError('更新文件必须是独立普通文件，不能是硬链接')
    value = digest(path)
    after = path.lstat()
    key = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if key(before) != key(after):
        raise ValueError('文件在校验期间变化')
    return dict(sha256=value, bytes=after.st_size, dev=after.st_dev,
                ino=after.st_ino, mtime_ns=after.st_mtime_ns)


def _manifest(path, version):
    value = read_json(path)
    if (value.get('application') != 'YingXu' or value.get('root') != 'YingXu/' or
            value.get('architecture') != 'Windows x64' or value.get('version') != version):
        raise ValueError('安装清单身份不匹配')
    result = {}
    files = value.get('files')
    if not isinstance(files, list) or not 1 <= len(files) < MAX_FILES:
        raise ValueError('安装清单文件数量无效')
    seen = set()
    for entry in files:
        name = entry.get('path')
        if (not program_path(name) or name == 'RELEASE_MANIFEST.json' or name.casefold() in seen or
                type(entry.get('bytes')) is not int or not 0 <= entry['bytes'] <= MAX_MEMBER_BYTES or
                not re.fullmatch('[a-f0-9]{64}', str(entry.get('sha256', '')))):
            raise ValueError('安装清单路径、大小或摘要无效')
        seen.add(name.casefold()); result[name] = entry
    if not {'YingXu.exe', 'server.py', 'launcher.pyw', 'frontend/index.html'} <= set(result):
        raise ValueError('更新缺少核心程序')
    if sum(e['bytes'] for e in result.values()) > MAX_TOTAL_BYTES:
        raise ValueError('安装清单超过大小限制')
    return result


def validate_plan(data_root, install_root, plan_id):
    if not re.fullmatch('[a-f0-9]{32}', str(plan_id)):
        raise ValueError('更新计划编号无效')
    data_root, install_root = checked_path(data_root), checked_path(install_root)
    if install_root == data_root or install_root.is_relative_to(data_root) or data_root.is_relative_to(install_root):
        raise ValueError('自动更新要求程序与应用数据分开存放')
    folder = checked_path(data_root, 'updates/incremental/' + plan_id)
    plan = read_json(folder / 'plan.json')
    if (plan.get('schema') != 1 or plan.get('id') != plan_id or plan.get('state') != 'ready' or
            Path(plan.get('install_root', '')).absolute() != install_root):
        raise ValueError('更新计划尚未就绪或安装目录不一致')
    for field in ('current_version', 'version'):
        if not re.fullmatch(r'\d+\.\d+\.\d+', str(plan.get(field, ''))):
            raise ValueError('版本号无效')
    old_path, new_path = install_root / 'RELEASE_MANIFEST.json', folder / 'stage/RELEASE_MANIFEST.json'
    if digest(old_path) != plan.get('old_manifest_sha256') or digest(new_path) != plan.get('manifest_sha256'):
        raise ValueError('安装清单已变化')
    old, new = _manifest(old_path, plan['current_version']), _manifest(new_path, plan['version'])
    old_case = {n.casefold():n for n in old}
    if any(n.casefold() in old_case and old_case[n.casefold()] != n for n in new):
        raise ValueError('更新不能仅改变文件路径大小写')
    groups = {}
    for group in ('changes', 'reused', 'removed'):
        entries = plan.get(group)
        if not isinstance(entries, list) or len(entries) > MAX_FILES:
            raise ValueError('计划文件列表无效')
        index = {}
        for entry in entries:
            name = entry.get('path')
            if not program_path(name) or name in index:
                raise ValueError('计划路径无效或重复')
            index[name] = entry
        groups[group] = index
    changes, reused, removed = (groups[g] for g in ('changes', 'reused', 'removed'))
    want_changes = {n for n, e in new.items() if n not in old or e['sha256'] != old[n]['sha256']} | {'RELEASE_MANIFEST.json'}
    if set(changes) != want_changes or set(reused) != set(new) - want_changes or set(removed) != set(old) - set(new):
        raise ValueError('增量计划与新旧官方清单不一致')
    original = {}; stage = {}
    for name in set(old) | set(changes):
        original[name] = fingerprint(checked_path(install_root, name))
        expected = plan['old_manifest_sha256'] if name == 'RELEASE_MANIFEST.json' else old.get(name, {}).get('sha256')
        actual = original[name]['sha256'] if original[name] else None
        if name in removed and removed[name].get('expected_old_sha256') is None:
            expected = None
        if actual != expected:
            raise ValueError('本地程序已修改或缺失，不能自动覆盖：' + name)
    for name, entry in changes.items():
        expected_new = plan['manifest_sha256'] if name == 'RELEASE_MANIFEST.json' else new[name]['sha256']
        expected_old = plan['old_manifest_sha256'] if name == 'RELEASE_MANIFEST.json' else old.get(name, {}).get('sha256')
        stage[name] = fingerprint(checked_path(folder / 'stage', name))
        if (entry.get('sha256') != expected_new or entry.get('expected_old_sha256') != expected_old or
                stage[name] is None or stage[name]['sha256'] != expected_new or
                entry.get('bytes') != stage[name]['bytes'] or
                (name != 'RELEASE_MANIFEST.json' and stage[name]['bytes'] != new[name]['bytes'])):
            raise ValueError('增量暂存与计划不一致：' + name)
    for name, entry in reused.items():
        if entry.get('sha256') != new[name]['sha256'] or entry.get('bytes') != new[name]['bytes']:
            raise ValueError('复用文件记录不一致')
    for name, entry in removed.items():
        if entry.get('expected_old_sha256') not in (old[name]['sha256'], None):
            raise ValueError('过期文件记录不一致')
    actual_stage = set()
    for parent, dirs, files in os.walk(folder / 'stage', followlinks=False):
        for name in dirs: checked_path(folder / 'stage', (Path(parent) / name).relative_to(folder / 'stage'))
        for name in files: actual_stage.add((Path(parent) / name).relative_to(folder / 'stage').as_posix())
    if actual_stage != set(changes):
        raise ValueError('暂存目录含有计划外文件')
    return dict(plan=plan, folder=str(folder), original=original, stage=stage,
                plan_fingerprint=fingerprint(folder / 'plan.json'))


def _same_snapshot(snapshot):
    root = Path(snapshot['plan']['install_root'])
    folder = Path(snapshot['folder'])
    if fingerprint(folder / 'plan.json') != snapshot['plan_fingerprint']:
        raise ValueError('更新计划在准备后发生变化')
    for name, expected in snapshot['original'].items():
        if fingerprint(checked_path(root, name)) != expected:
            raise ValueError('程序在准备后发生变化：' + name)
    for name, expected in snapshot['stage'].items():
        if fingerprint(checked_path(folder / 'stage', name)) != expected:
            raise ValueError('暂存文件在准备后发生变化：' + name)


def _copy(source, target, expected):
    source, target = Path(source), Path(target)
    checked_path(source.parent, source.name); checked_path(target.parent, target.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    created = None
    try:
        with source.open('rb') as src, target.open('xb',buffering=0) as dst:
            created = os.fstat(dst.fileno())
            shutil.copyfileobj(src, dst, 1024 * 1024); dst.flush(); os.fsync(dst.fileno())
        if digest(target) != expected:
            raise ValueError('更新副本摘要校验失败')
    except BaseException:
        # A failed exclusive creation owns this inode, not any subsequent file
        # at the path. Remove a partial copy only when every byte is an exact
        # prefix of the still-verified source; preserve all conflicts.
        if created is not None:
            try:
                info=fingerprint(target);current=target.lstat()
                if ((current.st_dev,current.st_ino)==(created.st_dev,created.st_ino) and
                        current.st_size<=source.stat().st_size and digest(source)==expected):
                    remaining=current.st_size;prefix=hashlib.sha256()
                    with source.open('rb') as stream:
                        while remaining:
                            block=stream.read(min(1024*1024,remaining))
                            if not block: raise ValueError('Source changed during partial-copy cleanup')
                            prefix.update(block);remaining-=len(block)
                    if prefix.hexdigest()==info['sha256'] and fingerprint(target)==info:target.unlink()
            except (OSError,ValueError):pass
        raise


def check_space(snapshot, job):
    plan = snapshot['plan']; old = snapshot['original']
    backup_bytes = sum(old[e['path']]['bytes'] for e in plan['changes'] + plan['removed'] if old[e['path']])
    changed_bytes = sum(e['bytes'] for e in plan['changes'])
    reserve = 32 * 1024 * 1024
    # Conservatively sufficient even when job and installation share a volume.
    required = backup_bytes + changed_bytes + reserve
    if any(shutil.disk_usage(p).free < required for p in (job, plan['install_root'])):
        raise ValueError('更新备份或替换磁盘空间不足')


def rollback(job, replace=os.replace):
    job = Path(job); journal_path = job / 'journal.json'; journal = read_json(journal_path)
    root = checked_path(journal['install_root'])
    errors = []
    for entry in reversed(journal['operations']):
        try:
            name = entry['path']
            if not program_path(name): raise ValueError('恢复路径无效')
            pending = checked_path(root, name + '.yx-update-' + journal['ticket'])
            if pending.exists():
                owned = entry.get('pending_fingerprint')
                actual_pending = fingerprint(pending)
                if owned and actual_pending == owned:
                    pending.unlink()
                else:
                    raise ValueError('安装暂存文件有冲突，保留以供检查：' + name)
            if not entry.get('armed'): continue
            target = checked_path(root, name); current = fingerprint(target)
            old_sha = entry['before']['sha256'] if entry['before'] else None
            actual = current['sha256'] if current else None
            if actual == old_sha: continue
            if actual != entry['after_sha256']:
                raise ValueError('恢复目标已被其他程序修改，保留现状：' + name)
            if entry['before']:
                saved = checked_path(job / 'backup', name)
                if fingerprint(saved)['sha256'] != old_sha: raise ValueError('回退备份损坏')
                pending = checked_path(root, name + '.yx-rollback-' + journal['ticket'])
                if pending.exists():
                    if fingerprint(pending)['sha256'] != old_sha: raise ValueError('回退暂存发生冲突')
                    pending.unlink()
                _copy(saved, pending, old_sha)
                if fingerprint(target) != current: raise ValueError('回退目标在复制期间变化')
                replace(pending, target)
            else:
                if fingerprint(target) != current: raise ValueError('回退目标发生变化')
                target.unlink()
        except Exception as error: errors.append(str(error))
    journal.update(state='rolled_back' if not errors else 'recovery_required', errors=errors)
    write_json(journal_path, journal)
    return not errors


def apply_incremental(snapshot, job, replace=os.replace):
    job = checked_path(job); root = checked_path(snapshot['plan']['install_root']); plan = snapshot['plan']
    _same_snapshot(snapshot); check_space(snapshot, job)
    if (job / 'journal.json').exists(): raise ValueError('已有安装事务，请先恢复')
    entries = sorted(plan['changes'] + plan['removed'], key=lambda e: e['path'] == 'RELEASE_MANIFEST.json')
    journal = dict(schema=1, ticket=job.name, install_root=str(root), state='preparing', operations=[])
    for entry in entries:
        name = entry['path']; before = snapshot['original'][name]
        if before: _copy(checked_path(root, name), checked_path(job / 'backup', name), before['sha256'])
        journal['operations'].append(dict(path=name, before=before, after_sha256=entry.get('sha256'), armed=False))
    _same_snapshot(snapshot)
    write_json(job / 'journal.json', journal)
    try:
        for entry in journal['operations']:
            name = entry['path']; target = checked_path(root, name)
            if fingerprint(target) != entry['before']: raise ValueError('替换前程序已变化')
            pending = None
            if entry['after_sha256']:
                pending = checked_path(root, name + '.yx-update-' + job.name)
                _copy(checked_path(Path(snapshot['folder']) / 'stage', name), pending, entry['after_sha256'])
                entry['pending_fingerprint'] = fingerprint(pending)
                write_json(job / 'journal.json', journal)
            if fingerprint(target) != entry['before']: raise ValueError('替换期间程序已变化')
            entry['armed'] = True; journal['state'] = 'applying'; write_json(job / 'journal.json', journal)
            if pending is not None: replace(pending, target)
            elif entry['before'] is not None: target.unlink()
            entry['done'] = True; write_json(job / 'journal.json', journal)
        # Reused files must still be the same files, including their timestamps.
        # A concurrent edit is preserved while only our replacements roll back.
        for reused in plan['reused']:
            name = reused['path']
            if fingerprint(checked_path(root,name)) != snapshot['original'][name]:
                raise ValueError('安装期间复用文件发生变化：' + name)
        for changed in plan['changes']:
            info = fingerprint(checked_path(root,changed['path']))
            if info is None or info['sha256'] != changed['sha256'] or info['bytes'] != changed['bytes']:
                raise ValueError('安装后的文件校验失败：' + changed['path'])
        for removed in plan['removed']:
            if fingerprint(checked_path(root,removed['path'])) is not None:
                raise ValueError('过期文件在安装期间重新出现')
        journal['state'] = 'installed'; write_json(job / 'journal.json', journal)
        return len(entries)
    except Exception:
        rollback(job)
        raise


def copy_helper_runtime(install_root, target):
    runtime = checked_path(install_root, 'runtime'); target = checked_path(target)
    records = {e['path']: e for e in read_json(runtime / 'RUNTIME_MANIFEST.json')['files']}
    names = [n for n in records if '/' not in n and Path(n).suffix.lower() in {'.exe','.dll','.pyd','.zip','._pth'}]
    if not names or sum(records[n]['bytes'] for n in names) > 48 * 1024 * 1024:
        raise ValueError('内置 Python 最小运行时无效或过大')
    target.mkdir()
    for name in names:
        source = checked_path(runtime, name); info = fingerprint(source)
        if info['sha256'] != records[name]['sha256'] or info['bytes'] != records[name]['bytes']:
            raise ValueError('内置 Python 校验失败')
        _copy(source, target / name, info['sha256'])
    pths = list(target.glob('python*._pth'))
    if len(pths) != 1 or not (target / 'python.exe').is_file(): raise ValueError('缺少独立 Python')
    stdlib = pths[0].stem + '.zip'
    if not (target / stdlib).is_file(): raise ValueError('缺少独立 Python 标准库')
    pths[0].write_text(stdlib + '\n.\n', encoding='ascii')
    return target / 'python.exe'


def _install_base(data_root):
    base = checked_path(data_root, 'updates/incremental-install'); base.mkdir(parents=True, exist_ok=True)
    return base


def _job(data_root, install_root, ticket):
    if not re.fullmatch('[a-f0-9]{32}', str(ticket)): raise ValueError('安装票据无效')
    job = checked_path(_install_base(data_root), ticket); config = read_json(job / 'job.json')
    if config.get('ticket') != ticket or Path(config.get('install_root','')).absolute() != checked_path(install_root):
        raise ValueError('安装票据与当前程序不符')
    return job, config


def prepare_install(data_root, install_root, plan_id, native_pid):
    if os.name != 'nt': raise ValueError('增量自动安装仅支持 Windows 完整版')
    root = checked_path(install_root)
    if Path(sys.executable).resolve() not in {root/'runtime/python.exe',root/'runtime/pythonw.exe'}:
        raise ValueError('请使用 Windows 完整包自带 Python 启动映序后再安装增量更新')
    base = _install_base(data_root)
    guard = ProcessGuard(native_pid, Path(install_root) / 'YingXu.exe')
    try:
        with installation_lock(base / 'prepare.lock'):
            with installation_lock(base / 'install.lock'):
                for prior in base.glob('*/journal.json'):
                    if read_json(prior).get('state') in {'applying','preparing','recovery_required'}:
                        raise ValueError('上次安装未完成，请退出并重新打开映序执行恢复')
                snapshot = validate_plan(data_root, install_root, plan_id)
                if set(installation_processes(install_root)) - {native_pid,os.getpid()}:
                    raise ValueError('请先退出同一安装的其他映序窗口或后台')
                ticket = uuid.uuid4().hex; job = checked_path(base, ticket); job.mkdir()
                check_space(snapshot, job)
                interpreter = copy_helper_runtime(install_root, job / 'helper')
                script = interpreter.parent / 'incremental_install.py'
                _copy(Path(__file__).resolve(), script, digest(Path(__file__)))
                environment = {k:v for k,v in os.environ.items() if k.upper() in {'YINGXU_PROJECTS_DIR'}}
                environment['YINGXU_DATA_DIR'] = str(checked_path(data_root))
                config = dict(ticket=ticket, install_root=str(checked_path(install_root)), native_pid=native_pid,
                              backend_pid=os.getpid(), backend_image=str(Path(sys.executable).resolve()),
                              snapshot=snapshot, environment=environment)
                write_json(job / 'job.json', config)
            process = subprocess.Popen([str(interpreter), '-I', '-B', str(script), '--worker', str(job)],
                cwd=job, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if (job / 'ready.json').exists() and process.poll() is None:
                    ready = read_json(job / 'ready.json')
                    if ready.get('ticket') != ticket or ready.get('helper_pid') != process.pid: raise ValueError('助手身份不匹配')
                    if guard.exited():
                        write_json(job / 'cancel.json', dict(ticket=ticket,cancelled=True))
                        raise ValueError('原映序窗口已退出，安装准备已取消')
                    return dict(prepared=True, ticket=ticket, helper_pid=process.pid, native_pid=native_pid, backend_pid=os.getpid())
                if process.poll() is not None: break
                time.sleep(.1)
            write_json(job / 'cancel.json', dict(ticket=ticket, cancelled=True))
            raise ValueError('更新助手未就绪，窗口保持打开')
    finally: guard.close()


def commit_install(data_root, install_root, ticket):
    job, config = _job(data_root, install_root, ticket)
    with installation_lock(job / 'decision.lock'):
        if (job / 'commit.json').exists(): return read_json(job / 'commit.json')
        if (job / 'cancel.json').exists() or (job / 'outcome.json').exists(): raise ValueError('安装准备已经取消或结束')
        ready = read_json(job / 'ready.json')
        guard = ProcessGuard(ready['helper_pid'], job / 'helper/python.exe')
        try:
            if guard.exited(): raise ValueError('安装助手已退出')
            _same_snapshot(config['snapshot'])
            result = dict(committed=True, ticket=ticket, native_pid=config['native_pid'], backend_pid=config['backend_pid'], helper_pid=ready['helper_pid'])
            # Durable receipt also allows native to resolve a lost HTTP response
            # after the backend has already completed its graceful shutdown.
            write_json(job / 'commit.json', result)
            return result
        finally: guard.close()


def cancel_install(data_root, install_root, ticket):
    job, config = _job(data_root, install_root, ticket)
    with installation_lock(job / 'decision.lock'):
        if (job / 'commit.json').exists(): raise ValueError('安装已提交，请等待助手完成')
        write_json(job / 'cancel.json', dict(ticket=ticket, cancelled=True))
    return dict(cancelled=True, ticket=ticket)


def wait_for_commit_and_exit(job, guards, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (job / 'cancel.json').exists(): return False
        if (job / 'commit.json').exists() and all(g.exited() for g in guards): return True
        time.sleep(.1)
    return False


def run_worker(job):
    job = checked_path(job); config = read_json(job / 'job.json'); guards=[]
    outcome = dict(ticket=config['ticket'], state='failed', version=config['snapshot']['plan']['version'])
    try:
        with installation_lock(job.parent / 'install.lock'):
            guards = [ProcessGuard(config['native_pid'], Path(config['install_root']) / 'YingXu.exe'),
                      ProcessGuard(config['backend_pid'], config['backend_image'])]
            ready = dict(ticket=config['ticket'], plan_id=config['snapshot']['plan']['id'], helper_pid=os.getpid(), native_pid=config['native_pid'], backend_pid=config['backend_pid'])
            write_json(job / 'ready.json', ready)
            write_json(job.parent / 'active.json', ready)
            if not wait_for_commit_and_exit(job, guards):
                outcome.update(state='cancelled', message='窗口或后台未退出，程序文件未修改'); return
            if installation_processes(config['install_root']):
                raise ValueError('另一个映序进程已启动，程序文件未修改')
            if (job / 'journal.json').exists():
                journal = read_json(job / 'journal.json')
                if journal['state'] != 'installed':
                    outcome['state'] = 'rolled_back' if rollback(job) else 'recovery_required'
                    return
            else:
                outcome['changed_files'] = apply_incremental(config['snapshot'], job)
            outcome['state'] = 'installed'; write_json(job / 'outcome.json', outcome)
    except Exception as error:
        outcome.update(message=str(error), error_type=type(error).__name__)
        if (job / 'journal.json').exists(): outcome['state'] = read_json(job / 'journal.json')['state']
    finally:
        for guard in guards: guard.close()
        write_json(job / 'outcome.json', outcome); write_json(job.parent / 'last-install.json', outcome)
    if outcome['state'] in {'installed','rolled_back'}:
        try:
            restart_application(config);outcome['restarted']=True
        except Exception as error:
            outcome.update(restarted=False,restart_error=type(error).__name__,message='更新或恢复已完成，但自动打开失败；请手动打开映序。')
        write_json(job / 'outcome.json', outcome);write_json(job.parent / 'last-install.json', outcome)


def restart_application(config):
    environment = {k:v for k,v in os.environ.items() if not k.upper().startswith(('YINGXU_','PYTHON'))}
    environment.update(config['environment'])
    subprocess.Popen([str(Path(config['install_root']) / 'YingXu.exe')], cwd=config['install_root'], env=environment,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)


def installation_processes(root):
    """Enumerate only process image paths for this installation, no commands."""
    from ctypes import wintypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    class Entry(ctypes.Structure):
        _fields_=[('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),('th32ProcessID',wintypes.DWORD),
                  ('th32DefaultHeapID',ctypes.c_size_t),('th32ModuleID',wintypes.DWORD),('cntThreads',wintypes.DWORD),
                  ('th32ParentProcessID',wintypes.DWORD),('pcPriClassBase',wintypes.LONG),('dwFlags',wintypes.DWORD),
                  ('szExeFile',wintypes.WCHAR*260)]
    api.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD];api.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    api.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    api.Process32NextW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=api.CreateToolhelp32Snapshot(2,0)
    if handle==ctypes.c_void_p(-1).value:raise OSError('无法检查映序进程')
    result=[]
    try:
        entry=Entry();entry.dwSize=ctypes.sizeof(Entry);exists=api.Process32FirstW(handle,ctypes.byref(entry))
        while exists:
            if entry.szExeFile.lower() in {'yingxu.exe','python.exe','pythonw.exe'}:
                guard=None
                try:
                    guard=ProcessGuard(int(entry.th32ProcessID))
                    buffer=ctypes.create_unicode_buffer(32768);size=wintypes.DWORD(len(buffer))
                    if not guard.api.QueryFullProcessImageNameW(guard.handle,0,buffer,ctypes.byref(size)):
                        raise ValueError('无法确认映序相关进程路径')
                    if Path(buffer.value).resolve().is_relative_to(Path(root).resolve()):result.append(int(entry.th32ProcessID))
                except ValueError:
                    if entry.szExeFile.lower()=='yingxu.exe':raise
                finally:
                    if guard:guard.close()
            exists=api.Process32NextW(handle,ctypes.byref(entry))
    finally:api.CloseHandle(handle)
    return result


def run_recovery(job, native_pid):
    """Explicit startup recovery waits for the asking native process to exit."""
    job=checked_path(job);config=read_json(job/'job.json');root=checked_path(config['install_root'])
    journal=read_json(job/'journal.json')
    if journal.get('install_root')!=str(root) or journal.get('ticket')!=job.name:
        raise ValueError('恢复日志身份不符')
    with installation_lock(job.parent/'install.lock'):
        guard=ProcessGuard(native_pid,root/'YingXu.exe')
        try:
            if set(installation_processes(root))!={native_pid}:raise ValueError('请先退出本安装的其他窗口与后台，再恢复')
            write_json(job/'recovery-ready.json',dict(native_pid=native_pid,helper_pid=os.getpid(),ticket=job.name))
            deadline=time.monotonic()+30
            while not guard.exited():
                if time.monotonic()>deadline:raise ValueError('恢复启动窗口没有退出')
                time.sleep(.1)
            if installation_processes(root):raise ValueError('另一个映序进程已启动，恢复未执行')
            ok=rollback(job)
            outcome=dict(ticket=job.name,state='rolled_back' if ok else 'recovery_required',recovery=True)
            write_json(job/'outcome.json',outcome);write_json(job.parent/'last-install.json',outcome)
        finally:guard.close()
    if ok:
        try:
            restart_application(config);outcome['restarted']=True
        except Exception as error:
            outcome.update(restarted=False,restart_error=type(error).__name__,message='恢复已完成，但自动打开失败；请手动打开映序。')
        write_json(job/'outcome.json',outcome);write_json(job.parent/'last-install.json',outcome)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--worker', type=Path);mode.add_argument('--recover', type=Path)
    parser.add_argument('--native-pid',type=int)
    args=parser.parse_args()
    if args.worker:run_worker(args.worker)
    else:run_recovery(args.recover,args.native_pid)


