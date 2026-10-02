import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_call_mock_http import MockCallHTTP
from yingxu.ai_call_jobs import AICallService
from yingxu.ai_connections import AIConnectionStore
from yingxu.ai_providers import declaration, loopback_url, validate_input, validate_schema
from yingxu.ai_tasks import AITaskService
from yingxu.migration_jobs import MigrationJobs
from yingxu.store import Store, UserError, uid


class AICallKernelTests(unittest.TestCase):
    def setUp(self):
        # Retain synthetic evidence; never recursively clean user/workspace data.
        base = Path(os.environ.get('YINGXU_CALL_TEST_ROOT', Path(__file__).resolve().parents[1] / 'test-evidence' / 'ai-call-kernel')).resolve()
        self.root = base / uid()
        self.store = Store(self.root / 'data', self.root / 'projects')
        self.tasks = AITaskService(self.store)
        self.project = self.store.create_project('合成调用测试')
        self.task = self.tasks.create_task({'project_id': self.project['id'], 'title': '合成结果'})
        self.run = self.tasks.freeze_run(self.task['id'], {
            'goal': '仅合成测试', 'expected_revision': self.task['revision'], 'client_id': 'fixture', 'conversation_id': 'fixture-session'})
        app = SimpleNamespace(store=self.store, jobs=SimpleNamespace(lock=threading.RLock(), jobs={}),
                              context=SimpleNamespace(_export_lock=threading.RLock()), changed=lambda: None)
        self.migrations = MigrationJobs(app, SimpleNamespace(execute=lambda *_args, **_kwargs: {}))
        self.mock = MockCallHTTP()
        self.connections = AIConnectionStore(self.store)
        self.connection = self.connections.put({'name': '合成本机服务', 'base_url': self.mock.url, 'expected_revision': 0})
        self.connections.check(self.connection['id'])
        self.services = []
        self.calls = self.make_service()

    def tearDown(self):
        self.mock.release_submit.set()
        for service in self.services:
            service.close()
        self.mock.close()
        self.migrations.close()

    def make_service(self, **kwargs):
        kwargs.setdefault('query_window', 0)
        kwargs.setdefault('request_timeout', .5)
        service = AICallService(self.store, self.tasks, self.migrations, self.connections, **kwargs)
        self.services.append(service)
        return service

    def create(self, key='fixture-request', prompt='synthetic', **extra):
        body = {'connection_id': self.connection['id'], 'operation_id': 'describe',
                'parameters': {'prompt': prompt}, 'idempotency_key': key}
        body.update(extra)
        return self.calls.create(self.task['id'], self.run['id'], body)

    def get(self, attempt):
        return self.calls.get(self.task['id'], self.run['id'], attempt['id'])

    def wait(self, attempt, condition=lambda data: data['state'] not in ('accepted', 'submitting'), timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.get(attempt)
            if condition(data) and not self.calls.status()['local_action']:
                return data
            time.sleep(.01)
        self.fail('bounded attempt did not reach expected state')

    def test_short_acceptance_and_same_key_never_submits_twice(self):
        self.mock.block_submit = True
        started = time.monotonic()
        attempt = self.create()
        self.assertLess(time.monotonic() - started, .3)
        self.assertEqual(attempt['state'], 'accepted')
        self.assertTrue(self.mock.submit_entered.wait(1))
        self.assertEqual(self.create()['id'], attempt['id'])
        self.mock.release_submit.set()
        self.assertEqual(self.wait(attempt)['state'], 'running')
        self.assertEqual(self.mock.counts['submit'], 1)
        self.assertEqual(self.calls.list(self.task['id'], self.run['id'])['total'], 1)

    def test_changed_payload_same_key_conflicts(self):
        attempt = self.create()
        with self.assertRaises(UserError) as caught:
            self.create(prompt='changed')
        self.assertEqual(caught.exception.status, 409)
        self.wait(attempt)
        self.assertEqual(self.mock.counts['submit'], 1)

    def test_lost_submit_response_unknown_survives_restart_and_lookup(self):
        self.mock.drop_submit = True
        attempt = self.create()
        self.assertEqual(self.wait(attempt)['state'], 'unknown')
        self.assertEqual(self.mock.counts['submit'], 1)
        self.calls.close()
        self.calls = self.make_service()
        time.sleep(.1)
        self.assertEqual(self.mock.counts['lookup'], 0)
        self.assertEqual(self.create()['id'], attempt['id'])
        self.calls.query(self.task['id'], self.run['id'], attempt['id'])
        self.assertEqual(self.wait(attempt, lambda x: bool(x['job_id']))['state'], 'running')
        self.assertEqual(self.mock.counts['lookup'], 1)
        self.assertEqual(self.mock.counts['submit'], 1)

    def test_submitting_checkpoint_recovers_unknown_without_replay(self):
        attempt = self.create()
        self.wait(attempt)
        self.calls.close()
        self.calls.attempts.update(attempt['id'], state='submitting')
        self.calls = self.make_service()
        self.assertEqual(self.get(attempt)['state'], 'unknown')
        time.sleep(.1)
        self.assertEqual(self.mock.counts['submit'], 1)
        self.assertEqual(self.mock.counts['query'], 0)

    def test_connection_revision_replacement_rejects_old_job_query(self):
        attempt = self.create()
        data = self.wait(attempt)
        self.connections.put({'id': self.connection['id'], 'name': '改过的连接', 'base_url': self.mock.url,
                              'expected_revision': self.connection['revision']})
        with self.assertRaises(UserError) as caught:
            self.calls.query(self.task['id'], self.run['id'], attempt['id'])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.mock.counts['query'], 0)
        self.assertEqual(self.get(attempt)['connection_revision'], 1)
        self.assertEqual(self.get(attempt)['job_id'], data['job_id'])

    def test_changed_service_identity_does_not_query_foreign_job(self):
        attempt = self.create()
        self.wait(attempt)
        self.mock.service_id = 'different-service'
        self.calls.query(self.task['id'], self.run['id'], attempt['id'])
        data = self.wait(attempt, lambda x: x['error_code'] == 'service_changed')
        self.assertEqual(data['state'], 'running')
        self.assertEqual(self.mock.counts['query'], 0)

    def test_operation_change_before_submit_requires_new_attempt(self):
        self.mock.capabilities['operations'][0]['input_schema']['properties']['prompt']['maxLength'] = 2
        attempt = self.create()
        self.assertEqual(self.wait(attempt)['state'], 'not_submitted')
        self.assertEqual(self.mock.counts['submit'], 0)

    def test_cancel_requested_is_not_cancel_confirmed(self):
        attempt = self.create()
        self.wait(attempt)
        self.calls.cancel(self.task['id'], self.run['id'], attempt['id'])
        self.assertEqual(self.wait(attempt, lambda x: x['last_action'] == 'cancel')['state'], 'cancel_requested')
        self.assertEqual(self.mock.counts['cancel'], 1)
        self.mock.query_state = 'cancelled'
        self.calls.query(self.task['id'], self.run['id'], attempt['id'])
        self.assertEqual(self.wait(attempt)['state'], 'cancelled')

    def test_unsupported_cancel_and_lookup_are_honest(self):
        self.mock.capabilities['operations'][0]['supports'].update(cancel=False, lookup=False)
        self.connections.check(self.connection['id'])
        self.mock.drop_submit = True
        attempt = self.create()
        self.wait(attempt)
        for action in (self.calls.cancel, self.calls.query):
            with self.assertRaises(UserError):
                action(self.task['id'], self.run['id'], attempt['id'])
        self.assertEqual(self.get(attempt)['state'], 'unknown')
        self.assertEqual(self.mock.counts['cancel'], 0)
        self.assertEqual(self.mock.counts['lookup'], 0)

    def test_cancel_response_lost_remains_unconfirmed(self):
        attempt = self.create()
        self.wait(attempt)
        self.mock.drop_cancel = True
        self.calls.cancel(self.task['id'], self.run['id'], attempt['id'])
        data = self.wait(attempt, lambda x: bool(x['error_code']))
        self.assertEqual(data['state'], 'cancel_requested')
        self.assertIn('不能视为已经取消', data['notice'])

    def test_bounded_queue_and_bounded_close_never_cancel_remote(self):
        self.calls.close()
        self.calls = self.make_service(queue_limit=2, request_timeout=2)
        self.mock.block_submit = True
        first = self.create('first')
        self.assertTrue(self.mock.submit_entered.wait(1))
        second = self.create('second')
        with self.assertRaises(UserError) as caught:
            self.create('third')
        self.assertEqual(caught.exception.status, 429)
        self.assertGreaterEqual(self.calls.status()['local_queued'], 1)
        started = time.monotonic()
        result = self.calls.close(timeout=1)
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertTrue(result['worker_stopped'])
        self.assertFalse(result['local_busy'])
        self.assertEqual(self.mock.counts['cancel'], 0)
        self.assertEqual(self.mock.counts['submit'], 1)
        self.calls = self.make_service()
        self.assertEqual(self.get(first)['state'], 'unknown')
        self.assertEqual(self.get(second)['state'], 'not_submitted')

    def test_migration_blocks_acceptance_and_submit_writer_blocks_migration(self):
        with self.migrations.lock:
            self.migrations.active = 'synthetic-migration'
        with self.assertRaises(UserError) as caught:
            self.create()
        self.assertEqual(caught.exception.status, 409)
        with self.migrations.lock:
            self.migrations.active = None
        self.mock.block_submit = True
        attempt = self.create()
        self.assertTrue(self.mock.submit_entered.wait(1))
        self.assertTrue(self.calls.status()['local_busy'])
        with self.migrations.mutation('POST', '/api/project-storage/migration'):
            with self.assertRaises(UserError):
                self.migrations.submit({'token': 'synthetic-preview'})
        self.mock.release_submit.set()
        self.wait(attempt)
        self.assertEqual(self.migrations.writers, 0)
        self.assertFalse(self.calls.status()['local_busy'])
        self.assertEqual(self.calls.status()['local_queued'], 0)
        self.assertEqual(self.calls.status()['remote_pending'], 1)
        with self.migrations.mutation('POST', '/api/project-storage/migration'):
            self.assertEqual(self.migrations.writers, 1)

    def test_no_idle_queries_and_remote_query_queue_does_not_block_exit(self):
        before = dict(self.mock.counts)
        time.sleep(.12)
        self.assertEqual(self.mock.counts, before)
        self.calls.close()
        self.calls = self.make_service(query_window=.6, query_interval=.05)
        attempt = self.create()
        self.wait(attempt)
        state = self.calls.status()
        self.assertGreater(state['queued'], 0)
        self.assertEqual(state['local_queued'], 0)
        self.assertFalse(state['local_busy'])
        time.sleep(.8)
        self.assertGreater(self.mock.counts['query'], 0)
        queries = self.mock.counts['query']
        time.sleep(.2)
        self.assertEqual(self.mock.counts['query'], queries)
        self.assertEqual(self.get(attempt)['state'], 'running')
        self.assertEqual(self.calls.status()['local_queued'], 0)
        self.assertEqual(self.calls.status()['queued'], 0)

    def test_private_environment_value_not_in_public_or_persistent_records(self):
        value = 'fixture-auth-value-123456'
        with patch.dict(os.environ, {'YINGXU_FIXTURE_SECRET': value}):
            private = self.connections.put({'name': '合成带凭据服务', 'base_url': self.mock.url,
                                            'credential_env': 'YINGXU_FIXTURE_SECRET', 'expected_revision': 0})
            self.connection = private
            self.connections.check(private['id'])
            self.mock.submit_state = 'succeeded'
            self.mock.results = [{'name': '合成结果', 'url': 'http://127.0.0.1/private?token=' + value,
                                  'Authorization': 'Bearer ' + value, 'size_bytes': 10}]
            attempt = self.create()
            data = self.wait(attempt)
            output = json.dumps([self.connections.list(), data, self.calls.list(self.task['id'], self.run['id'])])
            self.assertNotIn(value, output)
            self.assertNotIn('?token=', output)
            self.assertFalse(data['results'][0]['downloaded'])
            self.assertEqual(data['results'][0]['verification'], 'declared_unverified')
            with self.assertRaises(UserError):
                self.create('secret-input', prompt=value)
        with self.store.connection() as db:
            dump = '\n'.join(db.iterdump())
        self.assertNotIn(value, dump)
        self.assertNotIn('?token=', dump)

    def test_sensitive_declaration_and_result_rejected_without_leak(self):
        self.mock.capabilities['operations'][0]['name'] = 'Bearer synthetic-secret-value'
        with self.assertRaises(UserError):
            self.connections.check(self.connection['id'])
        self.mock.capabilities['operations'][0]['name'] = '合成描述'
        self.mock.results = [{'name': 'Bearer synthetic-secret-value'}]
        attempt = self.create()
        data = self.wait(attempt)
        self.assertEqual(data['state'], 'unknown')
        self.assertNotIn('synthetic-secret-value', json.dumps(data))
        self.assertEqual(data['results'], [])

    def test_immutable_selection_and_original_run_unchanged(self):
        original = self.tasks.get_run(self.task['id'], self.run['id'])['input_digest']
        attempt = self.create()
        self.wait(attempt)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connection() as db:
                db.execute('UPDATE ai_call_attempts SET parameters_json=? WHERE id=?', ('{}', attempt['id']))
        self.assertEqual(self.get(attempt)['parameters'], {'prompt': 'synthetic'})
        self.assertEqual(self.tasks.get_run(self.task['id'], self.run['id'])['input_digest'], original)
        self.assertNotEqual(self.tasks.get_task(self.task['id'])['status'], '已完成')
        with self.assertRaises(UserError):
            self.calls.get(self.task['id'], 'a' * 32, attempt['id'])

    def test_invalid_inputs_and_unchecked_connection_never_send(self):
        for params in ({'prompt': 2}, {'prompt': 'x', 'extra': 1}, {}, {'prompt': 'x' * 201}, {'token': 'x'}):
            with self.subTest(params_type=list(params)):
                with self.assertRaises(UserError):
                    self.create(parameters=params)
        unchecked = self.connections.put({'name': '未检查', 'base_url': self.mock.url, 'expected_revision': 0})
        with self.assertRaises(UserError):
            self.create(connection_id=unchecked['id'])
        self.assertEqual(self.mock.counts['submit'], 0)

    def test_only_explicit_loopback_and_no_schema_refs(self):
        for url in ('http://example.org:8080', 'https://127.0.0.1:8080', 'http://127.0.0.1',
                    'http://127.0.0.1:8080/a/../b', 'http://127.0.0.1:8080?token=x', 'http://user:pass@127.0.0.1:8080'):
            with self.subTest(url=url):
                with self.assertRaises(UserError):
                    loopback_url(url)
        self.assertEqual(loopback_url('http://localhost:8080/call/'), 'http://127.0.0.1:8080/call')
        self.mock.capabilities['operations'][0]['input_schema']['$ref'] = 'http://example.org/schema'
        with self.assertRaises(UserError):
            declaration(json.dumps(self.mock.capabilities).encode())

    def test_post_200_201_202_validated_but_get_202_rejected(self):
        self.mock.submit_state = 'succeeded'
        for status in (200, 201, 202):
            self.mock.post_status = status
            attempt = self.create('post-' + str(status))
            self.assertEqual(self.wait(attempt)['state'], 'succeeded')
        self.assertEqual(self.mock.counts['submit'], 3)
        self.mock.get_status = 202
        with self.assertRaises(UserError):
            self.connections.check(self.connection['id'])

    def test_malformed_required_returns_user_error(self):
        for required in ([{}], [['prompt']], [1], ['unknown'], ['prompt', 'prompt'], {}):
            with self.subTest(shape=type(required).__name__):
                self.mock.capabilities['operations'][0]['input_schema']['required'] = required
                with self.assertRaises(UserError):
                    declaration(json.dumps(self.mock.capabilities).encode())

    def test_large_response_headers_count_toward_total_budget(self):
        self.mock.post_header_bytes = 80 * 1024
        attempt = self.create()
        data = self.wait(attempt)
        self.assertEqual(data['state'], 'unknown')
        self.assertEqual(data['error_code'], 'response_limit')
        self.assertEqual(self.mock.counts['submit'], 1)
        self.assertNotIn('z' * 4000, json.dumps(data))
        self.assertEqual(data['results'], [])
        self.mock.post_header_bytes = 0
        self.mock.cap_header_bytes = 80 * 1024
        with self.assertRaises(UserError):
            self.connections.check(self.connection['id'])

    def test_slow_header_drip_obeys_total_deadline(self):
        self.calls.close()
        self.calls = self.make_service(request_timeout=.2)
        self.mock.drip_capabilities = True
        started = time.monotonic()
        attempt = self.create()
        data = self.wait(attempt)
        self.assertLess(time.monotonic() - started, .8)
        self.assertEqual(data['state'], 'not_submitted')
        self.assertEqual(self.mock.counts['submit'], 0)

    def test_extreme_numeric_constraints_and_values_are_user_errors(self):
        for number in (10 ** 1000, -(10 ** 1000), float('inf'), float('nan'), True):
            with self.subTest(number_type=type(number).__name__):
                with self.assertRaises(UserError):
                    validate_schema({'type': 'integer', 'minimum': number})
        for number in (10 ** 1000, -(10 ** 1000), float('inf'), float('nan')):
            with self.assertRaises(UserError):
                validate_input({'type': 'number'}, number)
        validate_input({'type': 'integer'}, 2 ** 64)

    def test_extreme_offsets_are_user_errors(self):
        for offset in (2 ** 100, -1, 10001, True):
            with self.assertRaises(UserError):
                self.calls.list(self.task['id'], self.run['id'], offset=offset)
        self.assertEqual(self.calls.list(self.task['id'], self.run['id'], offset=10000)['items'], [])

    def test_query_does_not_replace_a_queued_submit_or_cancel(self):
        self.mock.block_submit = True
        first = self.create(key='queue-first')
        self.assertTrue(self.mock.submit_entered.wait(1))
        second = self.create(key='queue-second')
        with self.assertRaises(UserError) as caught:
            self.calls.query(self.task['id'], self.run['id'], second['id'])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.calls._pending[second['id']]['action'], 'submit')
        self.mock.release_submit.set()
        self.wait(first);self.wait(second)
        self.assertEqual(self.mock.counts['submit'], 2)
        self.assertEqual(self.mock.counts['lookup'], 0)
        with self.calls._condition:
            self.calls._pending[second['id']] = {'action':'cancel'}
            try:
                with self.assertRaises(UserError):
                    self.calls.query(self.task['id'], self.run['id'], second['id'])
                self.assertEqual(self.calls._pending[second['id']]['action'], 'cancel')
            finally:del self.calls._pending[second['id']]

    def test_atomic_execution_version_check_rejects_between_request_changes(self):
        self.mock.change_before_submit = True
        attempt = self.create()
        self.assertEqual(self.wait(attempt)['state'], 'unknown')
        self.assertEqual(self.mock.counts['submit'], 0)
        self.assertEqual(self.create()['id'], attempt['id'])
        self.assertEqual(self.mock.counts['submit'], 0)

    def test_missing_atomic_binding_or_wrong_execution_proof_never_claims_success(self):
        for binding in (None,'best_effort'):
            declared = dict(self.mock.capabilities)
            if binding is None:del declared['execution_binding']
            else:declared['execution_binding'] = binding
            with self.assertRaises(UserError):declaration(json.dumps(declared).encode())
        self.mock.bad_execution_hash = True
        attempt = self.create()
        value = self.wait(attempt)
        self.assertEqual(value['state'], 'unknown');self.assertEqual(value['results'], [])
        self.assertEqual(self.mock.counts['submit'], 1)
        self.assertEqual(self.mock.received[0]['expected_declaration_sha256'],value['declaration_sha256'])


if __name__ == '__main__':
    unittest.main()
