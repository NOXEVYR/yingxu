"""Original Hub receipts with real temporary Store/task/binding; no HTTP."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import unittest
import uuid
from unittest.mock import patch

import test_ai_hub_calls as fixture
from yingxu.ai_hub_receipts import AIHubReceipts, MAX_LOCAL_BYTES
from yingxu.ai_providers import canonical
from yingxu.hub_contract import canonical as wire_canonical
from yingxu.store import UserError


class HubReceiptTests(unittest.TestCase):
    prepared = fixture.HubCallTests.prepared
    receipt = fixture.HubCallTests.receipt

    def setUp(self):
        fixture.HubCallTests.setUp(self)
        self.links = AIHubReceipts(self.store, self.tasks, self.service)
        self.network = patch('yingxu.hub_client.AIHubExecutionClient._request', side_effect=AssertionError('no HTTP'))
        self.network.start(); self.addCleanup(self.network.stop)

    def result(self, result_id='画面-é-中', data=b'synthetic result', declared=True, size=None):
        binding = self.prepared()
        receipt = self.receipt(binding)
        result = {'result_id': result_id, 'kind': 'document', 'media_type': 'text/plain; charset=utf-8',
                  'bytes': len(data) if size is None else size, 'locator': 'private-opaque-locator/result'}
        if declared: result['sha256'] = hashlib.sha256(data).hexdigest()
        receipt.update(provider_state='succeeded', dispatch_state='completed', provider_request_id='synthetic-native',
                       evidence_source='worker_report', results=[result],
                       results_manifest_sha256=hashlib.sha256(wire_canonical([result])).hexdigest())
        scope = (self.project['id'], self.task['id'], self.run['id'], binding['binding_id'])
        self.service.bindings.mark_submit_intent(*scope)
        self.service.bindings.observe(*scope, wire_canonical(receipt))
        path = Path(self.run['directories']['generated']['path']) / 'result.txt'
        path.write_bytes(data)
        body = {'idempotency_key': 'original-association', 'result_id': result_id,
                'results_manifest_sha256': receipt['results_manifest_sha256'], 'relative_path': 'result.txt'}
        return binding, receipt, body, path

    def receive(self, binding, body):
        return self.links.receive(self.task['id'], self.run['id'], binding['binding_id'], copy.deepcopy(body))

    def detail(self): return self.tasks.get_task(self.task['id'])

    def counts(self):
        with self.store.connection() as db:
            return tuple(db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                         for table in ('ai_hub_receipts', 'ai_receipts', 'ai_artifacts'))

    def conflict(self, function, status=409):
        with self.assertRaises(UserError) as caught: function()
        self.assertEqual(caught.exception.status, status)

    def freeze_next(self):
        task = self.tasks.get_task(self.task['id'])
        return self.tasks.freeze_run(task['id'], {'expected_revision': task['revision'], 'client_id': 'synthetic',
                                                'conversation_id': 'next'})

    def interrupted(self):
        binding, receipt, body, path = self.result()
        with patch.object(self.links, '_commit', side_effect=RuntimeError('synthetic gap')):
            with self.assertRaises(RuntimeError): self.receive(binding, body)
        selected = self.links.list(self.task['id'], self.run['id'], binding['binding_id'])['items'][0]
        return binding, receipt, body, path, selected

    def test_receive_replay_one_pending_review_artifact(self):
        binding, receipt, body, path = self.result()
        value = self.receive(binding, body)
        self.assertEqual(value, self.receive(binding, body)); self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(value['state'], 'completed'); self.assertEqual(value['execution_id'], receipt['execution_id'])
        self.assertEqual(value['source_verification'], 'matches_declared_sha256'); self.assertFalse(value['automatic_review'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT review_status FROM ai_artifacts').fetchone()[0], 'pending')

    def test_list_get_refs_hide_locator_and_keep_exact_identity(self):
        binding, receipt, body, path = self.result(result_id='画面-e\u0301-中')
        value = self.receive(binding, body)
        listing = self.links.list(self.task['id'], self.run['id'], binding['binding_id'])
        self.assertEqual(listing['results'][0]['result_id'], body['result_id'])
        self.assertEqual(listing['items'], [self.links.get(self.task['id'], self.run['id'], binding['binding_id'], value['id'])])
        serialized = json.dumps(listing, ensure_ascii=False)
        for private in ('locator', str(self.root), self.secret): self.assertNotIn(private, serialized)
        with self.store.connection() as db:
            self.assertNotIn('private-opaque-locator', str(dict(db.execute('SELECT * FROM ai_hub_receipts').fetchone())))

    def test_nfc_normalization_is_not_identity(self):
        binding, receipt, body, path = self.result(result_id='e\u0301')
        self.conflict(lambda: self.receive(binding, {**body, 'result_id': 'é'}))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_escaped_invalid_utf8_text_is_bounded_error_without_intent(self):
        binding, receipt, body, path = self.result()
        for field in ('result_id', 'relative_path', 'role'):
            self.conflict(lambda: self.receive(binding, {**body, field: '\ud800'}), 400)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_prepared_list_is_empty_without_request(self):
        binding = self.prepared()
        self.assertEqual(self.links.list(self.task['id'], self.run['id'], binding['binding_id']),
                         {'items': [], 'results': [], 'results_manifest_sha256': None})

    def test_new_key_same_completed_result_keeps_original_association(self):
        binding, receipt, body, path = self.result()
        first = self.receive(binding, body)
        self.assertEqual(self.receive(binding, {**body, 'idempotency_key': 'different-key'}), first)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_same_key_changed_path_role_or_manifest_conflicts(self):
        binding, receipt, body, path = self.result(); self.receive(binding, body)
        for change in ({'relative_path': 'other.txt'}, {'role': 'other'}, {'results_manifest_sha256': 'f' * 64}):
            self.conflict(lambda: self.receive(binding, {**body, **change}))
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_wrong_manifest_selected_hash_size_and_missing_sha_have_no_intent(self):
        binding, receipt, body, path = self.result()
        self.conflict(lambda: self.receive(binding, {**body, 'results_manifest_sha256': 'f' * 64}))
        path.write_bytes(b'changed same size'[:len(path.read_bytes())])
        self.conflict(lambda: self.receive(binding, body)); self.assertEqual(self.counts(), (0, 0, 0))

    def test_wrong_declared_size_rejects_before_intent(self):
        binding, receipt, body, path = self.result(size=999)
        self.conflict(lambda: self.receive(binding, body)); self.assertEqual(self.counts(), (0, 0, 0))

    def test_sha_is_required_without_manual_override(self):
        binding, receipt, body, path = self.result(declared=False)
        self.conflict(lambda: self.receive(binding, body)); self.assertEqual(self.counts(), (0, 0, 0))
        self.conflict(lambda: self.receive(binding, {**body, 'confirmed_unverified_source': True}), 400)

    def test_remote_locator_cannot_be_used_as_result_identity(self):
        binding, receipt, body, path = self.result()
        self.conflict(lambda: self.receive(binding, {**body, 'result_id': receipt['results'][0]['locator']}))

    def test_nonterminal_or_tampered_success_frozen_receipt_rejects(self):
        binding = self.prepared(); receipt = self.receipt(binding)
        scope = (self.project['id'], self.task['id'], self.run['id'], binding['binding_id'])
        self.service.bindings.mark_submit_intent(*scope); self.service.bindings.observe(*scope, wire_canonical(receipt))
        body = {'idempotency_key': 'a', 'result_id': 'a', 'results_manifest_sha256': 'f' * 64, 'relative_path': 'a.txt'}
        self.conflict(lambda: self.receive(binding, body))
        with self.store.connection() as db:
            db.execute('UPDATE ai_hub_bindings_candidate SET receipt_json=? WHERE id=?', ('{}', binding['binding_id']))
        try:
            self.conflict(lambda: self.receive(binding, body)); self.assertEqual(self.counts(), (0, 0, 0))
        finally:
            with self.store.connection() as db:
                db.execute('UPDATE ai_hub_bindings_candidate SET receipt_json=? WHERE id=?', (wire_canonical(receipt).decode('utf-8'), binding['binding_id']))

    def test_cross_task_scope_checked_before_completed_replay(self):
        binding, receipt, body, path = self.result(); value = self.receive(binding, body)
        other = self.tasks.create_task({'project_id': self.project['id'], 'title': 'other', 'goal': 'isolate'})
        other_run = self.tasks.freeze_run(other['id'], {'expected_revision': other['revision'], 'client_id': 'synthetic', 'conversation_id': 'o'})
        for method in (lambda: self.links.receive(other['id'], other_run['id'], binding['binding_id'], body),
                       lambda: self.links.get(other['id'], other_run['id'], binding['binding_id'], value['id']),
                       lambda: self.links.resume(other['id'], other_run['id'], binding['binding_id'], value['id']),
                       lambda: self.links.list(other['id'], other_run['id'], binding['binding_id'])):
            self.conflict(method, 404)

    def test_old_run_new_association_rejected_completed_exact_replay_allowed(self):
        binding, receipt, body, path = self.result(); value = self.receive(binding, body)
        self.freeze_next()
        self.assertEqual(self.receive(binding, body), value)
        self.conflict(lambda: self.receive(binding, {**body, 'idempotency_key': 'new-key'}))
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_commit_gap_resume_original_receipt_after_next_round(self):
        binding, receipt, body, path, selected = self.interrupted(); self.freeze_next()
        value = self.links.resume(self.task['id'], self.run['id'], binding['binding_id'], selected['id'])
        self.assertEqual(value['id'], selected['id']); self.assertEqual(value['state'], 'completed')
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_commit_gap_reopened_service_exact_retry_without_file(self):
        binding, receipt, body, path, selected = self.interrupted()
        path.unlink(); self.links = AIHubReceipts(self.store, self.tasks, self.service)
        value = self.receive(binding, body)
        self.assertEqual(value['id'], selected['id']); self.assertEqual(self.counts(), (1, 1, 1))

    def test_intent_gap_before_original_receipt_old_run_cannot_resume(self):
        binding, receipt, body, path = self.result()
        with patch.object(self.tasks.receipts, 'receive', side_effect=RuntimeError('intent only')):
            with self.assertRaises(RuntimeError): self.receive(binding, body)
        row = self.links.list(self.task['id'], self.run['id'], binding['binding_id'])['items'][0]
        self.assertEqual(self.counts(), (1, 0, 0)); self.freeze_next()
        self.conflict(lambda: self.links.resume(self.task['id'], self.run['id'], binding['binding_id'], row['id']))
        self.assertEqual(self.counts(), (1, 0, 0))

    def test_interrupted_original_receipt_does_not_retry_hash_or_index(self):
        binding, receipt, body, path, selected = self.interrupted()
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM ai_receipts').fetchone(); value = json.loads(row['result_json'])
            value.update(state='interrupted', results=[])
            db.execute("UPDATE ai_receipts SET state='interrupted',result_json=? WHERE id=?", (canonical(value), row['id']))
        with patch('yingxu.ai_receipts.hash_file', side_effect=AssertionError('no retry')):
            value = self.links.resume(self.task['id'], self.run['id'], binding['binding_id'], selected['id'])
        self.assertEqual(value['state'], 'needs_attention'); self.assertEqual(value['receipt_state'], 'interrupted')
        self.assertNotIn('artifact_id', value); self.assertEqual(self.counts(), (1, 1, 1))

    def test_partial_original_receipt_fixed_public_notice_without_private_error(self):
        binding, receipt, body, path = self.result()
        with patch('yingxu.ai_receipts.hash_file', side_effect=UserError('private ' + str(self.root), 409)):
            value = self.receive(binding, body)
        self.assertEqual(value['state'], 'needs_attention'); self.assertEqual(value['receipt_state'], 'partial')
        self.assertNotIn(str(self.root), json.dumps(value)); self.assertEqual(self.counts(), (1, 1, 0))

    def test_missing_or_outside_relative_path_rejected(self):
        binding, receipt, body, path = self.result()
        for relative in ('missing.txt', '../result.txt', str(path), 'a\\result.txt', './result.txt'):
            with self.assertRaises(UserError): self.receive(binding, {**body, 'relative_path': relative})
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_private_file_is_not_associated(self):
        binding, receipt, body, path = self.result(); private = path.with_name('.env.txt'); private.write_bytes(path.read_bytes())
        self.conflict(lambda: self.receive(binding, {**body, 'relative_path': private.name}), 403)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_hardlink_is_not_associated(self):
        binding, receipt, body, path = self.result(); os.link(path, path.with_name('hard.txt'))
        self.conflict(lambda: self.receive(binding, body), 403); self.assertEqual(self.counts(), (0, 0, 0))

    def test_windows_junction_ancestor_is_not_associated(self):
        if os.name != 'nt': self.skipTest('Windows junction fixture')
        import _winapi
        binding, receipt, body, path = self.result(); target = self.root / 'synthetic-target'; target.mkdir()
        (target / 'linked.txt').write_bytes(path.read_bytes())
        link = path.parent / 'linked'; _winapi.CreateJunction(str(target), str(link))
        self.conflict(lambda: self.receive(binding, {**body, 'relative_path': 'linked/linked.txt'}), 400)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_declared_oversize_rejected_without_hash(self):
        binding, receipt, body, path = self.result(size=MAX_LOCAL_BYTES + 1)
        with patch('yingxu.ai_hub_receipts.hash_file', side_effect=AssertionError('no hash')):
            self.conflict(lambda: self.receive(binding, body), 413)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_hash_runs_outside_store_lock_and_detects_late_replacement(self):
        binding, receipt, body, path = self.result()
        from yingxu.ai_tasks import hash_file
        def changed(selected):
            self.assertFalse(self.store.lock._is_owned())
            value = hash_file(selected); selected.write_bytes(b'changed'); return value
        with patch('yingxu.ai_hub_receipts.hash_file', side_effect=changed):
            self.conflict(lambda: self.receive(binding, body))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_hardlink_added_after_hash_cannot_persist_intent(self):
        binding, receipt, body, path = self.result()
        from yingxu.ai_tasks import hash_file
        def linked(selected):
            value = hash_file(selected); os.link(selected, selected.with_name('late-hard.txt')); return value
        with patch('yingxu.ai_hub_receipts.hash_file', side_effect=linked):
            self.conflict(lambda: self.receive(binding, body))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_changed_frozen_manifest_during_hash_cannot_persist_intent(self):
        binding, receipt, body, path = self.result()
        from yingxu.ai_tasks import hash_file
        def changed(selected):
            value = hash_file(selected)
            different = copy.deepcopy(receipt); different['results'][0]['result_id'] = 'other'
            different['results_manifest_sha256'] = hashlib.sha256(wire_canonical(different['results'])).hexdigest()
            with self.store.connection() as db:
                db.execute('UPDATE ai_hub_bindings_candidate SET receipt_json=? WHERE id=?',
                           (wire_canonical(different).decode('utf-8'), binding['binding_id']))
            return value
        with patch('yingxu.ai_hub_receipts.hash_file', side_effect=changed):
            self.conflict(lambda: self.receive(binding, body))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_both_gates_closed_and_busy_block_new_receipts(self):
        binding, receipt, body, path = self.result()
        for gate in (self.migration, self.updates):
            gate.blocked = True; self.conflict(lambda: self.receive(binding, body)); gate.blocked = False
        with self.service.local_operation(): self.conflict(lambda: self.receive(binding, body))
        self.service.close(); self.conflict(lambda: self.receive(binding, body)); self.assertEqual(self.counts(), (0, 0, 0))

    def test_hash_inflight_visible_to_exit_and_gates_then_release(self):
        binding, receipt, body, path = self.result(); entered = threading.Event(); release = threading.Event(); outcomes = []
        from yingxu.ai_tasks import hash_file
        def paused(selected): entered.set(); release.wait(5); return hash_file(selected)
        def work():
            try: outcomes.append(self.receive(binding, body))
            except BaseException as exc: outcomes.append(exc)
        with patch('yingxu.ai_hub_receipts.hash_file', side_effect=paused):
            thread = threading.Thread(target=work); thread.start()
            try:
                self.assertTrue(entered.wait(5)); self.assertTrue(self.service.status()['busy'])
                self.assertEqual((self.migration.active, self.updates.active), (1, 1))
                self.conflict(lambda: self.receive(binding, body))
            finally: release.set(); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertIsInstance(outcomes[0], dict)
        self.assertFalse(self.service.status()['busy']); self.assertEqual((self.migration.active, self.updates.active), (0, 0))

    def test_forged_completed_state_requires_real_original_receipt(self):
        binding, receipt, body, path = self.result()
        with patch.object(self.tasks.receipts, 'receive', side_effect=RuntimeError('gap')):
            with self.assertRaises(RuntimeError): self.receive(binding, body)
        with self.store.connection() as db:
            row = db.execute('SELECT id FROM ai_hub_receipts').fetchone()
            db.execute("UPDATE ai_hub_receipts SET state='completed',response_json=?", ('{"token":"private"}',))
        self.conflict(lambda: self.links.get(self.task['id'], self.run['id'], binding['binding_id'], row['id']))

    def test_intent_trigger_and_integrity_block_row_tampering(self):
        binding, receipt, body, path = self.result(); value = self.receive(binding, body)
        with self.store.connection() as db:
            with self.assertRaises(sqlite3.IntegrityError): db.execute("UPDATE ai_hub_receipts SET result_id='other'")
            db.execute('DROP TRIGGER ai_hub_receipts_immutable'); db.execute("UPDATE ai_hub_receipts SET result_json='{}'")
        self.conflict(lambda: self.links.get(self.task['id'], self.run['id'], binding['binding_id'], value['id']))

    def test_arbitrary_response_json_is_not_public(self):
        binding, receipt, body, path = self.result(); value = self.receive(binding, body)
        with self.store.connection() as db: db.execute('UPDATE ai_hub_receipts SET response_json=?', (canonical({'token': self.secret, 'locator': str(self.root)}),))
        current = self.links.get(self.task['id'], self.run['id'], binding['binding_id'], value['id'])
        self.assertEqual(current, value); self.assertNotIn(self.secret, json.dumps(current))

    def test_artifact_tamper_rejected_after_receipt_gap(self):
        binding, receipt, body, path, selected = self.interrupted()
        with self.store.connection() as db: db.execute("UPDATE ai_artifacts SET observed_sha256=?", ('f' * 64,))
        self.conflict(lambda: self.links.resume(self.task['id'], self.run['id'], binding['binding_id'], selected['id']))
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_malformed_original_receipt_row_is_fixed_error_without_replay(self):
        binding, receipt, body, path, selected = self.interrupted()
        with self.store.connection() as db: original = db.execute('SELECT result_json FROM ai_receipts').fetchone()[0]
        try:
            for corrupted in ('{', 'null', '{"state":"completed","results":null}'):
                with self.store.connection() as db: db.execute('UPDATE ai_receipts SET result_json=?', (corrupted,))
                self.conflict(lambda: self.links.resume(self.task['id'], self.run['id'], binding['binding_id'], selected['id']))
                self.assertEqual(self.counts(), (1, 1, 1))
        finally:
            with self.store.connection() as db: db.execute('UPDATE ai_receipts SET result_json=?', (original,))

    def test_initialize_twice_does_not_change_old_tables_or_state(self):
        binding, receipt, body, path = self.result(); value = self.receive(binding, body)
        with self.store.connection() as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM ai_hub_bindings_candidate')]
        reopened = AIHubReceipts(self.store, self.tasks, self.service)
        self.assertEqual(reopened.get(self.task['id'], self.run['id'], binding['binding_id'], value['id']), value)
        with self.store.connection() as db: self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM ai_hub_bindings_candidate')], before)

    def test_native_item_path_tamper_cannot_move_original_association(self):
        binding, receipt, body, path, selected = self.interrupted()
        with self.store.connection() as db:
            db.execute('UPDATE items SET path=? WHERE id=(SELECT item_id FROM ai_artifacts)', (str(path.with_name('other.txt')),))
        self.conflict(lambda: self.links.resume(self.task['id'], self.run['id'], binding['binding_id'], selected['id']))

    def test_size_boundary_exactly_512mib_declaration_reaches_hash(self):
        binding, receipt, body, path = self.result(size=MAX_LOCAL_BYTES)
        with patch('yingxu.ai_hub_receipts.hash_file', side_effect=UserError('synthetic bounded hash', 409)) as hashed:
            self.conflict(lambda: self.receive(binding, body))
        self.assertEqual(hashed.call_count, 1); self.assertEqual(self.counts(), (0, 0, 0))

    def test_original_manifest_covers_unselected_result_and_order(self):
        binding, receipt, body, path = self.result()
        original = copy.deepcopy(receipt)
        second = {**receipt['results'][0], 'result_id': '第二成果'}
        receipt['results'].append(second)
        receipt['results_manifest_sha256'] = hashlib.sha256(wire_canonical(receipt['results'])).hexdigest()
        with self.store.connection() as db:
            db.execute('UPDATE ai_hub_bindings_candidate SET receipt_json=? WHERE id=?',
                       (wire_canonical(receipt).decode('utf-8'), binding['binding_id']))
        self.conflict(lambda: self.receive(binding, body))
        correct = {**body, 'results_manifest_sha256': receipt['results_manifest_sha256']}
        value = self.receive(binding, correct)
        receipt['results'].reverse(); receipt['results_manifest_sha256'] = hashlib.sha256(wire_canonical(receipt['results'])).hexdigest()
        with self.store.connection() as db:
            db.execute('UPDATE ai_hub_bindings_candidate SET receipt_json=? WHERE id=?',
                       (wire_canonical(receipt).decode('utf-8'), binding['binding_id']))
        self.conflict(lambda: self.links.get(self.task['id'], self.run['id'], binding['binding_id'], value['id']))

    def fill_capacity(self, count, other_run=False):
        # Synthetic insertions occupy capacity even if corrupt history would need manual review.
        with self.store.connection() as db:
            row = dict(db.execute('SELECT * FROM ai_hub_receipts').fetchone())
            columns = list(row)
            values = []
            for index in range(count):
                clone = {**row, 'id': uuid.uuid4().hex, 'idempotency_key': 'synthetic-' + str(index),
                         'state': 'needs_attention'}
                if other_run: clone['run_id'] = self.run['id']
                values.append(tuple(clone[k] for k in columns))
            db.executemany('INSERT INTO ai_hub_receipts(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', values)

    def test_per_run_200_capacity_blocks_new_intent(self):
        binding, receipt, body, path = self.result()
        with patch('yingxu.ai_receipts.hash_file', side_effect=UserError('synthetic partial', 409)): self.receive(binding, body)
        self.fill_capacity(199)
        self.conflict(lambda: self.receive(binding, {**body, 'idempotency_key': 'new-explicit'}))
        self.assertEqual(self.counts()[0], 200)

    def test_global_5000_capacity_blocks_new_intent(self):
        binding, receipt, body, path = self.result()
        with patch('yingxu.ai_receipts.hash_file', side_effect=UserError('synthetic partial', 409)): self.receive(binding, body)
        self.fill_capacity(4999)
        self.conflict(lambda: self.receive(binding, {**body, 'idempotency_key': 'new-explicit'}))
        self.assertEqual(self.counts()[0], 5000)


if __name__ == '__main__': unittest.main()
