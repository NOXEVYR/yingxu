"""Explicit cleanup of verified, successfully installed download caches only."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .incremental_update import checked_path, read_file, _json_bytes
from .incremental_install import fingerprint, installation_lock
from .range_zip import MAX_MANIFEST, validate_manifest, version_key
from .release_identity import compare_builds
from .store import UserError

MAX_ENTRIES = 256
MAX_SCAN_FILES = 20000


class UpdateCache:
    def __init__(self, data_root, install_root, current_version):
        self.data_root = checked_path(data_root)
        self.install_root = checked_path(install_root)
        self.current_version = current_version
        self._preview = None

    def _json(self, path, limit=MAX_MANIFEST):
        value = json.loads(read_file(checked_path(path.parent, path.name), limit))
        if not isinstance(value, dict):
            raise ValueError('更新记录格式无效。')
        return value

    def _installed_plans(self):
        base = checked_path(self.data_root, 'updates/incremental-install')
        if not base.exists():
            return {}
        entries = list(base.iterdir())
        if len(entries) > MAX_ENTRIES:
            raise UserError('安装记录超过整理范围，请保留记录并人工检查。', 409)
        installed = {}
        for entry in entries:
            if not re.fullmatch('[a-f0-9]{32}', entry.name):
                continue
            try:
                entry = checked_path(base, entry.name)
                config = self._json(entry / 'job.json')
                outcome = self._json(entry / 'outcome.json', 16384)
                journal = self._json(entry / 'journal.json')
                if journal.get('state') in ('preparing', 'applying', 'recovery_required'):
                    raise UserError('上次安装尚未完成，请先执行启动时的恢复，再整理缓存。', 409)
                plan = config['snapshot']['plan']
                if (config.get('ticket') != entry.name or outcome.get('ticket') != entry.name or
                        journal.get('ticket') != entry.name or outcome.get('state') != 'installed' or
                        journal.get('state') != 'installed' or plan.get('state') != 'ready' or
                        outcome.get('version') != plan.get('version') or
                        config.get('install_root') != str(self.install_root) or
                        journal.get('install_root') != str(self.install_root)):
                    continue
                installed[plan['id']] = plan
            except (ValueError, OSError, KeyError, TypeError, AttributeError):
                continue
        return installed

    def _candidate(self, folder, installed, protected):
        if folder.name in protected or not re.fullmatch('[a-f0-9]{32}', folder.name):
            return None
        folder = checked_path(folder.parent, folder.name)
        plan = self._json(folder / 'plan.json')
        if (plan.get('schema') != 1 or plan.get('id') != folder.name or plan.get('state') != 'ready' or
                plan.get('install_root') != str(self.install_root) or installed.get(folder.name) != plan):
            return None
        identity = {key: value for key, value in plan.items() if key not in ('id', 'state')}
        if hashlib.sha256(_json_bytes(identity)).hexdigest()[:32] != folder.name:
            return None
        target = version_key(plan['version'])
        current = version_key(self.current_version)
        current_manifest = self._json(self.install_root / 'RELEASE_MANIFEST.json')
        if current_manifest.get('version') != self.current_version or target > current:
            return None
        if target == current and compare_builds(current_manifest.get('build_revision', ''), plan.get('latest_build', '')) != 'current':
            return None
        raw = read_file(checked_path(folder, 'stage/RELEASE_MANIFEST.json'), MAX_MANIFEST)
        manifest, records = validate_manifest(raw, plan['version'])
        if (hashlib.sha256(raw).hexdigest() != plan.get('manifest_sha256') or
                manifest.get('build_revision', '') != plan.get('latest_build', '')):
            return None
        expected = {'plan.json': hashlib.sha256(_json_bytes(plan)).hexdigest()}
        for row in plan['changes']:
            name = row['path']
            if name == 'RELEASE_MANIFEST.json':
                sha, size = plan['manifest_sha256'], len(raw)
            else:
                sha, size = records[name]['sha256'], records[name]['bytes']
            if row.get('sha256') != sha or row.get('bytes') != size or 'stage/' + name in expected:
                return None
            expected['stage/' + name] = sha
        snapshots, directories = {}, []
        count = 0
        for parent, dirs, files in os.walk(folder, followlinks=False):
            for name in dirs + files:
                checked_path(Path(parent), name)
                count += 1
                if count > MAX_SCAN_FILES:
                    raise ValueError('更新缓存超过检查范围。')
            directories.extend(str(Path(parent) / name) for name in dirs)
            for name in files:
                path = Path(parent) / name
                relative = path.relative_to(folder).as_posix()
                info = fingerprint(path)
                if relative not in expected or info is None or info['sha256'] != expected[relative]:
                    return None
                snapshots[relative] = info
        expected_dirs = {str(parent) for relative in expected for parent in
                         (folder / relative).parents if parent != folder and parent.is_relative_to(folder)}
        if set(snapshots) != set(expected) or set(directories) != expected_dirs:
            return None
        return dict(id=folder.name, files=snapshots, directories=sorted(directories),
                    bytes=sum(row['bytes'] for row in snapshots.values()))

    def _scan(self, protected):
        base = checked_path(self.data_root, 'updates/incremental')
        if not base.exists():
            return [], 0
        entries = list(base.iterdir())
        if len(entries) > MAX_ENTRIES:
            raise UserError('更新缓存超过整理范围，请保留记录并人工检查。', 409)
        installed = self._installed_plans()
        candidates = []
        for folder in entries:
            try:
                candidate = self._candidate(folder, installed, protected)
                if candidate:
                    candidates.append(candidate)
            except (ValueError, OSError, KeyError, TypeError, AttributeError):
                continue
        return sorted(candidates, key=lambda row: row['id']), len(entries)

    def preview(self, protected):
        rows, total = self._scan(protected)
        value = dict(id=uuid.uuid4().hex, expires=time.monotonic() + 300, rows=rows)
        self._preview = value
        return dict(preview_id=value['id'], cleanable_plans=len(rows),
                    cleanable_bytes=sum(row['bytes'] for row in rows), retained_plans=total-len(rows),
                    message='仅整理已成功安装且完整校验的旧下载缓存；当前补丁、安装备份、恢复记录和未知文件保留。')

    def clean(self, preview_id, protected):
        preview = self._preview
        if (not preview or preview['id'] != preview_id or time.monotonic() > preview['expires']):
            raise UserError('整理预览已失效，请重新预览。', 409)
        # One confirmation is consumed once, including after a failed recheck.
        self._preview = None
        base = checked_path(self.data_root, 'updates/incremental-install')
        base.mkdir(parents=True, exist_ok=True)
        with installation_lock(base / 'install.lock'):
            rows, _ = self._scan(protected)
            selected = {row['id']: row for row in rows}
            if any(selected.get(row['id']) != row for row in preview['rows']):
                raise UserError('缓存或安装记录已变化，未开始清理，请重新预览。', 409)
            # Delete registered files individually. Never recursively remove a
            # directory that may now contain an unknown file or reparse point.
            try:
                for row in preview['rows']:
                    folder = checked_path(self.data_root, 'updates/incremental/' + row['id'])
                    for relative, before in row['files'].items():
                        path = checked_path(folder, relative)
                        if fingerprint(path) != before:
                            raise ValueError('缓存文件已变化。')
                        path.unlink()
                    for name in sorted(row['directories'], key=lambda name: len(Path(name).parts), reverse=True):
                        checked_path(folder, Path(name).relative_to(folder).as_posix()).rmdir()
                    checked_path(folder).rmdir()
            except (ValueError, OSError) as error:
                raise UserError('整理已停止，剩余文件保留；请重新预览。', 409) from error
        return dict(cleaned_plans=len(preview['rows']), cleaned_bytes=sum(row['bytes'] for row in preview['rows']),
                    message='旧下载缓存已整理，安装备份与恢复记录保留。')
