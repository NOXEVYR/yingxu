"""Synthetic workbench calls and explicit original-round receipt recovery; no real service."""
import hashlib
import json
import http.client
from pathlib import Path
import socket
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from test_ai_calls_http import AICallHTTPTests as _Fixture
from yingxu.ai_call_jobs import AICallService
from server import Handler


class AICallReceiptTests(unittest.TestCase):
    _base_setup = _Fixture._base_setup
    _base_teardown = _Fixture._base_teardown
    setUp = _Fixture.setUp
    tearDown = _Fixture.tearDown
    request = _Fixture.request
    ok = _Fixture.ok
    task = _Fixture.task
    freeze = _Fixture.freeze
    prepared = _Fixture.prepared
    wait = _Fixture.wait

    def prepare_result(self, declared=True, results=None):
        task, run, connection, prefix, body = self.prepared()
        media = b'synthetic local media; not generated or decoded'
        digest = hashlib.sha256(media).hexdigest()
        self.provider.results = results or [{'name': 'result.txt', 'resource_id': 'same-resource',
                                            'size_bytes': len(media), **({'sha256': digest} if declared else {})}]
        file = Path(run['directories']['generated']['path']) / 'result.txt'
        file.write_bytes(media)
        status, attempt = self.request(prefix, body)
        self.assertEqual(status, 202)
        attempt = self.wait(prefix, attempt, {'succeeded'})
        url = prefix + '/' + attempt['id'] + '/receipts'
        plan = {'idempotency_key': 'original-association', 'result_id': attempt['results'][0]['result_id'],
                'relative_path': 'result.txt'}
        return task, run, attempt, url, plan, file

    def test_same_key_replay_and_new_key_same_completed_result_keep_one_artifact(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        first = self.ok(url, plan)
        self.assertEqual(first['state'], 'completed')
        self.assertEqual(first['source_verification'], 'matches_declared_sha256')
        self.assertEqual(self.ok(url, plan), first)
        self.assertEqual(self.ok(url, {**plan, 'idempotency_key': 'new-explicit-key'})['id'], first['id'])
        detail = self.ok('/api/ai-tasks/' + task['id'])
        self.assertEqual(detail['receipts_total'], 1)
        self.assertEqual(detail['artifacts_total'], 1)
        self.assertEqual(detail['artifacts'][0]['review_status'], 'pending')
        self.assertEqual(self.provider.submissions, 1)
        self.assertEqual(self.ok(url)['items'][0]['id'], first['id'])

    def test_missing_declaration_hash_requires_explicit_manual_source_confirmation(self):
        task, run, attempt, url, plan, file = self.prepare_result(declared=False)
        self.assertEqual(self.request(url, plan)[0], 409)
        first = self.ok(url, {**plan, 'confirmed_unverified_source': True})
        self.assertEqual(first['source_verification'], 'manual_source_unverified')
        self.assertEqual(first['file_verification'], 'verified_at_receipt')
        self.assertFalse(first['automatic_review'])

    def test_same_key_changed_path_or_confirmation_cannot_reuse_receipt(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        self.ok(url, plan)
        for changed in ({'relative_path': 'other.txt'}, {'confirmed_unverified_source': True}, {'result_id': 'f' * 64}):
            self.assertEqual(self.request(url, {**plan, **changed})[0], 409)
        self.assertEqual(self.ok('/api/ai-tasks/' + task['id'])['receipts_total'], 1)

    def test_wrong_hash_or_size_creates_no_receipt_or_association(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        file.write_bytes(b'changed')
        self.assertEqual(self.request(url, plan)[0], 409)
        self.assertEqual(self.ok(url)['items'], [])
        self.assertEqual(self.ok('/api/ai-tasks/' + task['id'])['receipts_total'], 0)

    def test_changed_success_result_snapshot_rejects_old_reference(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        self.app.ai_calls.attempts.update(attempt['id'], results=[{'name': 'different.txt'}])
        self.assertEqual(self.request(url, plan)[0], 409)
        self.assertEqual(self.ok(url)['items'], [])

    def test_duplicate_resource_names_still_get_distinct_frozen_result_ids(self):
        media = b'synthetic local media; not generated or decoded'
        digest = hashlib.sha256(media).hexdigest()
        results = [{'name': 'same', 'resource_id': 'same', 'sha256': digest}] * 2
        task, run, attempt, url, plan, file = self.prepare_result(results=results)
        self.assertNotEqual(attempt['results'][0]['result_id'], attempt['results'][1]['result_id'])
        first = self.ok(url, plan)
        second = self.ok(url, {**plan, 'idempotency_key': 'second', 'result_id': attempt['results'][1]['result_id']})
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['artifact_id'], second['artifact_id'])

    def interrupt_after_receipt(self, url, plan):
        with patch.object(self.app.ai_calls.receipts, '_commit', side_effect=RuntimeError('synthetic commit interruption')):
            self.assertEqual(self.request(url, plan)[0], 500)
        item = self.ok(url)['items'][0]
        self.assertEqual(item['state'], 'pending')
        return item

    def test_receipt_success_commit_gap_can_resume_after_new_round(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        pending = self.interrupt_after_receipt(url, plan)
        self.freeze(task, goal='new original round')
        result = self.ok(url + '/' + pending['id'] + '/resume', {})
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['id'], pending['id'])
        self.assertEqual(self.ok('/api/ai-tasks/' + task['id'])['receipts_total'], 1)
        self.assertEqual(self.ok('/api/ai-tasks/' + task['id'])['artifacts_total'], 1)
        self.assertEqual(self.provider.submissions, 1)

    def test_receipt_gap_recovers_across_service_reconstruction_and_exact_retry(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        pending = self.interrupt_after_receipt(url, plan)
        self.app.ai_calls.close(3)
        self.app.ai_calls = AICallService(self.app.store, self.app.ai_tasks, self.app.migration_jobs, self.app.ai_connections)
        result = self.ok(url, plan)
        self.assertEqual(result['id'], pending['id'])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(self.ok('/api/ai-tasks/' + task['id'])['receipts_total'], 1)
        self.assertEqual(self.provider.submissions, 1)

    def test_fresh_old_round_association_is_rejected_but_completed_replay_works(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        first = self.ok(url, plan)
        self.freeze(task)
        self.assertEqual(self.ok(url, plan), first)
        self.assertEqual(self.request(url, {**plan, 'idempotency_key': 'new-old-round'})[0], 409)

    def test_paths_private_files_and_links_are_not_allowed(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        for relative in ('../result.txt', str(file), 'other\\result.txt'):
            self.assertIn(self.request(url, {**plan, 'relative_path': relative})[0], (400, 403))
        private = file.parent / 'secrets' / 'secret.md'
        private.parent.mkdir()
        private.write_bytes(file.read_bytes())
        self.assertEqual(self.request(url, {**plan, 'relative_path': 'secrets/secret.md'})[0], 403)
        self.assertEqual(self.ok(url)['items'], [])

    def test_existing_foreign_attempt_cannot_replay_or_resume_an_original_link(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        other = self.task(self.other)
        other_run = self.freeze(other)
        foreign_url = url.replace(task['id'], other['id']).replace(run['id'], other_run['id'])
        pending = self.interrupt_after_receipt(url, plan)
        self.assertEqual(self.request(foreign_url, plan)[0], 404)
        self.assertEqual(self.request(foreign_url + '/' + pending['id'] + '/resume', {})[0], 404)
        self.assertEqual(self.ok(url)['items'][0]['state'], 'pending')
        first = self.ok(url, plan)
        self.assertEqual(self.request(foreign_url, plan)[0], 404)
        self.assertEqual(self.ok(url, plan), first)
        self.assertEqual(self.ok('/api/ai-tasks/' + other['id'])['artifacts_total'], 0)

    def test_receipt_commit_gap_recovers_after_task_has_been_explicitly_completed(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        pending = self.interrupt_after_receipt(url, plan)
        detail = self.ok('/api/ai-tasks/' + task['id'])
        artifact = detail['artifacts'][0]
        self.ok('/api/ai-tasks/' + task['id'] + '/artifacts/' + artifact['id'] + '/review',
                {'expected_revision': artifact['revision'], 'decision': 'accepted'}, 'PATCH')
        detail = self.ok('/api/ai-tasks/' + task['id'])
        self.ok('/api/ai-tasks/' + task['id'] + '/complete', {'expected_revision': detail['revision'], 'confirmed': True})
        linked = self.ok(url, plan)
        self.assertEqual(linked['id'], pending['id'])
        self.assertEqual(linked['state'], 'completed')
        self.assertEqual(self.ok('/api/ai-tasks/' + task['id'])['status'], '已完成')

    def test_real_http_reply_loss_can_retry_original_key_without_second_receipt(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        original = Handler.json
        def lose_reply(handler, value, status=200):
            if handler.path == url and handler.command == 'POST':
                handler.close_connection = True
                handler.connection.shutdown(socket.SHUT_RDWR)
                handler.connection.close()
                return
            return original(handler, value, status)
        with patch.object(Handler, 'json', lose_reply):
            with self.assertRaises((http.client.RemoteDisconnected, ConnectionError, OSError)):
                self.request(url, plan)
        result = self.ok(url, plan)
        self.assertEqual(result['state'], 'completed')
        detail = self.ok('/api/ai-tasks/' + task['id'])
        self.assertEqual(detail['receipts_total'], 1)
        self.assertEqual(detail['artifacts_total'], 1)
        self.assertEqual(self.provider.submissions, 1)

    def test_concurrent_same_intent_keeps_one_receipt_and_artifact(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.request(url, plan), range(2)))
        self.assertEqual([status for status, _ in replies], [201, 201])
        self.assertEqual(replies[0][1]['id'], replies[1][1]['id'])
        detail = self.ok('/api/ai-tasks/' + task['id'])
        self.assertEqual(detail['receipts_total'], 1)
        self.assertEqual(detail['artifacts_total'], 1)

    def test_declared_size_mismatch_is_rejected_before_receipt(self):
        media = b'synthetic local media; not generated or decoded'
        task, run, attempt, url, plan, file = self.prepare_result(results=[
            {'name': 'result.txt', 'sha256': hashlib.sha256(media).hexdigest(), 'size_bytes': len(media) + 1}])
        self.assertEqual(self.request(url, plan)[0], 409)
        self.assertEqual(self.ok(url)['items'], [])

    def test_mcp_readonly_bearer_cannot_receive_or_list_associations(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        self.ok('/api/mcp/configure', {'enabled': True, 'project_id': self.project['id']})
        bearer = self.ok('/api/mcp/connection', {})['config']['mcpServers']['yingxu']['headers']['Authorization']
        self.assertEqual(self.request(url, plan, authorized=False, bearer=bearer)[0], 403)
        self.assertEqual(self.request(url, authorized=False, bearer=bearer)[0], 403)
        self.assertEqual(self.ok(url)['items'], [])

    def test_interruption_before_receipt_recovers_saved_intent_without_resubmission(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        with patch.object(self.app.ai_tasks.receipts, 'receive', side_effect=RuntimeError('synthetic pre-receipt interruption')):
            self.assertEqual(self.request(url, plan)[0], 500)
        pending = self.ok(url)['items'][0]
        self.assertEqual(pending['state'], 'pending')
        self.assertEqual(self.ok('/api/ai-tasks/' + task['id'])['receipts_total'], 0)
        linked = self.ok(url + '/' + pending['id'] + '/resume', {})
        self.assertEqual(linked['state'], 'completed')
        self.assertEqual(linked['id'], pending['id'])
        self.assertEqual(self.provider.submissions, 1)

    def test_partial_receipt_preserves_failure_and_requires_explicit_new_key(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        with patch.object(self.app.store, 'index_files', side_effect=OSError('synthetic index interruption')):
            failed = self.ok(url, plan)
        self.assertEqual(failed['state'], 'needs_attention')
        self.assertEqual(failed['receipt_state'], 'partial')
        self.assertNotIn('artifact_id', failed)
        self.assertEqual(self.ok(url, plan), failed)
        linked = self.ok(url, {**plan, 'idempotency_key': 'explicit-retry-after-failure'})
        self.assertEqual(linked['state'], 'completed')
        self.assertNotEqual(linked['id'], failed['id'])
        detail = self.ok('/api/ai-tasks/' + task['id'])
        self.assertEqual(detail['receipts_total'], 2)
        self.assertEqual(detail['artifacts_total'], 1)
        self.assertEqual(detail['artifacts'][0]['review_status'], 'pending')
        self.assertEqual(self.ok(url)['items'][0]['state'], 'needs_attention')
        self.assertEqual(self.provider.submissions, 1)

    def test_size_limit_rejects_before_hashing_without_large_fixture(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        with patch('yingxu.ai_call_receipts.MAX_LOCAL_BYTES', 1), patch('yingxu.ai_call_receipts.hash_file') as hash_mock:
            self.assertEqual(self.request(url, plan)[0], 413)
            hash_mock.assert_not_called()
        self.assertEqual(self.ok(url)['items'], [])

    def test_unsuccessful_call_cannot_associate_a_stale_result(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        self.app.ai_calls.attempts.update(attempt['id'], state='failed')
        self.assertEqual(self.request(url, plan)[0], 409)
        self.assertEqual(self.ok(url)['items'], [])

    def test_linked_file_is_rejected_where_symlink_privilege_is_available(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        link = file.parent / 'linked.txt'
        try:link.symlink_to(file)
        except OSError as exc:self.skipTest('Synthetic symlink privilege unavailable: ' + str(exc.winerror if hasattr(exc, 'winerror') else exc.errno))
        self.assertIn(self.request(url, {**plan, 'relative_path': 'linked.txt'})[0], (400, 403))
        self.assertEqual(self.ok(url)['items'], [])

    def test_private_auth_wrong_attempt_and_migration_gate(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        self.assertEqual(self.request(url, plan, authorized=False)[0], 403)
        self.assertEqual(self.request(url, authorized=False)[0], 403)
        self.app.migration_jobs.active = 'synthetic-migration'
        try:self.assertEqual(self.request(url, plan)[0], 409)
        finally:self.app.migration_jobs.active = None
        wrong = url.replace(attempt['id'], 'f' * 32)
        self.assertEqual(self.request(wrong, plan)[0], 404)
        self.assertEqual(self.provider.submissions, 1)

    def test_pending_intent_is_immutable_and_resume_accepts_only_empty_body(self):
        task, run, attempt, url, plan, file = self.prepare_result()
        pending = self.interrupt_after_receipt(url, plan)
        self.assertEqual(self.request(url + '/' + pending['id'] + '/resume', {'changed': True})[0], 400)
        with self.assertRaises(Exception):
            with self.app.store.connection() as db:
                db.execute('UPDATE ai_call_receipts SET result_id=? WHERE id=?', ('0' * 64, pending['id']))


del _Fixture
