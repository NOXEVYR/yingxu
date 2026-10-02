"""Explicit call-result -> existing receipt association. No downloads, generation or approval."""
from __future__ import annotations

import json
import re

from .ai_attempts import result_references
from .ai_providers import canonical, fields, identifier
from .ai_tasks import hash_file, stamp, text
from .handoffs import _private_asset
from .store import UserError, clean_path, now, uid

MAX_LOCAL_BYTES = 512 * 1024 * 1024


class AICallReceipts:
    def __init__(self, store, tasks, attempts):
        self.store, self.tasks, self.attempts = store, tasks, attempts
        with store.lock, store.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS ai_call_receipts(
              id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES ai_tasks(id),
              run_id TEXT NOT NULL REFERENCES ai_runs(id),attempt_id TEXT NOT NULL REFERENCES ai_call_attempts(id),
              idempotency_key TEXT NOT NULL,payload_json TEXT NOT NULL,result_id TEXT NOT NULL,
              results_sha256 TEXT NOT NULL,result_json TEXT NOT NULL,source_verification TEXT NOT NULL,
              receipt_plan TEXT NOT NULL,state TEXT NOT NULL,response_json TEXT NOT NULL,
              created TEXT NOT NULL,updated TEXT NOT NULL,UNIQUE(attempt_id,idempotency_key));
              CREATE INDEX IF NOT EXISTS ai_call_receipts_run ON ai_call_receipts(task_id,run_id,attempt_id);
              CREATE UNIQUE INDEX IF NOT EXISTS ai_call_receipts_active_result ON ai_call_receipts(attempt_id,result_id)
                WHERE state IN ('pending','completed');
              CREATE TRIGGER IF NOT EXISTS ai_call_receipts_immutable BEFORE UPDATE OF
                task_id,run_id,attempt_id,idempotency_key,payload_json,result_id,results_sha256,
                result_json,source_verification,receipt_plan,created ON ai_call_receipts
              BEGIN SELECT RAISE(ABORT,'call receipt intent is immutable'); END;
            ''')

    @staticmethod
    def _public(row):
        response = json.loads(row['response_json'])
        return {**response, 'id': row['id'], 'task_id': row['task_id'], 'run_id': row['run_id'],
                'attempt_id': row['attempt_id'], 'result_id': row['result_id'],
                'results_sha256': row['results_sha256'], 'state': row['state'],
                'source_verification': row['source_verification'], 'result': json.loads(row['result_json']),
                'relative_path': json.loads(row['receipt_plan'])['files'][0]['relative_path'],
                'created': row['created'], 'automatic_review': False}

    def _row(self, db, task, run, attempt, selected):
        self.attempts.binding(db, task, run)
        row = db.execute('SELECT * FROM ai_call_receipts WHERE id=? AND task_id=? AND run_id=? AND attempt_id=?',
                         (selected, task, run, attempt)).fetchone()
        if not row:
            raise UserError('成果关联不属于原调用轮次。', 404)
        return row

    def list(self, task, run, attempt):
        self.attempts.get_internal(task, run, attempt)
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM ai_call_receipts WHERE attempt_id=? ORDER BY created,id LIMIT 200',
                              (attempt,)).fetchall()
        return {'items': [self._public(row) for row in rows]}

    def get(self, task, run, attempt, selected):
        identifier(selected)
        with self.store.connection() as db:
            return self._public(self._row(db, task, run, attempt, selected))

    def receive(self, task, run, attempt, body):
        fields(body, ('idempotency_key', 'result_id', 'relative_path', 'role', 'confirmed_unverified_source'),
               ('idempotency_key', 'result_id', 'relative_path'))
        key = identifier(body['idempotency_key'], '收件请求标识')
        result_id = body['result_id']
        if not isinstance(result_id, str) or not re.fullmatch('[0-9a-f]{64}', result_id):
            raise UserError('结果引用无效。')
        relative = text(body['relative_path'], '本轮成果路径', 1000, True)
        role = text(body.get('role', 'result'), '成果用途', 80, True)
        confirmed = body.get('confirmed_unverified_source', False)
        if type(confirmed) is not bool:
            raise UserError('来源确认必须为布尔值。')
        payload = canonical({'result_id': result_id, 'relative_path': relative, 'role': role,
                             'confirmed_unverified_source': confirmed})
        with self.tasks.operation_lock:
            # Ownership applies even to historical replay; writable/latest checks do not.
            data = self.attempts.get_internal(task, run, attempt)
            # Exact replay precedes writable/latest checks, including the receipt/link commit gap.
            with self.store.connection() as db:
                old = db.execute('SELECT * FROM ai_call_receipts WHERE task_id=? AND run_id=? AND attempt_id=? AND idempotency_key=?',
                                 (task, run, attempt, key)).fetchone()
            if old:
                if old['payload_json'] != payload:
                    raise UserError('同一请求标识不能关联不同结果或文件。', 409)
                return self._finish(old) if old['state'] == 'pending' else self._public(old)
            snapshot, references = result_references(data['results'])
            result = next((entry for entry in references if entry['result_id'] == result_id), None)
            if data['state'] != 'succeeded' or not result:
                raise UserError('请先核对工具成功结果；该引用可能已经改变。', 409)
            declared_hash = result.get('sha256')
            if not declared_hash and not confirmed:
                raise UserError('工具没有提供文件摘要；请明确确认这是手动关联，来源尚未核验。', 409)
            _task, _run, _project, directory = self.tasks.receipts._boundary(task, run)
            path = self.tasks.receipts._relative(directory, relative)
            if _private_asset({'path': str(path)}, self.store.data_root, _project['root']):
                raise UserError('不能关联私密文件。', 403)
            if path.stat().st_size > MAX_LOCAL_BYTES:
                raise UserError('一次关联最多核验 512 MiB；更大文件请使用原收件流程。', 413)
            actual, identity = hash_file(path)
            if declared_hash and actual != declared_hash:
                raise UserError('实际文件摘要与工具结果声明不一致。', 409)
            if 'size_bytes' in result and identity[2] != result['size_bytes']:
                raise UserError('实际文件大小与工具结果声明不一致。', 409)
            selected = uid()
            plan = {'idempotency_key': 'call-receipt-' + selected,
                    'files': [{'relative_path': relative, 'role': role, 'expected_sha256': actual}]}
            source = 'matches_declared_sha256' if declared_hash else 'manual_source_unverified'
            with self.store.lock, self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                self.attempts.binding(db, task, run, True)
                latest = db.execute('SELECT id FROM ai_runs WHERE task_id=? ORDER BY run_number DESC LIMIT 1', (task,)).fetchone()
                if latest['id'] != run:
                    raise UserError('只能向最新轮次关联新成果。', 409)
                current = db.execute('SELECT state,results_json FROM ai_call_attempts WHERE id=?', (attempt,)).fetchone()
                if (current['state'] != 'succeeded' or result_references(json.loads(current['results_json']))[0] != snapshot
                        or stamp(clean_path(path)) != identity):
                    raise UserError('工具结果或文件在关联期间变化，请重新核对。', 409)
                active = db.execute("SELECT * FROM ai_call_receipts WHERE attempt_id=? AND result_id=? AND state IN ('pending','completed')",
                                    (attempt, result_id)).fetchone()
                if active:
                    if active['payload_json'] != payload or active['state'] == 'pending':
                        raise UserError('此结果已有原关联；请核对原请求，不能改投其他文件。', 409)
                    return self._public(active)
                if (db.execute('SELECT count(*) FROM ai_call_receipts').fetchone()[0] >= 5000
                        or db.execute('SELECT count(*) FROM ai_call_receipts WHERE run_id=?', (run,)).fetchone()[0] >= 200):
                    raise UserError('成果关联记录达到上限，请核对历史或创建下一轮。', 409)
                values = (selected, task, run, attempt, key, payload, result_id, snapshot, canonical(result), source,
                          canonical(plan), 'pending', canonical({'notice': '收件尚待核对，未审核采用。'}), now(), now())
                db.execute('INSERT INTO ai_call_receipts VALUES(' + ','.join('?' for _ in values) + ')', values)
                row = db.execute('SELECT * FROM ai_call_receipts WHERE id=?', (selected,)).fetchone()
            return self._finish(row)

    def resume(self, task, run, attempt, selected):
        identifier(selected)
        with self.tasks.operation_lock:
            with self.store.connection() as db:
                row = self._row(db, task, run, attempt, selected)
            return self._finish(row) if row['state'] == 'pending' else self._public(row)

    def _finish(self, row):
        # No Store.lock or open database transaction spans filesystem hashing/indexing.
        plan = json.loads(row['receipt_plan'])
        receipt = self.tasks.receipts.receive(row['task_id'], row['run_id'], plan)
        outcome = receipt.get('results', [{}])[0] if len(receipt.get('results', [])) == 1 else {}
        complete = (receipt['state'] == 'completed' and outcome.get('status') == 'verified'
                    and outcome.get('sha256') == plan['files'][0]['expected_sha256'])
        response = {'receipt_id': receipt['id'], 'receipt_state': receipt['state'],
                    'notice': '已关联原成果，审核采用仍需确认。' if complete else '原收件未完成；记录保留，请处理错误后明确新建关联请求。'}
        if complete:
            response.update(artifact_id=outcome['artifact_id'], item_id=outcome['item_id'],
                            observed_sha256=outcome['sha256'], file_verification='verified_at_receipt')
        return self._commit(row, outcome, response, complete)

    def _commit(self, row, outcome, response, complete):
        with self.store.lock, self.store.connection() as db:
            self.attempts.binding(db, row['task_id'], row['run_id'])
            if complete:
                artifact = db.execute('SELECT * FROM ai_artifacts WHERE id=? AND task_id=? AND run_id=?',
                                      (outcome['artifact_id'], row['task_id'], row['run_id'])).fetchone()
                if (not artifact or artifact['item_id'] != outcome['item_id']
                        or artifact['observed_sha256'] != outcome['sha256']):
                    raise UserError('原成果关联不一致，保留待核对记录。', 409)
            db.execute('UPDATE ai_call_receipts SET state=?,response_json=?,updated=? WHERE id=?',
                       ('completed' if complete else 'needs_attention', canonical(response), now(), row['id']))
            updated = db.execute('SELECT * FROM ai_call_receipts WHERE id=?', (row['id'],)).fetchone()
        return self._public(updated)
