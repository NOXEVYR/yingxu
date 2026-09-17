"""Bounded migration jobs and a reservation gate for application writes."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import traceback

from .store import UserError, uid


class MigrationJobs:
    def __init__(self, app, migration):
        self.app = app
        self.migration = migration
        self.lock = threading.RLock()
        self.writers = 0
        self.active = None
        self.cross_project_active = False
        self.records = {}
        self.tokens = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='yingxu-migration')

    @contextmanager
    def mutation(self, method, path=''):
        if method in ('GET', 'HEAD'):
            yield
            return
        with self.lock:
            # Retrying the accepted token must remain possible if its first
            # response was lost. submit() only permits that same token while busy.
            if self.cross_project_active:
                raise UserError('文件正在跨项目移动，请等待完成后再修改或导入。', 409)
            if self.active and not (method == 'POST' and path == '/api/project-storage/migration'):
                raise UserError('项目正在迁移，请等待完成后再修改或导入。', 409)
            self.writers += 1
        try:
            yield
        finally:
            with self.lock:
                self.writers -= 1

    @contextmanager
    def cross_project_move(self):
        """Reserve writes before taking the exporter/store locks; never invert them."""
        with self.lock:
            if self.active or self.cross_project_active or self.writers > 1:
                raise UserError('还有文件操作正在进行，请稍后再跨项目移动。', 409)
            with self.app.jobs.lock:
                if any(job['state'] in ('queued', 'running') for job in self.app.jobs.jobs.values()):
                    raise UserError('请等待导入或扫描完成后再跨项目移动。', 409)
            self.cross_project_active = True
        try:
            with self.app.context._export_lock:
                yield
        finally:
            with self.lock:
                self.cross_project_active = False

    def submit(self, body):
        if not isinstance(body, dict) or set(body) != {'token'} or not isinstance(body['token'], str):
            raise UserError('迁移参数无效。')
        token = body['token']
        if not token or len(token) > 256:
            raise UserError('迁移预览已失效，请重新预览。', 409)
        with self.lock:
            if token in self.tokens:
                return {'job_id': self.tokens[token]}
            if self.active or self.writers > 1:
                raise UserError('还有文件操作正在进行，请稍后重新迁移。', 409)
            with self.app.jobs.lock:
                if any(job['state'] in ('queued', 'running') for job in self.app.jobs.jobs.values()):
                    raise UserError('请等待当前导入或扫描完成后再迁移。', 409)
            jid = uid()
            while len(self.records) >= 16:
                oldest = next(iter(self.records))
                del self.records[oldest]
                for previous, value in list(self.tokens.items()):
                    if value == oldest:
                        del self.tokens[previous]
            self.records[jid] = {'id': jid, 'state': 'running', 'message': '准备迁移，原文件将保留作备份',
                                 'completed': 0, 'total': 0, 'result': None, 'error': ''}
            self.tokens[token] = jid
            self.active = jid
            try:
                self.pool.submit(self._run, jid, token)
            except Exception:
                self.active = None
                del self.tokens[token]
                del self.records[jid]
                raise
            return {'job_id': jid}

    def get(self, jid):
        with self.lock:
            if jid not in self.records:
                raise UserError('迁移任务记录已过期，请检查项目位置。', 404)
            return deepcopy(self.records[jid])

    def status(self):
        with self.lock:
            return {'active_job_id': self.active}

    def preview(self, body):
        with self.app.context._export_lock, self.app.store.lock:
            return self.migration.preview(body)

    def _progress(self, jid, message='', completed=None, total=None, **fields):
        if isinstance(message, dict):
            fields = dict(message)
        else:
            fields['message'] = str(message)
            if completed is not None:
                fields['completed'] = completed
            if total is not None:
                fields['total'] = total
        with self.lock:
            self.records[jid].update({key: value for key, value in fields.items()
                                      if key in ('message', 'completed', 'total')})

    def _run(self, jid, token):
        try:
            # Match ContextExporter.archive lock ordering. Re-read roots inside
            # the exporter lock so a queued export cannot publish to the backup.
            with self.app.context._export_lock, self.app.store.lock:
                result = self.migration.execute({'token': token},
                    progress=lambda *args, **kwargs: self._progress(jid, *args, **kwargs))
            try:
                self.app.changed()
            except Exception:
                # A context regeneration failure cannot undo a committed move.
                # Report the actual storage outcome, with a recoverable warning.
                traceback.print_exc()
                result.setdefault('warnings', []).append('项目位置已更新；AI 协作索引需要重新生成。')
            with self.lock:
                self.records[jid].update(state='done', message='项目已迁移，原目录已保留作备份', result=result)
        except Exception as error:
            if not isinstance(error, UserError):
                traceback.print_exc()
            with self.lock:
                self.records[jid].update(state='error', message='迁移未完成，原文件保留', error=str(error))
        finally:
            with self.lock:
                self.active = None

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=False)
