"""Validated preference for newly created projects; existing files never move."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from .store import UserError, clean_path, uid


class ProjectStorage:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings
        self.fallback_root = Path(store.project_root)
        # Keep the configured location when a disk is absent. The app and Settings
        # must still open; create_project must validate this root before any writes.
        configured = settings.get()['project_storage_root']
        if configured:
            with store.lock:
                store.project_root = Path(os.path.abspath(configured))
                try:
                    store.project_root = self._validate(configured)
                except UserError:
                    pass

    def _validate(self, value):
        self.settings.validate({'project_storage_root': value})
        if not value:
            raise UserError('请选择项目存放文件夹。')
        path = clean_path(value)
        if not path.is_dir():
            raise UserError('项目存放位置必须是文件夹。')
        if path == Path(path.anchor) or path == Path.home().resolve():
            raise UserError('请选择具体的项目文件夹，不要选择磁盘根目录或整个用户目录。')
        data = Path(self.store.data_root).resolve()
        if path == data or path.is_relative_to(data) or data.is_relative_to(path):
            raise UserError('项目存放文件夹不能与映序应用数据目录重叠。')
        protected = []
        if os.name == 'nt':
            protected = [Path(value).resolve() for key in ('WINDIR', 'ProgramFiles', 'ProgramFiles(x86)')
                         if (value := os.environ.get(key))]
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
                raise UserError('请选择本机磁盘文件夹，不使用网络映射驱动器。')
        elif sys.platform == 'darwin':
            protected = [Path('/System'), Path('/Library'), Path('/Applications'), Path('/usr'), Path('/bin'), Path('/sbin')]
        else:
            protected = [Path('/usr'), Path('/bin'), Path('/sbin'), Path('/etc'), Path('/proc'), Path('/sys'), Path('/dev')]
        if any(path == root or path.is_relative_to(root) for root in protected):
            raise UserError('请选择个人项目文件夹，不要使用系统或程序安装目录。')
        with self.store.connection() as db:
            # Include removed projects: they remain recoverable from the app trash.
            roots = [Path(row[0]).resolve() for row in db.execute('SELECT root FROM projects')]
        if any(path == root or path.is_relative_to(root) for root in roots):
            raise UserError('项目存放位置不能是已有项目或其内部文件夹。')
        try:
            # Verify readability without scanning children or changing the directory.
            with os.scandir(path):
                pass
        except OSError as error:
            raise UserError('无法访问项目存放文件夹，请检查磁盘与权限。', 409) from error
        return path

    @staticmethod
    def _probe(path):
        probe = path / ('.yingxu-storage-check-' + uid() + '.tmp')
        identity = None
        try:
            with probe.open('xb') as output:
                identity = os.fstat(output.fileno())
                output.write(b'YingXu storage check\n')
                output.flush()
                os.fsync(output.fileno())
            clean_path(path)
        except OSError as error:
            raise UserError('无法写入项目存放文件夹，请检查空间与权限。', 409) from error
        finally:
            if identity is not None:
                try:
                    current = probe.lstat()
                    if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                        probe.unlink()
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise UserError('无法清理项目位置的写入检查文件，存放位置未更改。', 409) from error

    def snapshot(self):
        with self.store.lock:
            error = ''
            try:
                self._validate(str(self.store.project_root))
            except UserError as failure:
                error = str(failure)
            with self.store.connection() as db:
                projects = [dict(row) for row in db.execute(
                    'SELECT id,name,root FROM projects WHERE removed=0 ORDER BY created DESC')]
            return {'root': str(self.store.project_root),
                    'configured_root': self.settings.get()['project_storage_root'],
                    'project_count': len(projects), 'existing_roots': projects,
                    'affects': 'new_projects', 'available': not error, 'error': error}

    def validated_root(self):
        """Called under the store lock immediately before creating new files."""
        with self.store.lock:
            return self._validate(str(self.store.project_root))

    def configure(self, body):
        if not isinstance(body, dict) or set(body) != {'root'}:
            raise UserError('项目存放位置参数无效。')
        with self.store.lock:
            path = self._validate(body['root'])
            self._probe(path)
            # Recheck after the probe, before publishing either preference or root.
            path = self._validate(str(path))
            try:
                self.settings.update({'project_storage_root': str(path)})
            except OSError as error:
                raise UserError('项目存放位置设置保存失败，当前存放位置未更改。', 409) from error
            self.store.project_root = path
            return self.snapshot()
