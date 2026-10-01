"""Coordinate explicit update actions with accepted project writes and shutdown."""
from contextlib import contextmanager
import json
import os
import sys
import threading
import time

from .store import UserError


class UpdateService:
    def __init__(self, app, install_root, current_version):
        from . import __build__
        self.app = app
        self.install_root = install_root
        self.current_version = current_version
        self.current_build = __build__
        self.lock = threading.RLock()
        self.action_lock = threading.RLock()
        self.writers = 0
        self.pending = None
        self.committed = False
        self.manager = None
        self.automatic = None
        self._closed = False
        self._closed_event = threading.Event()

    @property
    def closed(self):
        # This read is also used while AutomaticUpdates holds its state lock.
        # Keep it lock-free to avoid reversing the service -> automatic lock
        # order used by status().
        return self._closed_event.is_set()

    def _manager(self):
        with self.lock:
            if self.manager is None:
                if self._closed:
                    raise UserError('应用正在退出。', 409)
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
            if self._closed and self.manager is None:
                result = dict(state='idle', plan_id='', current_version=self.current_version,
                              latest_version='', download_bytes=0, total_download_bytes=0,
                              changed_files=0, reused_files=0, removed_files=0,
                              message='应用已关闭。', release_url='', can_install=sys.platform == 'win32')
            else:
                self._expire()
                result = self._manager().status()
            result['installing'] = self.committed
            result['last_install'] = self.last_install()
            result['automatic'] = self.automatic_status()
            return result

    def _automatic(self):
        with self.lock:
            if self.automatic is None:
                if self._closed:
                    raise UserError('应用正在退出。', 409)
                from .automatic_updates import AutomaticUpdates
                self.automatic = AutomaticUpdates(self)
            return self.automatic

    def start_automatic_updates(self, delay=8):
        """Schedule a delayed background check after the UI reports readiness."""
        with self.lock:
            if self._closed:
                raise UserError('应用正在退出。', 409)
            automatic = self._automatic()
        return automatic.start(delay)

    def automatic_status(self):
        with self.lock:
            automatic = self.automatic
            closed = self._closed
            if automatic is None and not closed:
                automatic = self._automatic()
        if automatic is not None:
            return automatic.status()
        return {'state': 'disabled', 'message': '应用已关闭。', 'running': False,
                'scheduled': False, 'automatic_check_enabled': False,
                'automatic_download_enabled': False, 'install_automatically': False}

    def automatic_check(self, force=True):
        """Queue an explicit, immediate check through the automatic worker."""
        with self.lock:
            if self._closed:
                raise UserError('应用正在退出。', 409)
            automatic = self._automatic()
        return automatic.check_now(force=force)

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
        with self.action_lock:
            with self.lock:
                if self._closed:
                    raise UserError('应用正在退出。', 409)
            return self._manager().plan()

    def download(self, plan_id):
        with self.action_lock:
            with self.lock:
                if self._closed:
                    raise UserError('应用正在退出。', 409)
            return self._manager().download(plan_id)

    def prepare(self, plan_id, native_pid):
        from .incremental_install import prepare_install
        if type(native_pid) is not int or native_pid <= 0:
            raise UserError('桌面进程标识无效。')
        with self.lock:
            if self._closed:
                raise UserError('应用正在退出。', 409)
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
            if self._closed:
                return
            self._closed = True
            self._closed_event.set()
            automatic = self.automatic
            pending = self.pending if self.pending and not self.committed else None
            if pending is not None:
                self.pending = None

        # Stop new scheduler work before dealing with a pending install or
        # taking the final manager snapshot. A checker can outlive this bounded
        # join; the closed gate prevents it from constructing a new manager.
        close_error = None
        if automatic is not None:
            try:
                automatic.close()
            except Exception as error:
                close_error = error
        if pending is not None:
            try:
                from .incremental_install import cancel_install
                cancel_install(self.app.store.data_root, self.install_root, pending['ticket'])
            except Exception as error:
                close_error = close_error or error
        # Capture after stopping the scheduler. _manager refuses to create a
        # manager once _closed is set, including after a timed-out check.
        with self.lock:
            manager = self.manager
        if manager is not None:
            try:
                manager.close()
                worker = getattr(manager, '_worker', None)
                if worker is not None and worker is not threading.current_thread():
                    worker.join(timeout=17)
            except Exception as error:
                close_error = close_error or error
        if close_error is not None:
            raise close_error
