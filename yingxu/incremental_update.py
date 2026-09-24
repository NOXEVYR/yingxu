"""On-demand, manifest-bound Windows updates using changed ZIP members only."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import threading

from .range_zip import (Network, RangeZip, UpdateError, REPOSITORY, MAX_ARCHIVE, MAX_MANIFEST,
                        MAX_EXPANDED, validate_manifest, version_key, safe_relative, _check_tree)
from .store import UserError

API = f'https://api.github.com/repos/{REPOSITORY}/releases'
MAX_METADATA = 8 * 1024**2


def checked_path(root, relative=''):
    root = Path(os.path.abspath(root))
    if relative:
        safe_relative(relative)
    path = root / relative
    for current in (path, *path.parents):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise UpdateError('更新路径包含链接或目录联接，请使用真实安装目录。')
    if not path.resolve().is_relative_to(root.resolve()):
        raise UpdateError('更新路径越界。')
    return path


def file_hash(path, active=None):
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise UpdateError('更新文件必须是独立普通文件。')
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            if active:
                active()
            value.update(block)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise UpdateError('本地文件在更新检查期间发生变化，请重新检查。')
    return value.hexdigest()


def read_file(path, limit):
    if not path.is_file() or path.stat().st_size > limit or path.stat().st_nlink != 1:
        raise UpdateError('本地更新清单缺失、过大或不是独立文件，请使用完整包。')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise UpdateError('本地清单超过大小限制。')
    return raw


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def write_file(root, name, content):
    target = checked_path(root, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    checked_path(root, name)
    if target.exists() and (not target.is_file() or target.stat().st_nlink != 1):
        raise UpdateError('暂存目录存在非普通文件。')
    fd, temporary = tempfile.mkstemp(prefix='.write-', suffix='.part', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        checked_path(root, name)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def select_release(releases):
    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
            continue
        tag = release.get('tag_name', '')
        if not isinstance(tag, str) or not tag.startswith('yingxu-v'):
            continue
        version = tag[8:]
        try:
            key = version_key(version)
        except UpdateError:
            continue
        page = f'https://github.com/{REPOSITORY}/releases/tag/{tag}'
        if release.get('html_url') != page:
            continue
        assets = release.get('assets')
        if not isinstance(assets, list) or len(assets) > 100:
            continue
        names = (f'YingXu-v{version}-Windows-x64.zip', f'YingXu-v{version}-manifest.json')
        selected = []
        for index, name in enumerate(names):
            matches = [a for a in assets if isinstance(a, dict) and a.get('name') == name]
            if len(matches) != 1:
                break
            asset = matches[0]
            url = f'https://github.com/{REPOSITORY}/releases/download/{tag}/{name}'
            if (type(asset.get('id')) is not int or asset['id'] <= 0 or asset.get('state') != 'uploaded' or
                    type(asset.get('size')) is not int or not 0 < asset['size'] <= (MAX_ARCHIVE if index == 0 else 128 * 1024) or
                    not isinstance(asset.get('digest'), str) or not re.fullmatch('sha256:[a-fA-F0-9]{64}', asset['digest']) or
                    asset.get('browser_download_url') != url):
                break
            selected.append(dict(id=asset['id'], name=name, url=url, size=asset['size'], sha256=asset['digest'][7:].lower()))
        if len(selected) == 2:
            candidates.append(dict(version=version, key=key, release_url=page, asset=selected[0], external_manifest=selected[1]))
    return max(candidates, key=lambda item: item['key'], default=None)


class UpdateManager:
    def __init__(self, data_root, install_root, current_version):
        version_key(current_version)
        self.data_root = Path(os.path.abspath(data_root))
        self.install_root = Path(os.path.abspath(install_root))
        self.current_version = current_version
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker = None
        self._context = None
        self._status = dict(state='idle', plan_id='', current_version=current_version, latest_version='',
                            download_bytes=0, total_download_bytes=0, changed_files=0, reused_files=0,
                            removed_files=0, message='尚未检查增量更新。', release_url='', can_install=sys.platform == 'win32')

    def status(self):
        with self._lock:
            return dict(self._status)

    def close(self):
        self._stop.set()

    def _update(self, **fields):
        with self._lock:
            self._status.update(fields)

    def _busy(self):
        return self._worker is not None and self._worker.is_alive()

    def plan(self):
        with self._lock:
            if self._stop.is_set():
                raise UserError('应用正在退出。', 409)
            if self._busy():
                return self.status()
            self._context = None
            self._status.update(state='planning', plan_id='', download_bytes=0, total_download_bytes=0,
                                changed_files=0, reused_files=0, removed_files=0, message='正在核对发布清单与本地程序文件。')
            self._worker = threading.Thread(target=self._plan, name='yingxu-incremental-plan', daemon=True)
            self._worker.start()
            return self.status()

    def _network(self, seconds=1800, max_bytes=MAX_ARCHIVE + MAX_METADATA):
        return Network(self._stop.is_set, seconds=seconds, max_bytes=max_bytes)

    def _releases(self, network):
        releases = []
        for page in range(1, 4):
            raw = network.get(f'{API}?per_page=100&page={page}', 2 * 1024**2)
            entries = json.loads(raw)
            if not isinstance(entries, list) or len(entries) > 100:
                raise UpdateError('GitHub 发布列表格式无效。')
            releases.extend(entries)
            if len(entries) < 100:
                return releases
        raise UpdateError('发布列表超过检查范围，请使用 GitHub 发布页。')

    def _plan(self):
        try:
            install = checked_path(self.install_root).resolve()
            data_root = checked_path(self.data_root).resolve()
            if install.is_relative_to(data_root) or data_root.is_relative_to(install):
                raise UpdateError('增量更新要求程序目录与应用数据目录分开存放。')
            network = self._network(seconds=600, max_bytes=32 * 1024**2)
            # Pick the numeric newest Windows release before validating its
            # download contract; never silently fall back to an older release.
            visible = []
            for release in self._releases(network):
                if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
                    continue
                tag = release.get('tag_name', '')
                if not isinstance(tag, str) or not tag.startswith('yingxu-v'):
                    continue
                try:
                    key = version_key(tag[8:])
                except UpdateError:
                    continue
                page = f'https://github.com/{REPOSITORY}/releases/tag/{tag}'
                if release.get('html_url') != page or not isinstance(release.get('assets'), list):
                    continue
                name = f'YingXu-v{tag[8:]}-Windows-x64.zip'
                if any(isinstance(asset, dict) and asset.get('name') == name for asset in release['assets']):
                    visible.append((key, tag[8:], page, release))
            if not visible:
                raise UpdateError('没有找到具备完整性摘要的 Windows 正式发布，请查看 GitHub 发布页。')
            key, version, page, release = max(visible, key=lambda item: item[0])
            self._update(latest_version=version, release_url=page)
            if key <= version_key(self.current_version):
                self._update(state='current', message='当前已经是最新正式版。')
                return
            candidate = select_release([release])
            if candidate is None:
                raise UpdateError('最新发布缺少可验证的资产摘要，请使用发布页的完整包。')
            external = candidate['external_manifest']
            raw_external = network.get(external['url'], external['size'])
            if len(raw_external) != external['size'] or hashlib.sha256(raw_external).hexdigest() != external['sha256']:
                raise UpdateError('发布清单与 GitHub 资产摘要不一致。')
            metadata = json.loads(raw_external)
            asset = candidate['asset']
            if (not isinstance(metadata, dict) or metadata.get('file') != asset['name'] or metadata.get('version') != candidate['version'] or
                    metadata.get('bytes') != asset['size'] or metadata.get('sha256') != asset['sha256'] or metadata.get('root') != 'YingXu/' or
                    not isinstance(metadata.get('release_manifest_sha256'), str) or
                    not re.fullmatch('[a-f0-9]{64}', metadata['release_manifest_sha256'])):
                raise UpdateError('此发布未提供可验证的增量清单，请使用发布页的完整包。')
            archive = RangeZip(asset['url'], asset['size'], network)
            member = archive.members.get('YingXu/RELEASE_MANIFEST.json')
            if member is None or member.size > MAX_MANIFEST:
                raise UpdateError('发布包缺少有界文件清单。')
            output = io.BytesIO()
            archive.extract(member, output, metadata['release_manifest_sha256'])
            manifest_raw = output.getvalue()
            manifest, new = validate_manifest(manifest_raw, candidate['version'])
            if metadata.get('source_commit', '') != manifest.get('source_commit', ''):
                raise UpdateError('外部与内部发布清单来源不一致。')
            expected = {'YingXu/' + name for name in new} | {'YingXu/RELEASE_MANIFEST.json'}
            if set(archive.members) != expected or any(archive.members['YingXu/' + name].size != row['bytes'] for name, row in new.items()):
                raise UpdateError('发布包目录与文件清单不一致。')
            old_raw = read_file(checked_path(self.install_root, 'RELEASE_MANIFEST.json'), MAX_MANIFEST)
            _, old = validate_manifest(old_raw, self.current_version)
            _check_tree(set(old) | set(new))
            changes, reused, removed = [], [], []
            for name, row in new.items():
                network.active()
                path = checked_path(self.install_root, name)
                actual = file_hash(path, network.active) if path.exists() else None
                previous = old.get(name)
                if previous is not None and actual is None:
                    raise UpdateError('已登记的本地程序文件缺失，请修复完整安装后重试。')
                if actual is not None and previous is None:
                    raise UpdateError('新程序文件与未登记的本地文件冲突，已停止更新。')
                if actual is not None and actual != previous['sha256']:
                    raise UpdateError('已登记的程序文件存在本地修改，已停止更新以保留改动。')
                if previous is not None and path.stat().st_size != previous['bytes']:
                    raise UpdateError('本地程序文件大小与已安装清单不一致。')
                if actual == row['sha256']:
                    if path.stat().st_size != row['bytes']:
                        raise UpdateError('相同文件摘要对应的发布大小不一致。')
                    reused.append(dict(row))
                else:
                    changes.append(dict(row, expected_old_sha256=actual))
                    archive.prepare(archive.members['YingXu/' + name])
            for name, row in old.items():
                if name in new:
                    continue
                path = checked_path(self.install_root, name)
                actual = file_hash(path, network.active) if path.exists() else None
                if actual is not None and actual != row['sha256']:
                    raise UpdateError('待移除的旧程序文件有本地修改，已停止更新。')
                removed.append(dict(path=name, expected_old_sha256=actual))
            old_sha = hashlib.sha256(old_raw).hexdigest()
            manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
            changes.append(dict(path='RELEASE_MANIFEST.json', expected_old_sha256=old_sha,
                                sha256=manifest_sha, bytes=len(manifest_raw)))
            payload = dict(schema=1, install_root=str(self.install_root.resolve()), current_version=self.current_version,
                           version=candidate['version'], asset=asset, external_manifest=external,
                           manifest_sha256=manifest_sha, old_manifest_sha256=old_sha,
                           changes=changes, reused=reused, removed=removed)
            plan_id = hashlib.sha256(_json_bytes(payload)).hexdigest()[:32]
            folder = self._folder(plan_id)
            payload.update(id=plan_id, state='planned')
            write_file(folder, 'plan.json', _json_bytes(payload))
            context = dict(folder=folder, plan=payload, archive=archive, manifest=manifest_raw)
            total = sum(archive.members['YingXu/' + row['path']].compressed for row in changes if row['path'] != 'RELEASE_MANIFEST.json')
            with self._lock:
                self._context = context
                self._status.update(state='planned', plan_id=plan_id, total_download_bytes=total,
                                    changed_files=len(changes), reused_files=len(reused), removed_files=len(removed),
                                    message='已生成增量计划；确认下载后仅传输变化文件。')
        except Exception as error:
            self._fail(error)

    def _folder(self, plan_id):
        if not re.fullmatch('[a-f0-9]{32}', plan_id):
            raise UpdateError('更新计划标识无效。')
        parent = checked_path(self.data_root, 'updates/incremental')
        parent.mkdir(parents=True, exist_ok=True)
        if not (parent / plan_id).exists() and len(list(parent.iterdir())) >= 16:
            raise UpdateError('增量更新缓存已满，请先整理已完成的更新记录。')
        folder = checked_path(parent, plan_id)
        folder.mkdir(exist_ok=True)
        return folder

    def download(self, plan_id):
        with self._lock:
            if self._stop.is_set():
                raise UserError('应用正在退出。', 409)
            if not self._context or plan_id != self._context['plan']['id']:
                raise UserError('更新计划无效或已过期，请重新检查。', 409)
            if self._busy() or self._status['state'] == 'ready':
                return self.status()
            self._status.update(state='downloading', download_bytes=0, message='正在下载并校验变化文件。')
            self._worker = threading.Thread(target=self._download, name='yingxu-incremental-download', daemon=True)
            self._worker.start()
            return self.status()

    def ready_plan(self, plan_id):
        with self._lock:
            if self._status['state'] != 'ready' or not self._context or self._context['plan']['id'] != plan_id:
                raise UserError('增量更新尚未下载完成。', 409)
            try:
                path = checked_path(self._context['folder'], 'plan.json')
                same = read_file(path, MAX_MANIFEST) == _json_bytes(self._context['plan'])
            except (OSError, ValueError):
                same = False
            if not same:
                raise UserError('更新计划已发生变化，请重新检查。', 409)
            return path

    def _progress(self, amount):
        with self._lock:
            self._status['download_bytes'] += amount

    def _download(self):
        context = self._context
        payload, folder, archive = context['plan'], context['folder'], context['archive']
        temporary = None
        try:
            # Revalidation before network activity catches edits made after preview.
            network = self._network()
            for row in payload['changes'] + payload['removed']:
                path = checked_path(self.install_root, row['path'])
                actual = file_hash(path, network.active) if path.exists() else None
                if actual != row['expected_old_sha256']:
                    raise UpdateError('本地程序文件已变化，请重新生成更新计划。')
            for row in payload['reused']:
                path = checked_path(self.install_root, row['path'])
                if not path.is_file() or file_hash(path, network.active) != row['sha256']:
                    raise UpdateError('待复用的程序文件已变化，请重新生成更新计划。')
            network.etags.update(archive.network.etags)
            archive.network = network
            stage = checked_path(folder, 'stage')
            stage.mkdir(exist_ok=True)
            self._clean_partial_files(stage, network)
            pending = []
            for row in payload['changes']:
                if row['path'] == 'RELEASE_MANIFEST.json':
                    continue
                target = checked_path(stage, row['path'])
                if target.is_file() and target.stat().st_size == row['bytes'] and file_hash(target, network.active) == row['sha256']:
                    continue
                pending.append(row)
            total = sum(archive.members['YingXu/' + row['path']].compressed for row in pending)
            self._update(total_download_bytes=total)
            required = sum(row['bytes'] for row in pending) + len(context['manifest']) + 64 * 1024**2
            if shutil.disk_usage(folder).free < required:
                raise UpdateError('磁盘空间不足，无法暂存增量更新。')
            # Count cache storage with bounds; never delete unknown user files.
            count = used = 0
            for root, dirs, files in os.walk(folder.parent, followlinks=False):
                network.active()
                for name in dirs + files:
                    path = checked_path(Path(root), name)
                    count += 1
                    if path.is_file():
                        used += path.stat().st_size
                    if count > 100000 or used + required > 4 * 1024**3:
                        raise UpdateError('增量更新缓存达到容量限制，请整理已完成的更新记录。')
            payload['state'] = 'downloading'
            write_file(folder, 'plan.json', _json_bytes(payload))
            for row in pending:
                network.active()
                target = checked_path(stage, row['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                checked_path(stage, row['path'])
                if target.exists() and (not target.is_file() or target.stat().st_nlink != 1):
                    raise UpdateError('暂存文件不是独立普通文件。')
                fd, name = tempfile.mkstemp(prefix='.member-', suffix='.part', dir=target.parent)
                temporary = Path(name)
                with os.fdopen(fd, 'wb') as stream:
                    archive.extract(archive.members['YingXu/' + row['path']], stream, row['sha256'], self._progress)
                    stream.flush()
                    os.fsync(stream.fileno())
                checked_path(stage, row['path'])
                os.replace(temporary, target)
                temporary = None
            write_file(stage, 'RELEASE_MANIFEST.json', context['manifest'])
            actual_stage = set()
            for root, dirs, files in os.walk(stage, followlinks=False):
                for name in dirs + files:
                    checked_path(Path(root), name)
                actual_stage.update((Path(root) / name).relative_to(stage).as_posix() for name in files)
            if actual_stage != {row['path'] for row in payload['changes']}:
                raise UpdateError('暂存目录包含计划外文件，已保留这些文件并停止安装。')
            payload['state'] = 'ready'
            write_file(folder, 'plan.json', _json_bytes(payload))
            self._update(state='ready', message='变化文件已下载并校验，可以保存工作并安装。')
        except Exception as error:
            payload['state'] = 'planned'
            try:
                write_file(folder, 'plan.json', _json_bytes(payload))
            except (OSError, ValueError):
                pass
            self._fail(error)
        finally:
            if temporary is not None:
                try:
                    checked_path(temporary.parent, temporary.name).unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass

    def _fail(self, error):
        self._update(state='error', message=str(error) if isinstance(error, UpdateError) else
                     '未能完成增量更新，请检查网络后重试；也可使用发布页的完整包。')

    @staticmethod
    def _clean_partial_files(stage, network):
        count = 0
        for root, dirs, files in os.walk(stage, followlinks=False):
            network.active()
            for name in dirs + files:
                checked_path(Path(root), name)
                count += 1
                if count > 50000:
                    raise UpdateError('暂存目录条目过多，请整理更新缓存。')
            for name in files:
                if re.fullmatch(r'\.(?:member|write)-[a-zA-Z0-9_-]+\.part', name):
                    path = checked_path(Path(root), name)
                    if path.stat().st_nlink != 1:
                        raise UpdateError('不完整暂存文件不是独立文件。')
                    path.unlink()
