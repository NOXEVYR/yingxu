"""Explicit original Hub result -> local receipt association; never fetch media."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .ai_providers import canonical, fields, identifier
from .ai_tasks import hash_file, stamp, text
from .handoffs import _private_asset
from .hub_contract import ContractError
from .store import UserError, clean_path, now, uid

MAX_LOCAL_BYTES = 512 * 1024 * 1024
INTENT_FIELDS = ('id', 'project_id', 'task_id', 'run_id', 'binding_id', 'call_id',
                 'execution_id', 'idempotency_key', 'payload_json', 'result_id',
                 'results_manifest_sha256', 'result_json', 'receipt_plan', 'created')


class AIHubReceipts:
    def __init__(self, store, tasks, calls):
        if tasks.store is not store or calls.store is not store or calls.tasks is not tasks:
            raise ValueError('receipt store mismatch')
        self.store, self.tasks, self.calls = store, tasks, calls
        with store.lock, store.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS ai_hub_receipts(
              id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
              task_id TEXT NOT NULL REFERENCES ai_tasks(id),run_id TEXT NOT NULL REFERENCES ai_runs(id),
              binding_id TEXT NOT NULL REFERENCES ai_hub_bindings_candidate(id),call_id TEXT NOT NULL,
              execution_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,payload_json TEXT NOT NULL,
              result_id TEXT NOT NULL,results_manifest_sha256 TEXT NOT NULL,result_json TEXT NOT NULL,
              receipt_plan TEXT NOT NULL,created TEXT NOT NULL,intent_sha256 TEXT NOT NULL,
              state TEXT NOT NULL,response_json TEXT NOT NULL,updated TEXT NOT NULL,
              UNIQUE(binding_id,idempotency_key));
              CREATE INDEX IF NOT EXISTS ai_hub_receipts_run ON ai_hub_receipts(task_id,run_id,binding_id);
              CREATE UNIQUE INDEX IF NOT EXISTS ai_hub_receipts_active_result ON ai_hub_receipts(binding_id,result_id)
                WHERE state IN ('pending','completed');
              CREATE TRIGGER IF NOT EXISTS ai_hub_receipts_immutable BEFORE UPDATE OF
                id,project_id,task_id,run_id,binding_id,call_id,execution_id,idempotency_key,payload_json,
                result_id,results_manifest_sha256,result_json,receipt_plan,created,intent_sha256 ON ai_hub_receipts
              BEGIN SELECT RAISE(ABORT,'hub receipt intent immutable'); END;''')

    @staticmethod
    def _digest(row):
        return hashlib.sha256(canonical({k: row[k] for k in INTENT_FIELDS}).encode('utf-8')).hexdigest()

    @staticmethod
    def _result(result):
        # The remote opaque locator is never stored in this annex or public response.
        return {k: result[k] for k in ('result_id', 'kind', 'media_type', 'bytes', 'sha256') if k in result}

    def _snapshot(self, task, run, binding):
        original, _intent = self.calls._intent(task, run, binding)
        try:
            frozen, receipt = self.calls.bindings.submission_snapshot(original['project_id'], task, run, binding)
        except (ContractError, ValueError, TypeError, KeyError):
            raise UserError('原执行声明需要重新核对。', 409) from None
        return original, frozen, receipt

    def _selected(self, task, run, binding, result_id, manifest):
        original, frozen, receipt = self._snapshot(task, run, binding)
        result = next((r for r in receipt['results'] if r['result_id'] == result_id), None) if receipt else None
        if not receipt or receipt['provider_state'] != 'succeeded' or not result or receipt['results_manifest_sha256'] != manifest:
            raise UserError('原成功结果或整份成果声明不匹配。', 409)
        if not result.get('sha256'):
            raise UserError('原成果缺少 SHA-256 声明，不能建立已核验关联。', 409)
        return original, frozen, receipt, self._result(result)

    def _checked(self, row):
        try:
            if row['intent_sha256'] != self._digest(row) or row['state'] not in ('pending', 'completed', 'needs_attention'):
                raise ValueError('intent')
            task, frozen, receipt, result = self._selected(row['task_id'], row['run_id'], row['binding_id'],
                                                         row['result_id'], row['results_manifest_sha256'])
            payload = json.loads(row['payload_json'])
            plan = json.loads(row['receipt_plan'])
            if (task['project_id'] != row['project_id'] or frozen.body_copy()['origin']['call_id'] != row['call_id']
                    or receipt['execution_id'] != row['execution_id'] or canonical(result) != row['result_json']
                    or set(payload) != {'result_id', 'results_manifest_sha256', 'relative_path', 'role'}
                    or payload['result_id'] != row['result_id'] or payload['results_manifest_sha256'] != row['results_manifest_sha256']
                    or plan != {'idempotency_key': 'hub-receipt-' + row['id'], 'files': [{
                        'relative_path': payload['relative_path'], 'role': payload['role'], 'expected_sha256': result['sha256']}]}
                    or canonical(payload) != row['payload_json']):
                raise ValueError('binding')
            return plan, result
        except (ValueError, TypeError, KeyError):
            raise UserError('原成果关联记录需要核对，不能重新收件。', 409) from None

    def _row(self, task, run, binding, selected):
        # Scope is checked before any idempotent or historical lookup.
        self._snapshot(task, run, binding)
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM ai_hub_receipts WHERE id=? AND task_id=? AND run_id=? AND binding_id=?',
                             (selected, task, run, binding)).fetchone()
        if row is None:
            raise UserError('成果关联不属于原执行轮次。', 404)
        self._checked(row)
        return row

    def _receipt_outcome(self, row, plan):
        try:
            return self._read_receipt_outcome(row, plan)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise UserError('原收件记录需要核对，保留原关联。', 409) from None

    def _read_receipt_outcome(self, row, plan):
        with self.store.connection() as db:
            native = db.execute('SELECT * FROM ai_receipts WHERE task_id=? AND run_id=? AND idempotency_key=?',
                                (row['task_id'], row['run_id'], plan['idempotency_key'])).fetchone()
            if native is None:
                return None, None
            value = json.loads(native['result_json'])
            if (native['plan_json'] != canonical(plan['files']) or value.get('id') != native['id']
                    or value.get('task_id') != row['task_id'] or value.get('run_id') != row['run_id']
                    or value.get('state') != native['state']):
                raise UserError('原收件身份不一致，保留关联等待核对。', 409)
            outcomes = value.get('results', [])
            outcome = outcomes[0] if len(outcomes) == 1 else {}
            if native['state'] == 'completed':
                artifact = db.execute('SELECT * FROM ai_artifacts WHERE id=? AND task_id=? AND run_id=?',
                                      (outcome.get('artifact_id'), row['task_id'], row['run_id'])).fetchone()
                item = db.execute('SELECT * FROM items WHERE id=? AND project_id=?',
                                  (outcome.get('item_id'), row['project_id'])).fetchone()
                project = self.store._project(db, row['project_id'])
                run = self.tasks._run(db, row['task_id'], row['run_id'])
                directory = run['directories']['generated']['relative_path']
                expected_path = Path(project['root']) / directory / plan['files'][0]['relative_path']
                if (not artifact or outcome.get('status') != 'verified'
                        or not item or Path(item['path']) != expected_path
                        or outcome.get('sha256') != plan['files'][0]['expected_sha256']
                        or outcome.get('role') != plan['files'][0]['role']
                        or artifact['item_id'] != outcome.get('item_id')
                        or artifact['observed_sha256'] != outcome['sha256']
                        or artifact['role'] != outcome['role'] or artifact['verification_status'] != 'verified'):
                    raise UserError('原成果审核关联不一致，保留记录等待核对。', 409)
            return value, outcome

    def _public(self, row):
        plan, result = self._checked(row)
        view = {k: row[k] for k in ('id', 'task_id', 'run_id', 'binding_id', 'execution_id', 'result_id',
                                   'results_manifest_sha256', 'state', 'created')}
        view.update(relative_path=plan['files'][0]['relative_path'], result=result,
                    source_verification='matches_declared_sha256', automatic_review=False)
        if row['state'] != 'pending':
            receipt, outcome = self._receipt_outcome(row, plan)
            if not receipt or (row['state'] == 'completed') != (receipt['state'] == 'completed'):
                raise UserError('原关联完成状态需要核对。', 409)
            view.update(receipt_id=receipt['id'], receipt_state=receipt['state'])
            if row['state'] == 'completed':
                view.update(artifact_id=outcome['artifact_id'], item_id=outcome['item_id'],
                            observed_sha256=outcome['sha256'], file_verification='verified_at_receipt')
        # Never echo original receipt filesystem errors or arbitrary response_json.
        view['notice'] = '已关联原成果，审核采用仍需确认。' if row['state'] == 'completed' else '原关联保留，需明确核对收件状态。'
        return view

    def list(self, task, run, binding):
        _original, _frozen, receipt = self._snapshot(task, run, binding)
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM ai_hub_receipts WHERE task_id=? AND run_id=? AND binding_id=? ORDER BY created,id LIMIT 200',
                              (task, run, binding)).fetchall()
        return {'items': [self._public(row) for row in rows],
                'results': [self._result(r) for r in receipt['results']] if receipt and receipt['provider_state'] == 'succeeded' else [],
                'results_manifest_sha256': receipt['results_manifest_sha256'] if receipt else None}

    def get(self, task, run, binding, selected):
        identifier(selected, '成果关联标识')
        return self._public(self._row(task, run, binding, selected))

    def receive(self, task, run, binding, body):
        fields(body, ('idempotency_key', 'result_id', 'results_manifest_sha256', 'relative_path', 'role'),
               ('idempotency_key', 'result_id', 'results_manifest_sha256', 'relative_path'))
        key = identifier(body['idempotency_key'], '收件请求标识')
        manifest = body['results_manifest_sha256']
        if not isinstance(manifest, str) or not re.fullmatch('[0-9a-f]{64}', manifest):
            raise UserError('成果声明摘要无效。')
        # Do not trim/normalize Unicode remote identities: validation is by exact original membership.
        result_id = body['result_id']
        if not isinstance(result_id, str) or not 0 < len(result_id) <= 200:
            raise UserError('原成果标识无效。')
        relative = text(body['relative_path'], '本轮成果路径', 1000, True)
        role = text(body.get('role', 'result'), '成果用途', 80, True)
        try:
            for value in (result_id, relative, role): value.encode('utf-8')
            canonical(body).encode('utf-8')
            payload = canonical({'result_id': result_id, 'results_manifest_sha256': manifest,
                                 'relative_path': relative, 'role': role})
            payload.encode('utf-8')
        except UnicodeError:
            raise UserError('成果标识、路径和用途必须是有效 UTF-8 文本。') from None
        with self.calls.local_operation(), self.tasks.operation_lock:
            original, frozen, receipt, result = self._selected(task, run, binding, result_id, manifest)
            with self.store.connection() as db:
                old = db.execute('SELECT * FROM ai_hub_receipts WHERE task_id=? AND run_id=? AND binding_id=? AND idempotency_key=?',
                                 (task, run, binding, key)).fetchone()
            if old:
                self._checked(old)
                if old['payload_json'] != payload:
                    raise UserError('同一请求不能改投不同成果声明或文件。', 409)
                return self._finish(old) if old['state'] == 'pending' else self._public(old)
            # New association always needs the latest writable original round, even if another key exists.
            _task, _run, project, directory = self.tasks.receipts._boundary(task, run)
            try:
                path = self.tasks.receipts._relative(directory, relative)
                if _private_asset({'path': str(path)}, self.store.data_root, project['root']):
                    raise UserError('不能关联私密文件。', 403)
                if result['bytes'] > MAX_LOCAL_BYTES or path.stat().st_size > MAX_LOCAL_BYTES:
                    raise UserError('一次关联最多核验 512 MiB。', 413)
                actual, identity = hash_file(path)
                if actual != result['sha256'] or identity[2] != result['bytes']:
                    raise UserError('实际文件摘要或大小与原成果声明不一致。', 409)
            except OSError:
                raise UserError('本轮成果文件不可读取，请重新核对。', 409) from None
            selected = uid()
            plan = {'idempotency_key': 'hub-receipt-' + selected, 'files': [{
                'relative_path': relative, 'role': role, 'expected_sha256': actual}]}
            with self.store.lock, self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                self.calls.bindings._scope(db, original['project_id'], task, run, writable=True)
                # Revalidate frozen manifest and file identity after hashing, before intent persistence.
                current = db.execute('SELECT * FROM ai_hub_bindings_candidate WHERE id=?', (binding,)).fetchone()
                from .hub_client import validate_receipt
                try:
                    checked = validate_receipt(current['receipt_json'].encode('utf-8'), self.calls.bindings._frozen(current))['receipt']
                    if checked != receipt or stamp(clean_path(path)) != identity or path.stat().st_nlink != 1:
                        raise UserError('原成果声明或文件在核验期间改变。', 409)
                except (ContractError, OSError, ValueError, TypeError):
                    raise UserError('原成果声明或文件在核验期间改变。', 409) from None
                active = db.execute("SELECT * FROM ai_hub_receipts WHERE binding_id=? AND result_id=? AND state IN ('pending','completed')",
                                    (binding, result_id)).fetchone()
                if active:
                    if active['payload_json'] != payload or active['state'] == 'pending':
                        raise UserError('原成果已有关联，需按原请求核对，不能改投。', 409)
                    return self._public(active)
                if (db.execute('SELECT count(*) FROM ai_hub_receipts').fetchone()[0] >= 5000
                        or db.execute('SELECT count(*) FROM ai_hub_receipts WHERE run_id=?', (run,)).fetchone()[0] >= 200):
                    raise UserError('成果关联记录达到上限。', 409)
                row = dict(zip(INTENT_FIELDS, (selected, original['project_id'], task, run, binding,
                    frozen.body_copy()['origin']['call_id'], receipt['execution_id'], key, payload,
                    result_id, manifest, canonical(result), canonical(plan), now())))
                row.update(intent_sha256=self._digest(row), state='pending', response_json='{}', updated=now())
                db.execute('INSERT INTO ai_hub_receipts(' + ','.join(row) + ') VALUES(' + ','.join('?' for _ in row) + ')', tuple(row.values()))
            return self._finish(row)

    def resume(self, task, run, binding, selected):
        identifier(selected, '原关联标识')
        with self.calls.local_operation(), self.tasks.operation_lock:
            row = self._row(task, run, binding, selected)
            return self._finish(row) if row['state'] == 'pending' else self._public(row)

    def _finish(self, row):
        plan, _result = self._checked(row)
        # Original AIReceiptService handles exact completed replay across later rounds.
        # Its receiving/interrupted/partial records are never automatically redone.
        try:
            self.tasks.receipts.receive(row['task_id'], row['run_id'], plan)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise UserError('原收件记录需要核对，保留原关联。', 409) from None
        receipt, _outcome = self._receipt_outcome(row, plan)
        return self._commit(row, receipt)

    def _commit(self, row, receipt):
        with self.store.lock, self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM ai_hub_receipts WHERE id=?', (row['id'],)).fetchone()
            self._checked(current)
            if receipt is None:
                raise UserError('原收件结果尚未确认，保留原关联。', 409)
            state = 'completed' if receipt['state'] == 'completed' else 'needs_attention'
            db.execute('UPDATE ai_hub_receipts SET state=?,response_json=?,updated=? WHERE id=?',
                       (state, canonical({'receipt_id': receipt['id'], 'receipt_state': receipt['state']}), now(), row['id']))
            updated = db.execute('SELECT * FROM ai_hub_receipts WHERE id=?', (row['id'],)).fetchone()
        return self._public(updated)
