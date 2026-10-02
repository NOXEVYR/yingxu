"""Immutable AI Hub selections on original frozen runs, separate from jobs."""
from __future__ import annotations

import hashlib
import json

from .aihub_interop import AIHubProvider, PROTOCOL, digest, service_error
from .ai_providers import ProviderError, canonical, fields, identifier
from .store import UserError, now, uid


class AICapabilitySelections:
    def __init__(self, store, ai_tasks, connections):
        self.store, self.tasks, self.connections = store, ai_tasks, connections
        with store.lock, store.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS ai_capability_selections(
                id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES ai_tasks(id),
                run_id TEXT NOT NULL REFERENCES ai_runs(id),project_id TEXT NOT NULL REFERENCES projects(id),
                idempotency_key TEXT NOT NULL,payload_json TEXT NOT NULL,input_digest TEXT NOT NULL,
                connection_id TEXT NOT NULL,connection_revision INTEGER NOT NULL,connection_json TEXT NOT NULL,
                capability_id TEXT NOT NULL,snapshot_raw TEXT NOT NULL,response_sha256 TEXT NOT NULL,
                created TEXT NOT NULL,UNIQUE(task_id,run_id,idempotency_key));
                CREATE INDEX IF NOT EXISTS ai_capability_selections_run ON ai_capability_selections(task_id,run_id,created,id);
                CREATE TRIGGER IF NOT EXISTS ai_capability_selection_immutable BEFORE UPDATE ON ai_capability_selections
                BEGIN SELECT RAISE(ABORT,'capability selection is immutable'); END;
            ''')

    def _binding(self, db, task_id, run_id, writable=False):
        identifier(task_id, '任务标识')
        identifier(run_id, '轮次标识')
        task = self.tasks._task(db, task_id, writable)
        run = self.tasks._run(db, task_id, run_id)
        return task, run

    @staticmethod
    def _public(row, detail=False):
        snapshot = json.loads(row['snapshot_raw'])
        value = {key: row[key] for key in ('id', 'task_id', 'run_id', 'project_id', 'connection_id',
                                         'connection_revision', 'capability_id', 'input_digest', 'created')}
        value.update({'connection_name': json.loads(row['connection_json'])['name'],
                      'name': snapshot['declaration']['parsed'].get('name', snapshot['selected']['key']),
                      'key': snapshot['selected']['key'], 'service_id': snapshot['identity']['service_instance_id'],
                      'source_connection_revision': snapshot['connection_revision'],
                      'declaration_sha256': snapshot['declaration']['sha256'],
                      'response_sha256': row['response_sha256'], 'execution_allowed': False,
                      'actual_invocation': 'unverified', 'dispatch_version_locked': False})
        if detail:
            value['snapshot'] = snapshot
        return value

    def _existing(self, db, task_id, run_id, key, payload):
        row = db.execute('SELECT * FROM ai_capability_selections WHERE task_id=? AND run_id=? AND idempotency_key=?',
                         (task_id, run_id, key)).fetchone()
        if row and row['payload_json'] != payload:
            raise UserError('同一选型请求标识不能用于不同能力或连接版本。', 409)
        return row

    def get(self, task_id, run_id, selection_id):
        identifier(selection_id, '选型记录')
        with self.store.connection() as db:
            self._binding(db, task_id, run_id)
            row = db.execute('SELECT * FROM ai_capability_selections WHERE id=? AND task_id=? AND run_id=?',
                             (selection_id, task_id, run_id)).fetchone()
        if row is None:
            raise UserError('选型记录不属于本轮。', 404)
        return self._public(row, True)

    def list(self, task_id, run_id, limit=24, offset=0):
        if type(limit) is not int or not 1 <= limit <= 24 or type(offset) is not int or not 0 <= offset <= 256:
            raise UserError('选型记录分页无效。')
        with self.store.connection() as db:
            self._binding(db, task_id, run_id)
            rows = db.execute('SELECT * FROM ai_capability_selections WHERE task_id=? AND run_id=? ORDER BY created DESC,id LIMIT ? OFFSET ?',
                              (task_id, run_id, limit, offset)).fetchall()
            total = db.execute('SELECT count(*) FROM ai_capability_selections WHERE run_id=?', (run_id,)).fetchone()[0]
        return {'items': [self._public(row) for row in rows], 'total': total, 'limit': limit, 'offset': offset}

    @staticmethod
    def _describe(row, secret):
        previous = json.loads(row['declaration_raw'])
        current, _raw = AIHubProvider(row, secret).describe()
        for key in ('identity', 'workspace', 'workspace_root', 'connection_revision'):
            if current[key] != previous[key]:
                raise UserError('曜核实例或工作区已变化，请重新检查连接；原选型保持不变。', 409)
        return current

    def create(self, task_id, run_id, body):
        fields(body, ('connection_id', 'connection_revision', 'source_connection_revision',
                      'capability_id', 'idempotency_key'),
               ('connection_id', 'connection_revision', 'source_connection_revision',
                'capability_id', 'idempotency_key'))
        identifier(body['connection_id'], '连接标识')
        identifier(body['capability_id'], '能力 ID')
        key = identifier(body['idempotency_key'], '选型请求标识')
        digest(body['source_connection_revision'])
        if type(body['connection_revision']) is not int or not 1 <= body['connection_revision'] <= 2 ** 31:
            raise UserError('连接修订无效。')
        payload = canonical({k: body[k] for k in body if k != 'idempotency_key'})
        with self.store.connection() as db:
            self._binding(db, task_id, run_id)
            previous = self._existing(db, task_id, run_id, key, payload)
            if previous:
                return self._public(previous, True)
        row, secret = self.connections.resolve(body['connection_id'], body['connection_revision'])
        if row['provider'] != PROTOCOL or not row['declaration_raw']:
            raise UserError('请选择已检查的曜核只读连接。', 409)
        checked = json.loads(row['declaration_raw'])
        if checked['connection_revision'] != body['source_connection_revision']:
            raise UserError('所选曜核连接已变化，请重新选择。', 409)
        try:
            describe = self._describe(row, secret)
            _snapshot, raw = AIHubProvider(row, secret).snapshot(describe, body['capability_id'])
        except ProviderError as error:
            raise service_error(error) from None
        with self.connections.lock, self.store.lock, self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            task, run = self._binding(db, task_id, run_id, True)
            previous = self._existing(db, task_id, run_id, key, payload)
            if previous:
                return self._public(previous, True)
            current = self.connections._get(row['id'])
            if current['revision'] != row['revision'] or not current['enabled'] or current['declaration_sha256'] != row['declaration_sha256']:
                raise UserError('读取快照期间连接已变化，请重新检查。', 409)
            if db.execute('SELECT count(*) FROM ai_capability_selections WHERE run_id=?', (run_id,)).fetchone()[0] >= 24:
                raise UserError('本轮最多保存24个选型快照。', 409)
            count, size = db.execute('SELECT count(*),coalesce(sum(length(cast(snapshot_raw AS BLOB))),0) FROM ai_capability_selections').fetchone()
            if count >= 256 or size + len(raw) > 16 * 1024 ** 2:
                raise UserError('选型快照达到容量上限，请核对已有记录。', 409)
            selection_id = uid()
            connection = {k: row[k] for k in ('id', 'name', 'provider', 'base_url', 'revision')}
            values = (selection_id, task_id, run_id, task['project_id'], key, payload, run['input_digest'],
                      row['id'], row['revision'], canonical(connection), body['capability_id'],
                      raw.decode('utf-8'), hashlib.sha256(raw).hexdigest(), now())
            db.execute('INSERT INTO ai_capability_selections VALUES(' + ','.join('?' for _ in values) + ')', values)
        return self.get(task_id, run_id, selection_id)

    def verify(self, task_id, run_id, selection_id):
        selected = self.get(task_id, run_id, selection_id)
        row, secret = self.connections.resolve(selected['connection_id'], selected['connection_revision'])
        original = selected['snapshot']
        if row['provider'] != PROTOCOL:
            raise UserError('原连接类型已变化。', 409)
        try:
            current, _ = AIHubProvider(row, secret).describe()
            for key in ('identity', 'workspace', 'workspace_root', 'connection_revision'):
                if current[key] != original[key]:
                    raise UserError('原实例或工作区已变化，旧选型保持原样。', 409)
            AIHubProvider(row, secret).snapshot(current, selected['capability_id'], selected['declaration_sha256'])
        except ProviderError as error:
            raise service_error(error) from None
        with self.connections.lock, self.store.connection() as db:
            self._binding(db, task_id, run_id)
            latest = self.connections._get(row['id'])
            if latest['revision'] != row['revision'] or not latest['enabled']:
                raise UserError('核对期间连接配置已变化，未确认匹配。', 409)
        # No UPDATE: current heartbeat is not evidence for the original selection.
        return {'id': selected['id'], 'task_id': task_id, 'run_id': run_id,
                'declaration_matches': True, 'execution_allowed': False,
                'dispatch_version_locked': False, 'actual_invocation': 'unverified'}
