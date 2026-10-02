"""Opt-in private DB annex. No networking, credentials, GUI or receipt writes.

Callers supply an explicit adapter attempt; this is independent from the old
direct-http attempt store and never changes that store's state. Startup recovery
is explicit, only after the previous process has stopped, never an init side effect.
"""
import hashlib
import json
import re

from .hub_prepare import FrozenRequest, prepare as freeze_request
from .hub_contract import ContractError, canonical
from .hub_client import validate_receipt
from .store import UserError, now, uid


class HubBindings:
    def __init__(self, store, tasks):
        if tasks.store is not store:
            raise ValueError('task store mismatch')
        self.store, self.tasks = store, tasks
        with store.lock, store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ai_hub_bindings_candidate(
              id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
              project_id TEXT NOT NULL REFERENCES projects(id),
              task_id TEXT NOT NULL REFERENCES ai_tasks(id), run_id TEXT NOT NULL REFERENCES ai_runs(id),
              call_id TEXT NOT NULL, local_request_id TEXT NOT NULL UNIQUE, wire_request_id TEXT NOT NULL UNIQUE,
              input_digest TEXT NOT NULL, connection_id TEXT NOT NULL, connection_revision INTEGER NOT NULL,
              payload BLOB NOT NULL, payload_sha256 TEXT NOT NULL,
              remote_authority TEXT NOT NULL, ledger_epoch TEXT NOT NULL, expected_client TEXT NOT NULL,
              local_state TEXT NOT NULL, receipt_json TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
              UNIQUE(task_id,run_id,call_id))''')
            db.execute('''CREATE TRIGGER IF NOT EXISTS ai_hub_binding_candidate_immutable BEFORE UPDATE OF
              id,idempotency_key,project_id,task_id,run_id,call_id,local_request_id,wire_request_id,input_digest,
              connection_id,connection_revision,payload,payload_sha256,remote_authority,ledger_epoch,expected_client,
              created ON ai_hub_bindings_candidate
              BEGIN SELECT RAISE(ABORT,'hub original binding is immutable'); END''')
            db.execute('CREATE INDEX IF NOT EXISTS ai_hub_binding_candidate_run ON ai_hub_bindings_candidate(task_id,run_id)')

    def _scope(self, db, project_id, task_id, run_id, writable=False):
        task = self.tasks._task(db, task_id, writable)
        run = self.tasks._run(db, task_id, run_id)
        if task['project_id'] != project_id:
            raise UserError('原项目与任务不匹配。', 409)
        if writable:
            latest = db.execute('SELECT id FROM ai_runs WHERE task_id=? ORDER BY run_number DESC LIMIT 1', (task_id,)).fetchone()
            if latest is None or latest['id'] != run_id:
                raise UserError('只能为当前最新可写轮次准备或提交。', 409)
        return run

    def _row(self, db, project_id, task_id, run_id, binding_id):
        self._scope(db, project_id, task_id, run_id)
        row = db.execute('SELECT * FROM ai_hub_bindings_candidate WHERE id=? AND project_id=? AND task_id=? AND run_id=?',
                         (binding_id, project_id, task_id, run_id)).fetchone()
        if row is None:
            raise UserError('执行绑定不属于原项目任务轮次。', 404)
        return row

    @staticmethod
    def _frozen(row):
        payload = bytes(row['payload'])
        if hashlib.sha256(payload).hexdigest() != row['payload_sha256']:
            raise ContractError('stored_payload_integrity')
        return FrozenRequest(payload, row['remote_authority'], row['ledger_epoch'], row['expected_client'],
                             row['input_digest'], row['local_request_id'])

    def prepare(self, key, attempt, selection, descriptor, input_json, **options):
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', key):
            raise ContractError('idempotency_key')
        if type(attempt.get('connection_revision')) is not int or attempt['connection_revision'] < 1:
            raise ContractError('local_connection_revision')
        frozen = freeze_request(attempt, selection, descriptor, input_json, **options)
        with self.tasks.operation_lock, self.store.lock, self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._prepare_in_transaction(db, key, attempt, frozen)

    def _prepare_in_transaction(self, db, key, attempt, frozen):
        """Caller holds task/store locks and an open transaction, with no network."""
        body = frozen.body_copy()
        scope = (attempt['project_id'], attempt['task_id'], attempt['run_id'])
        intent = (frozen.payload, frozen.service_authority, frozen.ledger_epoch, frozen.expected_client,
                  frozen.original_input_digest, frozen.local_request_id, attempt['connection_id'], attempt['connection_revision'])
        self._scope(db, *scope)
        old = db.execute('SELECT * FROM ai_hub_bindings_candidate WHERE idempotency_key=?', (key,)).fetchone()
        if old:
            old_intent = (bytes(old['payload']), old['remote_authority'], old['ledger_epoch'], old['expected_client'],
                          old['input_digest'], old['local_request_id'], old['connection_id'], old['connection_revision'])
            if tuple(old[k] for k in ('project_id', 'task_id', 'run_id')) != scope or intent != old_intent:
                raise UserError('同一幂等标识不能跨范围或改变原意图。', 409)
            return self._public(old)
        run = self._scope(db, *scope, writable=True)
        if run['input_digest'] != frozen.original_input_digest:
            raise UserError('原轮输入摘要不匹配。', 409)
        if db.execute('SELECT count(*) FROM ai_hub_bindings_candidate WHERE run_id=?', (scope[2],)).fetchone()[0] >= 100 or db.execute('SELECT count(*) FROM ai_hub_bindings_candidate').fetchone()[0] >= 5000:
            raise UserError('持久绑定达到有界数量上限。', 409)
        if db.execute('SELECT 1 FROM ai_hub_bindings_candidate WHERE local_request_id=? OR wire_request_id=? OR (task_id=? AND run_id=? AND call_id=?)',
                      (frozen.local_request_id, body['request_id'], scope[1], scope[2], attempt['id'])).fetchone():
            raise UserError('原调用身份已经冻结，不能用新键重复准备。', 409)
        stamp = now()
        binding_id = uid()
        db.execute('INSERT INTO ai_hub_bindings_candidate VALUES(' + ','.join('?' for _ in range(20)) + ')',
                   (binding_id, key, *scope, attempt['id'], frozen.local_request_id, body['request_id'],
                    frozen.original_input_digest, attempt['connection_id'], attempt['connection_revision'], frozen.payload,
                    hashlib.sha256(frozen.payload).hexdigest(), frozen.service_authority, frozen.ledger_epoch, frozen.expected_client,
                    'prepared', None, stamp, stamp))
        return self._public(self._row(db, *scope, binding_id))

    def get(self, project_id, task_id, run_id, binding_id):
        with self.store.connection() as db:
            return self._public(self._row(db, project_id, task_id, run_id, binding_id))

    def submission_snapshot(self, project_id, task_id, run_id, binding_id):
        """Private snapshot for an explicit original-request status operation.

        Does not grant permission to resend; never includes scoped credentials.
        """
        with self.store.connection() as db:
            row = self._row(db, project_id, task_id, run_id, binding_id)
            frozen = self._frozen(row)
            return frozen, validate_receipt(row['receipt_json'].encode('utf-8'), frozen)['receipt'] if row['receipt_json'] else None

    def mark_submit_intent(self, project_id, task_id, run_id, binding_id):
        with self.tasks.operation_lock, self.store.lock, self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, project_id, task_id, run_id, binding_id)
            self._scope(db, project_id, task_id, run_id, True)
            self._frozen(row)
            if row['local_state'] != 'prepared':
                raise UserError('原请求已有提交意图；请核对，不能自动重复派单。', 409)
            db.execute("UPDATE ai_hub_bindings_candidate SET local_state='submitting',updated=? WHERE id=?", (now(), binding_id))
            return self._public(self._row(db, project_id, task_id, run_id, binding_id))

    def mark_unknown(self, project_id, task_id, run_id, binding_id):
        with self.store.lock, self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, project_id, task_id, run_id, binding_id)
            if row['local_state'] not in ('submitting', 'unknown'):
                raise UserError('只可将未确定的原提交标为未知。', 409)
            db.execute("UPDATE ai_hub_bindings_candidate SET local_state='unknown',updated=? WHERE id=?", (now(), binding_id))
            return self._public(self._row(db, project_id, task_id, run_id, binding_id))

    def recover_interrupted(self):
        """Explicit startup after previous process stopped, no network effects."""
        with self.store.lock, self.store.connection() as db:
            return db.execute("UPDATE ai_hub_bindings_candidate SET local_state='unknown',updated=? WHERE local_state='submitting'", (now(),)).rowcount

    def observe(self, project_id, task_id, run_id, binding_id, raw, *, known_secret=''):
        if type(raw) is not bytes:
            raise ContractError('receipt_bytes_required')
        with self.store.lock, self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, project_id, task_id, run_id, binding_id)
            if row['local_state'] == 'prepared':
                raise UserError('必须先明确保存提交意图。', 409)
            frozen = self._frozen(row)
            previous = validate_receipt(row['receipt_json'].encode('utf-8'), frozen, known_secret=known_secret)['receipt'] if row['receipt_json'] else None
            validate_receipt(raw, frozen, previous=previous, known_secret=known_secret)
            # Persist original validated bytes. Sanitized validation is rerun on
            # each public read; hashing cancel references twice would break equality.
            raw_text = raw.decode('utf-8')
            db.execute("UPDATE ai_hub_bindings_candidate SET local_state='observed',receipt_json=?,updated=? WHERE id=?", (raw_text, now(), binding_id))
            return self._public(self._row(db, project_id, task_id, run_id, binding_id))

    def _public(self, row):
        frozen = self._frozen(row)
        view = dict(frozen.public_binding(), binding_id=row['id'], project_id=row['project_id'], task_id=row['task_id'], run_id=row['run_id'],
                    call_id=row['call_id'], local_state=row['local_state'], preparation_only=row['local_state'] == 'prepared',
                    submitted=False, submission_intent_recorded=row['local_state'] != 'prepared',
                    installed_in_product=False, auto_received=False, auto_reviewed=False, native_verified=False)
        view.pop('submitted', None)
        view['remote_accept_observed'] = bool(row['receipt_json'])
        view['hub_accepted'] = bool(row['receipt_json'])
        view['provider_submission_observed'] = False
        view['evidence_source'] = None
        view['provider_state'] = None
        view['dispatch_state'] = None
        view['results'] = []
        view['results_manifest_sha256'] = None
        if row['receipt_json']:
            verified = validate_receipt(row['receipt_json'].encode('utf-8'), frozen)
            receipt = verified['receipt']
            view.update(execution_id=receipt['execution_id'], queue_task_id=receipt['queue_task_id'],
                        results_manifest_sha256=receipt['results_manifest_sha256'],
                        provider_state=receipt['provider_state'], dispatch_state=receipt['dispatch_state'],
                        cancel_requested=receipt['cancel_requested'], cancel_effect=verified['cancel_effect'],
                        evidence_source=receipt['evidence_source'])
            view['provider_submission_observed'] = (receipt['evidence_source'] == 'worker_report'
                and (receipt['provider_state'] in ('running', 'succeeded')
                     or receipt['outcome'].get('cancel_evidence', {}).get('kind') == 'native_terminal'))
            view['results'] = [{'remote_identity': entry['remote_identity'], 'kind': entry['description']['kind'],
                                'size_bytes': entry['description']['size_bytes'],
                                **({'sha256': entry['description']['sha256']} if 'sha256' in entry['description'] else {}),
                                'receipt_link_state': 'not_linked_by_this_adapter',
                                'review_link_state': 'not_linked_by_this_adapter'} for entry in verified['mapped_results']]
        return view
