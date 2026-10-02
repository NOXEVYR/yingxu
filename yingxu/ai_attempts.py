"""Immutable selections attached to existing frozen runs; no media registration."""
from __future__ import annotations

import hashlib
import json

from .ai_providers import canonical, identifier
from .store import UserError, now, uid

TERMINAL = frozenset(('succeeded', 'failed', 'cancelled', 'not_submitted'))
REMOTE_PENDING = frozenset(('unknown', 'queued', 'running', 'cancel_requested'))


def result_references(results):
    """Identity includes the complete successful snapshot and position, not a remote resource ID."""
    snapshot = hashlib.sha256(canonical(results).encode('utf-8')).hexdigest()
    return snapshot, [{**result, 'result_id': hashlib.sha256(canonical(
        {'snapshot': snapshot, 'index': index, 'result': result}).encode('utf-8')).hexdigest()}
        for index, result in enumerate(results)]


class AIAttemptStore:
    def __init__(self, store, ai_tasks):
        self.store = store
        self.ai_tasks = ai_tasks
        with store.lock, store.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS ai_call_attempts(
              id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES ai_tasks(id),
              run_id TEXT NOT NULL REFERENCES ai_runs(id),project_id TEXT NOT NULL REFERENCES projects(id),
              idempotency_key TEXT NOT NULL,payload_json TEXT NOT NULL,request_id TEXT NOT NULL UNIQUE,
              input_digest TEXT NOT NULL,connection_id TEXT NOT NULL,connection_revision INTEGER NOT NULL,
              connection_json TEXT NOT NULL,operation_id TEXT NOT NULL,parameters_json TEXT NOT NULL,
              declaration_raw TEXT NOT NULL,declaration_sha256 TEXT NOT NULL,service_id TEXT NOT NULL,
              operation_json TEXT NOT NULL,state TEXT NOT NULL,native_job_id TEXT NOT NULL DEFAULT '',
              results_json TEXT NOT NULL DEFAULT '[]',error_code TEXT NOT NULL DEFAULT '',
              notice TEXT NOT NULL DEFAULT '',last_action TEXT NOT NULL DEFAULT '',
              created TEXT NOT NULL,updated TEXT NOT NULL,
              UNIQUE(task_id,run_id,idempotency_key));
              CREATE INDEX IF NOT EXISTS ai_call_attempts_run ON ai_call_attempts(task_id,run_id,created,id);
              CREATE TRIGGER IF NOT EXISTS ai_call_selection_immutable BEFORE UPDATE OF
                task_id,run_id,project_id,idempotency_key,payload_json,request_id,input_digest,
                connection_id,connection_revision,connection_json,operation_id,parameters_json,
                declaration_raw,declaration_sha256,service_id,operation_json ON ai_call_attempts
              BEGIN SELECT RAISE(ABORT,'call selection is immutable'); END;
            ''')
            # No recovered job is submitted or polled automatically.
            db.execute("UPDATE ai_call_attempts SET state='unknown',error_code='restart_during_submit',notice=?,updated=? WHERE state='submitting'",
                       ('提交响应不确定；请核对原请求，不能自动重新生成。', now()))
            db.execute("UPDATE ai_call_attempts SET state='not_submitted',error_code='restart_before_submit',notice=?,updated=? WHERE state='accepted'",
                       ('应用关闭前尚未提交；请明确准备新的尝试。', now()))

    def binding(self, db, task_id, run_id, writable=False):
        identifier(task_id, '任务标识')
        identifier(run_id, '轮次标识')
        task = self.ai_tasks._task(db, task_id, writable)
        run = self.ai_tasks._run(db, task_id, run_id)
        return task, run

    @staticmethod
    def _decode(row):
        data = dict(row)
        for name, target in (('parameters_json', 'parameters'), ('operation_json', 'operation_snapshot'),
                             ('connection_json', 'connection_snapshot'), ('results_json', 'results')):
            data[target] = json.loads(data.pop(name))
        data['supports'] = data['operation_snapshot']['supports']
        data['job_id'] = data['native_job_id']
        data['p1_description_only'] = True
        return data

    def get_internal(self, task_id, run_id, attempt_id):
        identifier(attempt_id, '执行尝试标识')
        with self.store.connection() as db:
            self.binding(db, task_id, run_id)
            row = db.execute('SELECT * FROM ai_call_attempts WHERE id=? AND task_id=? AND run_id=?',
                             (attempt_id, task_id, run_id)).fetchone()
        if row is None:
            raise UserError('执行尝试不属于此任务轮次。', 404)
        return self._decode(row)

    @staticmethod
    def public(data, summary=False):
        result = {key: value for key, value in data.items() if key not in ('payload_json', 'idempotency_key')}
        result['results_sha256'], result['results'] = result_references(data['results'])
        if summary:
            for key in ('declaration_raw', 'operation_snapshot', 'parameters'):
                result.pop(key, None)
        result['parameters_digest'] = hashlib.sha256(canonical(data['parameters']).encode('utf-8')).hexdigest()
        return result

    def existing(self, task_id, run_id, key, payload):
        with self.store.connection() as db:
            self.binding(db, task_id, run_id)
            row = db.execute('SELECT * FROM ai_call_attempts WHERE task_id=? AND run_id=? AND idempotency_key=?',
                             (task_id, run_id, key)).fetchone()
        if row:
            if row['payload_json'] != payload:
                raise UserError('同一请求标识不能用于不同连接、操作或参数。', 409)
            return self._decode(row)
        return None

    def create(self, task_id, run_id, key, payload, connection, operation, parameters):
        declared = json.loads(connection['declaration_raw'])
        attempt_id = uid()
        with self.store.lock, self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            task, run = self.binding(db, task_id, run_id, True)
            if db.execute('SELECT count(*) FROM ai_call_attempts WHERE run_id=?', (run_id,)).fetchone()[0] >= 100:
                raise UserError('每轮最多保存 100 次执行尝试。', 409)
            if db.execute('SELECT count(*) FROM ai_call_attempts').fetchone()[0] >= 10000:
                raise UserError('执行记录达到上限，请核对历史记录。', 409)
            connection_snapshot = {k: connection[k] for k in ('id', 'name', 'provider', 'base_url', 'revision')}
            values = (attempt_id, task_id, run_id, task['project_id'], key, payload, uid(), run['input_digest'],
                      connection['id'], connection['revision'], canonical(connection_snapshot), operation['id'],
                      canonical(parameters), connection['declaration_raw'], connection['declaration_sha256'],
                      declared['service_id'], canonical(operation), 'accepted', '', '[]', '',
                      '已接受；P1 仅记录结果描述，不下载或收录媒体。', '', now(), now())
            db.execute('INSERT INTO ai_call_attempts VALUES(' + ','.join('?' for _ in values) + ')', values)
        return self.get_internal(task_id, run_id, attempt_id)

    def update(self, attempt_id, **changes):
        allowed = {'state', 'native_job_id', 'results', 'error_code', 'notice', 'last_action'}
        if set(changes) - allowed:
            raise ValueError('immutable attempt fields')
        if 'results' in changes:
            changes['results_json'] = canonical(changes.pop('results'))
        changes['updated'] = now()
        with self.store.lock, self.store.connection() as db:
            db.execute('UPDATE ai_call_attempts SET ' + ','.join(k + '=?' for k in changes) + ' WHERE id=?',
                       (*changes.values(), attempt_id))

    def list(self, task_id, run_id, limit=48, offset=0):
        if type(limit) is not int or not 1 <= limit <= 48 or type(offset) is not int or not 0 <= offset <= 10000:
            raise UserError('执行记录分页无效。')
        with self.store.connection() as db:
            self.binding(db, task_id, run_id)
            rows = db.execute('SELECT * FROM ai_call_attempts WHERE task_id=? AND run_id=? ORDER BY created DESC,id LIMIT ? OFFSET ?',
                              (task_id, run_id, limit, offset)).fetchall()
            total = db.execute('SELECT count(*) FROM ai_call_attempts WHERE task_id=? AND run_id=?', (task_id, run_id)).fetchone()[0]
        return {'items': [self.public(self._decode(row), True) for row in rows], 'total': total, 'limit': limit, 'offset': offset}

    def pending_count(self):
        with self.store.connection() as db:
            return db.execute("SELECT count(*) FROM ai_call_attempts WHERE state IN ('unknown','queued','running','cancel_requested')").fetchone()[0]
