"""Single bounded worker for explicit local calls; remote pending is not busy."""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import threading
import time

from .ai_attempts import AIAttemptStore, REMOTE_PENDING, TERMINAL
from .ai_call_receipts import AICallReceipts
from .ai_providers import (HTTPProvider, MAX_PARAMETERS, ProviderError, canonical,
                           fields, identifier, safe_value, validate_input)
from .store import UserError


class AICallService:
    def __init__(self, store, ai_tasks, migration_jobs, connections, *, queue_limit=8,
                 request_timeout=2.0, query_window=10.0, query_interval=0.25):
        if (type(queue_limit) is not int or not 1 <= queue_limit <= 32
                or not 0.05 <= request_timeout <= 5 or not 0 <= query_window <= 60
                or not 0.05 <= query_interval <= 5):
            raise ValueError('invalid bounded worker settings')
        self.store = store
        self.ai_tasks = ai_tasks
        self.migration_jobs = migration_jobs
        self.connections = connections
        self.attempts = AIAttemptStore(store, ai_tasks)
        self.receipts = AICallReceipts(store, ai_tasks, self.attempts)
        self.queue_limit = queue_limit
        self.request_timeout = request_timeout
        self.query_window = query_window
        self.query_interval = query_interval
        self._condition = threading.Condition(threading.RLock())
        self._pending = {}
        self._active = None
        self._active_provider = None
        self._local_busy = False
        self._closed = False
        self._worker = threading.Thread(target=self._loop, name='yingxu-ai-call', daemon=True)
        self._worker.start()

    def _mutation(self):
        return self.migration_jobs.mutation('POST', '/api/ai-calls') if self.migration_jobs else nullcontext()

    def _available(self, attempt_id=None):
        if self._closed:
            raise UserError('工具调用已关闭，不能接受新操作。', 409)
        if attempt_id in self._pending or (self._active and self._active['id'] == attempt_id):
            return False
        if len(self._pending) + int(self._active is not None) >= self.queue_limit:
            raise UserError('工具调用队列已满，请等待本地操作结束。', 429)
        return True

    def _enqueue(self, data, action, *, ready=None, deadline=None, interval=None):
        self._pending[data['id']] = {
            'id': data['id'], 'task_id': data['task_id'], 'run_id': data['run_id'], 'action': action,
            'ready': time.monotonic() if ready is None else ready,
            'deadline': time.monotonic() + self.query_window if deadline is None else deadline,
            'interval': self.query_interval if interval is None else interval,
        }
        self._condition.notify_all()

    def create(self, task_id, run_id, body):
        fields(body, ('connection_id', 'operation_id', 'parameters', 'idempotency_key'),
               ('connection_id', 'operation_id', 'parameters', 'idempotency_key'))
        identifier(body['connection_id'], '连接标识')
        identifier(body['operation_id'], '操作标识')
        key = identifier(body['idempotency_key'], '请求标识')
        safe_value(body['parameters'])
        if len(canonical(body['parameters']).encode('utf-8')) > MAX_PARAMETERS:
            raise UserError('输入参数超过 32 KiB。', 413)
        payload = canonical({k: body[k] for k in ('connection_id', 'operation_id', 'parameters')})
        with self._mutation(), self._condition:
            previous = self.attempts.existing(task_id, run_id, key, payload)
            if previous:
                return self.attempts.public(previous)
            self._available()
            # No network here: an explicit successful connection check is required.
            with self.connections.lock:
                connection, secret = self.connections.resolve(body['connection_id'])
                if connection['provider'] != 'yingxu-http-v1':
                    raise UserError('此连接仅支持能力选型，不能提交执行请求。', 409)
                if not connection['declaration_raw']:
                    raise UserError('请先明确检查连接及操作声明。', 409)
                safe_value(body['parameters'], (secret,))
                declared = json.loads(connection['declaration_raw'])
                operation = next((x for x in declared['operations'] if x['id'] == body['operation_id']), None)
                if operation is None:
                    raise UserError('连接中没有此操作。', 409)
                validate_input(operation['input_schema'], body['parameters'])
                data = self.attempts.create(task_id, run_id, key, payload, connection, operation, body['parameters'])
            self._enqueue(data, 'submit')
            return self.attempts.public(data)

    def get(self, task_id, run_id, attempt_id):
        data = self.attempts.get_internal(task_id, run_id, attempt_id)
        result = self.attempts.public(data)
        with self._condition:
            result['local_action'] = (self._active['action'] if self._active and self._active['id'] == attempt_id
                                      else self._pending.get(attempt_id, {}).get('action', ''))
        result['monitoring'] = result['local_action'] in ('query', 'lookup')
        return result

    def list(self, task_id, run_id, limit=48, offset=0):
        return self.attempts.list(task_id, run_id, limit, offset)

    def _control(self, task_id, run_id, attempt_id, action):
        with self._mutation(), self._condition:
            data = self.attempts.get_internal(task_id, run_id, attempt_id)
            if data['state'] in TERMINAL:
                raise UserError('执行尝试已经终结，不能继续查询或取消。', 409)
            pending = self._pending.get(attempt_id)
            if action == 'query' and pending and pending['action'] in ('submit', 'cancel'):
                raise UserError('本地提交或取消仍在排队，请稍后核对。', 409)
            if action == 'cancel' and data['state'] == 'accepted' and attempt_id in self._pending:
                del self._pending[attempt_id]
                self.attempts.update(attempt_id, state='cancelled', last_action='local_cancel',
                                     notice='尚未提交工具；已取消本地尝试。')
                self._condition.notify_all()
                return self.get(task_id, run_id, attempt_id)
            connection, _secret = self.connections.resolve(data['connection_id'], data['connection_revision'])
            if connection['base_url'] != data['connection_snapshot']['base_url']:
                raise UserError('原服务连接已变化，请人工核对。', 409)
            if action == 'query':
                action = 'query' if data['native_job_id'] else 'lookup'
            if not data['supports'][action]:
                raise UserError('此操作未声明支持查询、重查或取消。', 409)
            if action == 'cancel' and not data['native_job_id']:
                raise UserError('尚无工具任务标识，不能冒充取消成功。', 409)
            if self._active and self._active['id'] == attempt_id:
                raise UserError('此尝试已有本地操作在途，请稍后核对。', 409)
            if attempt_id in self._pending:
                del self._pending[attempt_id]
            self._available(attempt_id)
            self.attempts.update(attempt_id, last_action=action + '_queued',
                                 notice='取消申请已排队，尚未确认。' if action == 'cancel' else '核对请求已排队，不会重新生成。')
            self._enqueue(data, action)
            return self.get(task_id, run_id, attempt_id)

    def query(self, task_id, run_id, attempt_id):
        return self._control(task_id, run_id, attempt_id, 'query')

    def cancel(self, task_id, run_id, attempt_id):
        return self._control(task_id, run_id, attempt_id, 'cancel')

    def status(self):
        with self._condition:
            state = {'local_busy': self._local_busy, 'busy': self._local_busy,
                     'queued': len(self._pending), 'closed': self._closed,
                     'local_queued': sum(x['action'] in ('submit', 'cancel') for x in self._pending.values())
                                     + int(bool(self._active and self._active['action'] in ('submit', 'cancel')
                                                and not self._local_busy)),
                     'local_action': self._active['action'] if self._active else '',
                     'active_attempt_id': self._active['id'] if self._active else ''}
        state['remote_pending'] = self.attempts.pending_count()
        return state

    def close(self, timeout=2.5):
        """Bounded stop; no remote cancellation and no automatic replay on reopen."""
        timeout = max(0.0, min(float(timeout), 5.0))
        with self._condition:
            self._closed = True
            self._pending.clear()
            provider = self._active_provider
            self._condition.notify_all()
        if provider:
            provider.abort()
        self._worker.join(timeout)
        result = self.status()
        result['worker_stopped'] = not self._worker.is_alive()
        return result

    def _loop(self):
        while True:
            with self._condition:
                while not self._closed:
                    if not self._pending:
                        self._condition.wait()
                        continue
                    work = min(self._pending.values(), key=lambda x: x['ready'])
                    delay = work['ready'] - time.monotonic()
                    if delay > 0:
                        self._condition.wait(delay)
                        continue
                    self._pending.pop(work['id'])
                    self._active = work
                    break
                if self._closed:
                    return
            try:
                self._perform(work)
            except Exception:
                # Do not expose provider responses, URLs, credentials or reprs.
                try:
                    data = self.attempts.get_internal(work['task_id'], work['run_id'], work['id'])
                    self.attempts.update(work['id'], state='unknown' if data['state'] == 'submitting' else data['state'],
                                         error_code='internal_error', notice='本地操作中断，请核对原请求。')
                except Exception:
                    pass
            finally:
                with self._condition:
                    self._active = None
                    self._active_provider = None
                    self._local_busy = False
                    self._condition.notify_all()

    def _perform(self, work):
        action = work['action']
        submitted = False
        data = None
        try:
            # Only short submit/control writes reserve the migration gate.
            with self._mutation() if action in ('submit', 'cancel') else nullcontext():
                if action in ('submit', 'cancel'):
                    with self._condition:
                        self._local_busy = True
                data = self.attempts.get_internal(work['task_id'], work['run_id'], work['id'])
                if data['state'] in TERMINAL:
                    return
                connection, secret = self.connections.resolve(data['connection_id'], data['connection_revision'])
                provider = HTTPProvider(connection, secret, self.request_timeout)
                with self._condition:
                    if self._closed:
                        raise ProviderError('stopped', '本地操作已停止。')
                    self._active_provider = provider
                current, _raw = provider.describe()
                if current['service_id'] != data['service_id']:
                    raise ProviderError('service_changed', '服务身份已变化；不能向新服务查询旧任务。')
                if action == 'submit':
                    current_operation = next((x for x in current['operations'] if x['id'] == data['operation_id']), None)
                    if current_operation != data['operation_snapshot']:
                        raise ProviderError('operation_changed', '操作声明已变化，请明确准备新尝试。')
                    if hashlib.sha256(_raw).hexdigest() != data['declaration_sha256']:
                        raise ProviderError('declaration_changed', '声明版本已变化，请重新检查并明确准备新尝试。')
                    # Recheck config and frozen binding immediately before sending.
                    with self.connections.lock:
                        self.connections.resolve(data['connection_id'], data['connection_revision'])
                        with self.store.connection() as db:
                            self.attempts.binding(db, data['task_id'], data['run_id'], True)
                        safe_value(data['parameters'], (secret,))
                        self.attempts.update(data['id'], state='submitting', last_action='submit', error_code='',
                                             notice='提交中；响应不确定时不会自动重放生成。')
                    submitted = True
                elif action == 'cancel':
                    self.attempts.update(data['id'], last_action='cancel',
                                         notice='正在申请取消，尚未得到服务确认。')
                result = provider.call(action, data)
                with self._condition:
                    stopped = self._closed
                if stopped and action == 'submit':
                    raise ProviderError('stopped_during_submit', '应用关闭时提交结果待核对。', submitted=True)
                # A confirmed job response is evidence of provider state, not media acceptance.
                self.attempts.update(data['id'], **result, error_code='', last_action=action,
                                     notice='工具已确认取消。' if result['state'] == 'cancelled' else
                                            '工具结果可用；P1 只保存未核验描述，尚未下载或收件。' if result['state'] == 'succeeded' else
                                            '取消已申请，仍待服务确认。' if result['state'] == 'cancel_requested' else
                                            '已记录工具状态；生成、收件与审核分别判定。')
                data.update(result)
        except (UserError, ProviderError) as error:
            if data is None:
                # Binding failures (e.g. project recycled) still leave recoverable evidence.
                self.attempts.update(work['id'], state='not_submitted' if action == 'submit' else 'unknown',
                                     error_code='binding_or_migration_conflict', notice='项目或迁移状态阻止本地操作，请核对原请求。')
                return
            may_have_submitted = submitted and getattr(error, 'submitted', False)
            if action == 'submit':
                state = 'unknown' if may_have_submitted else 'not_submitted'
            elif action == 'cancel' and getattr(error, 'submitted', False):
                state = 'cancel_requested'
            else:
                state = data['state']
            self.attempts.update(data['id'], state=state, error_code=getattr(error, 'code', 'connection_changed'),
                                 notice='提交结果不确定，请按原请求核对；不会自动重新生成。' if state == 'unknown' else
                                        '取消响应不确定，不能视为已经取消。' if action == 'cancel' and state == 'cancel_requested' else
                                        '本地操作未完成，请核对连接、迁移或工具支持；不会重新生成。',
                                 last_action=action)
            return
        finally:
            with self._condition:
                self._local_busy = False
        if data and data['state'] in REMOTE_PENDING and data['supports']['query'] and data['native_job_id']:
            with self._condition:
                if self._closed:
                    return
                interval = min(work['interval'] * 2, 2.0)
                ready = time.monotonic() + interval
                if ready < work['deadline'] and len(self._pending) < self.queue_limit:
                    self._enqueue(data, 'query', ready=ready, deadline=work['deadline'], interval=interval)
