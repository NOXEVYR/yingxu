"""Coordinate explicit update actions with accepted project writes and shutdown."""
from contextlib import contextmanager
import json
import os
import threading
import time

from .store import UserError


class UpdateService:
    def __init__(self, app, install_root, current_version):
        self.app = app
        self.install_root = install_root
        self.current_version = current_version
        self.lock = threading.RLock()
        self.writers = 0
        self.pending = None
        self.committed = False
        self.manager = None

    def _manager(self):
        with self.lock:
            if self.manager is None:
                from .incremental_update import UpdateManager
                self.manager = UpdateManager(self.app.store.data_root, self.install_root, self.current_version)
            return self.manager

    def _expire(self):
        if self.pending and not self.committed and time.monotonic() > self.pending['expires']:
            from .incremental_install import cancel_install
            cancel_install(self.app.store.data_root, self.install_root, self.pending['ticket'])
            self.pending = None

    @contextmanager
    def mutation(self, method, path):
        if method in ('GET', 'HEAD'):
            yield
            return
        with self.lock:
            self._expire()
            if (self.pending or self.committed) and path not in (
                    '/api/updates/install/commit', '/api/updates/install/cancel'):
                raise UserError('正在准备退出并更新，请等待完成。', 409)
            self.writers += 1
        try:
            yield
        finally:
            with self.lock:
                self.writers -= 1

    def status(self):
        with self.lock:
            self._expire()
            result = self._manager().status()
            result['installing'] = self.committed
            result['last_install'] = self.last_install()
            return result

    def last_install(self):
        from .incremental_update import checked_path, read_file
        try:
            path = checked_path(self.app.store.data_root, 'updates/incremental-install/last-install.json')
            if not path.exists():
                return None
            record = json.loads(read_file(path, 16384))
            if record.get('state') not in ('installed', 'rolled_back', 'recovery_required', 'failed', 'cancelled'):
                return None
            return {'state': record['state'], 'version': str(record.get('version', ''))[:64],
                    'message': str(record.get('message', ''))[:2000],
                    'restarted': record.get('restarted') is not False}
        except (OSError, ValueError, TypeError, AttributeError):
            return {'state': 'failed', 'version': '', 'message': '上次更新记录无法读取，请保留更新记录以便检查。'}

    def plan(self):
        return self._manager().plan()

    def download(self, plan_id):
        return self._manager().download(plan_id)

    def prepare(self, plan_id, native_pid):
        from .incremental_install import prepare_install
        if type(native_pid) is not int or native_pid <= 0:
            raise UserError('桌面进程标识无效。')
        with self.lock:
            self._expire()
            if self.pending or self.committed or self.writers > 1:
                raise UserError('还有操作正在进行，请稍后再安装更新。', 409)
            manager = self._manager()
            state = manager.status()
            if state.get('state') != 'ready' or state.get('plan_id') != plan_id:
                raise UserError('更新尚未下载完成，请重新检查。', 409)
            manager.ready_plan(plan_id)
            migration = self.app.migration_jobs
            with migration.lock:
                if migration.active or migration.cross_project_active or migration.writers:
                    raise UserError('请等待项目迁移或文件操作完成后再更新。', 409)
                with self.app.jobs.lock:
                    if any(job['state'] in ('queued', 'running') for job in self.app.jobs.jobs.values()):
                        raise UserError('请等待导入或扫描完成后再更新。', 409)
                # All request mutations pass this lock; holding it here closes
                # the interval between checking writes and reserving installation.
                try:
                    result = prepare_install(self.app.store.data_root, self.install_root, plan_id, native_pid)
                except (ValueError, OSError, RuntimeError) as error:
                    raise UserError(str(error), 409) from error
                self.pending = {'ticket': result['ticket'], 'native_pid': native_pid,
                                'backend_pid': os.getpid(), 'plan_id': plan_id,
                                'expires': time.monotonic() + 120}
                return result

    def commit(self, ticket):
        from .incremental_install import commit_install
        with self.lock:
            self._expire()
            if not self.pending or self.pending['ticket'] != ticket:
                raise UserError('更新安装确认已失效，请重新开始。', 409)
            try:
                result = commit_install(self.app.store.data_root, self.install_root, ticket)
            except (ValueError, OSError, RuntimeError) as error:
                raise UserError(str(error), 409) from error
            self.committed = True
            return result

    def cancel(self, ticket):
        from .incremental_install import cancel_install
        with self.lock:
            if self.committed:
                raise UserError('更新已开始，请等待完成。', 409)
            if not self.pending or self.pending['ticket'] != ticket:
                raise UserError('更新安装确认已失效。', 409)
            result = cancel_install(self.app.store.data_root, self.install_root, ticket)
            self.pending = None
            return result

    def install_status(self, ticket=None):
        with self.lock:
            self._expire()
            if ticket is None and not self.pending:
                return {'prepared': False, 'committed': False}
            if ticket is None:
                ticket = self.pending['ticket']
            if not self.pending or self.pending['ticket'] != ticket:
                raise UserError('更新安装确认已失效。', 409)
            return {key: value for key, value in self.pending.items() if key != 'expires'} | {
                'committed': self.committed, 'prepared': True}

    def close(self):
        with self.lock:
            manager = self.manager
            if self.pending and not self.committed:
                from .incremental_install import cancel_install
                cancel_install(self.app.store.data_root, self.install_root, self.pending['ticket'])
                self.pending = None
        if manager is not None:
            manager.close()
