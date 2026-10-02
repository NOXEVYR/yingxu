import copy
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_ai_tasks_http as fixtures
from aihub_mock_http import HubFixture, encode
from server import Handler
from yingxu.aihub_interop import AIHubProvider, validate_describe, validate_snapshot
from yingxu.ai_providers import ProviderError
from yingxu.store import UserError


class InteropValidationTests(unittest.TestCase):
    def setUp(self):
        self.hub = HubFixture()
        self.addCleanup(self.hub.close)

    def validate(self, snapshot):
        # Escaped surrogate JSON is valid wire UTF-8 but invalid declaration text.
        raw = json.dumps(snapshot, ensure_ascii=True).encode('utf-8')
        return validate_snapshot(raw, self.hub.url, self.hub.describe, self.hub.capability_id)

    def test_chinese_bytes_and_original_text_preserved(self):
        value = self.hub.snapshot()
        self.assertGreater(value['declaration']['bytes'], len(value['declaration']['text']))
        self.assertEqual(self.validate(value), value)

    def test_bytes_hash_parsed_and_binding_mismatches_refused(self):
        original = self.hub.snapshot()
        changes = [('bytes', 1), ('sha256', 'f' * 64), ('parsed', {'key': 'fixture.video'})]
        for field, value in changes:
            with self.subTest(field=field):
                bad = copy.deepcopy(original)
                bad['declaration'][field] = value
                with self.assertRaises(UserError): self.validate(bad)
        bad = copy.deepcopy(original)
        bad['identity']['service_instance_id'] = 'changed'
        with self.assertRaises(UserError): self.validate(bad)

    def test_snapshot_id_alone_does_not_identify_instance(self):
        old = self.hub.snapshot()
        self.hub.describe['identity']['service_instance_id'] = 'new-instance'
        new = self.hub.snapshot()
        self.assertEqual(old['snapshot_id'], new['snapshot_id'])
        with self.assertRaises(UserError):
            validate_snapshot(encode(new), self.hub.url, {**self.hub.describe, 'identity': old['identity']}, self.hub.capability_id)

    def test_declared_execution_and_heartbeat_never_make_runnable(self):
        provider = AIHubProvider({'base_url': self.hub.url})
        with self.assertRaises(ProviderError): provider.call('submit', {})
        with self.assertRaises(ProviderError): provider._request('POST', '/jobs', {})
        with self.assertRaises(ProviderError): provider._request('GET', '/api/capabilities')
        value = self.hub.snapshot()
        value['execution']['direct_execution'] = True
        with self.assertRaises(UserError): self.validate(value)
        self.assertEqual(self.hub.requests, [])

    def test_unknown_duplicate_secret_and_malformed_fields_refused(self):
        original = self.hub.snapshot()
        cases = []
        bad = copy.deepcopy(original); bad['unexpected'] = 'extra'; cases.append(bad)
        bad = copy.deepcopy(original); bad['evidence']['declaration'] = []; cases.append(bad)
        bad = copy.deepcopy(original); bad['declaration']['text'] = '\ud800'; cases.append(bad)
        bad = copy.deepcopy(original); bad['declaration']['parsed']['password'] = 'secret'; cases.append(bad)
        bad = copy.deepcopy(original); bad['declaration']['parsed']['number'] = float('inf'); cases.append(bad)
        for value in cases:
            with self.subTest(case=cases.index(value)), self.assertRaises(UserError): self.validate(value)
        with self.assertRaises(UserError):
            validate_describe(b'{"protocol":"x","protocol":"y"}', self.hub.url)
        description = copy.deepcopy(self.hub.describe); description['operations']['read_only'] = [{}]
        with self.assertRaises(UserError): validate_describe(encode(description), self.hub.url)

    def test_128k_response_accepted_then_one_byte_over_refused(self):
        self.hub.pad = 131072 - len(encode(self.hub.snapshot()))
        provider = AIHubProvider({'base_url': self.hub.url})
        _value, raw = provider.snapshot(self.hub.describe, self.hub.capability_id)
        self.assertEqual(len(raw), 131072)
        self.hub.pad += 1
        with self.assertRaises(ProviderError):
            AIHubProvider({'base_url': self.hub.url}).snapshot(self.hub.describe, self.hub.capability_id)

    def test_chinese_declaration_over_32k_refused(self):
        self.hub.declaration['description'] = '文' * 11000
        with self.assertRaises(UserError): self.validate(self.hub.snapshot())

    def test_expected_digest_prevents_silent_republish(self):
        value = self.hub.snapshot()
        with self.assertRaises(UserError):
            validate_snapshot(encode(value), self.hub.url, self.hub.describe, self.hub.capability_id, 'f' * 64)

    def test_unregistered_harness_is_a_valid_non_executable_declaration(self):
        for status in ('registration_unavailable', 'not_explicitly_registered'):
            value = self.hub.snapshot()
            value['harness'] = {'status': status, 'id': 'codex', 'enabled': None, 'revision': None,
                                'connection_mode': None, 'origin': 'legacy_usage'}
            self.assertEqual(self.validate(value)['harness']['status'], status)
            value['harness']['enabled'] = True
            with self.assertRaises(UserError): self.validate(value)

    def test_chinese_and_internal_spaces_in_client_id_are_supported(self):
        value = self.hub.snapshot()
        value['selected']['client_id'] = '映序 测试端'
        self.assertEqual(self.validate(value)['selected']['client_id'], '映序 测试端')
        value['selected']['client_id'] = '../other'
        with self.assertRaises(UserError): self.validate(value)

    def test_crlf_declaration_keeps_exact_text_and_digest(self):
        self.hub.declaration['description'] = '第一行\r\n第二行'
        original = self.hub.snapshot()
        value = self.validate(original)
        self.assertEqual(value['declaration']['text'], original['declaration']['text'])
        self.assertEqual(value['declaration']['sha256'], original['declaration']['sha256'])
        self.assertEqual(value['declaration']['parsed']['description'], '第一行\r\n第二行')

    def test_non_json_and_redirect_responses_are_not_followed(self):
        self.hub.bad_snapshot = lambda _: b'not json'
        with self.assertRaises(UserError):
            AIHubProvider({'base_url': self.hub.url}).snapshot(self.hub.describe, self.hub.capability_id)
        self.hub.bad_snapshot = None
        self.hub.snapshot_status = 302
        before = len(self.hub.requests)
        with self.assertRaises(ProviderError):
            AIHubProvider({'base_url': self.hub.url}).snapshot(self.hub.describe, self.hub.capability_id)
        self.assertEqual(len(self.hub.requests), before + 1)


class InteropHTTPTests(unittest.TestCase):
    _base_setup = fixtures.AITaskHttpTests.setUp
    _base_teardown = fixtures.AITaskHttpTests.tearDown
    request = fixtures.AITaskHttpTests.request
    ok = fixtures.AITaskHttpTests.ok
    task = fixtures.AITaskHttpTests.task
    freeze = fixtures.AITaskHttpTests.freeze

    def setUp(self):
        log_patch = patch.object(Handler, 'log_message', lambda *_args: None)
        log_patch.start(); self.addCleanup(log_patch.stop)
        self._base_setup()
        self.hub = HubFixture()

    def tearDown(self):
        try: self._base_teardown()
        finally: self.hub.close()

    def prepared(self):
        task = self.task(); run = self.freeze(task)
        connection = self.ok('/api/ai-connections', {'name': '合成曜核', 'provider': 'aihub-interop/1',
                                                   'base_url': self.hub.url, 'expected_revision': 0})
        self.assertEqual(self.hub.requests, [])
        connection = self.ok('/api/ai-connections/' + connection['id'] + '/check', {})
        prefix = '/api/ai-tasks/' + task['id'] + '/runs/' + run['id'] + '/capability-selections'
        body = {'connection_id': connection['id'], 'connection_revision': connection['revision'],
                'source_connection_revision': connection['connection_revision'],
                'capability_id': self.hub.capability_id, 'idempotency_key': 'fixed-selection'}
        return task, run, connection, prefix, body

    def test_saved_selection_restart_and_read_do_not_probe_or_execute(self):
        task, run, connection, prefix, body = self.prepared()
        selected = self.ok(prefix, body)
        self.assertFalse(selected['execution_allowed'])
        self.assertEqual(selected['snapshot']['declaration']['text'], self.hub.snapshot()['declaration']['text'])
        reads = len(self.hub.requests)
        self.assertEqual(self.ok(prefix)['total'], 1)
        self.assertEqual(self.ok(prefix + '/' + selected['id'])['snapshot'], selected['snapshot'])
        self.assertEqual(self.ok(prefix, body)['id'], selected['id'])
        from yingxu.ai_capability_selections import AICapabilitySelections
        reopened = AICapabilitySelections(self.app.store, self.app.ai_tasks, self.app.ai_connections)
        self.assertEqual(reopened.get(task['id'], run['id'], selected['id'])['snapshot'], selected['snapshot'])
        self.assertEqual(len(self.hub.requests), reads)
        self.assertTrue(all(method == 'GET' for method, _ in self.hub.requests))
        self.assertEqual(self.ok(prefix.replace('/capability-selections', '/calls'))['total'], 0)

    def test_backend_rejects_direct_submit_on_hub_provider(self):
        _task, _run, connection, prefix, _body = self.prepared()
        status, _ = self.request(prefix.replace('/capability-selections', '/calls'),
                                 {'connection_id': connection['id'], 'operation_id': 'capability_dispatch',
                                  'parameters': {}, 'idempotency_key': 'no-execution'})
        self.assertEqual(status, 409)
        self.assertEqual(len(self.hub.requests), 1)
        self.assertEqual(connection['operations'], [])

    def test_republish_verify_conflict_never_updates_original(self):
        _task, _run, _connection, prefix, body = self.prepared()
        selected = self.ok(prefix, body)
        self.assertTrue(self.ok(prefix + '/' + selected['id'] + '/verify', {})['declaration_matches'])
        self.hub.declaration['name'] = '新版声明'
        self.assertEqual(self.request(prefix + '/' + selected['id'] + '/verify', {})[0], 409)
        self.assertEqual(self.ok(prefix + '/' + selected['id'])['snapshot'], selected['snapshot'])
        changed = {**body, 'capability_id': 'different'}
        self.assertEqual(self.request(prefix, changed)[0], 409)

    def test_restart_and_workspace_change_refuse_old_selection(self):
        _task, _run, _connection, prefix, body = self.prepared()
        selected = self.ok(prefix, body)
        self.hub.describe['identity']['service_instance_id'] = 'restarted-service'
        self.assertEqual(self.request(prefix + '/' + selected['id'] + '/verify', {})[0], 409)
        self.assertEqual(self.request(prefix, {**body, 'idempotency_key': 'new-selection'})[0], 409)
        self.assertEqual(self.ok(prefix + '/' + selected['id'])['snapshot'], selected['snapshot'])
        self.hub.describe['identity']['service_instance_id'] = 'a' * 32
        self.hub.describe['workspace']['binding_revision'] = 'f' * 64
        self.assertEqual(self.request(prefix + '/' + selected['id'] + '/verify', {})[0], 409)

    def test_private_auth_wrong_run_strict_bodies_and_pagination(self):
        _task, _run, _connection, prefix, body = self.prepared()
        selected = self.ok(prefix, body)
        self.assertEqual(self.request(prefix, authorized=False)[0], 403)
        self.assertEqual(self.request(prefix, body, authorized=False)[0], 403)
        self.ok('/api/mcp/configure', {'enabled': True, 'project_id': self.project['id']})
        bearer = self.ok('/api/mcp/connection', {})['config']['mcpServers']['yingxu']['headers']['Authorization']
        self.assertEqual(self.request(prefix, authorized=False, bearer=bearer)[0], 403)
        self.assertEqual(self.request(prefix, body, authorized=False, bearer=bearer)[0], 403)
        other_task = self.task(); other_run = self.freeze(other_task)
        other = '/api/ai-tasks/' + other_task['id'] + '/runs/' + other_run['id'] + '/capability-selections'
        self.assertEqual(self.request(other + '/' + selected['id'])[0], 404)
        self.assertEqual(self.request(prefix + '?limit=999999999999999999999')[0], 400)
        self.assertEqual(self.request(prefix + '/' + selected['id'] + '/verify', {'extra': 1})[0], 400)
        self.assertEqual(self.request(prefix + '?unexpected=1')[0], 400)

    def test_configuration_change_in_flight_prevents_save_and_verify(self):
        _task, _run, connection, prefix, body = self.prepared()
        def change_connection():
            self.app.ai_connections.put({'id': connection['id'], 'name': 'changed', 'provider': 'aihub-interop/1',
                                         'base_url': self.hub.url, 'expected_revision': 1})
            self.hub.before_snapshot = None
        self.hub.before_snapshot = change_connection
        self.assertEqual(self.request(prefix, body)[0], 409)
        self.assertEqual(self.ok(prefix)['total'], 0)

    def test_migration_and_remote_errors_keep_old_data(self):
        _task, _run, _connection, prefix, body = self.prepared()
        with self.app.migration_jobs.lock:
            previous = self.app.migration_jobs.active
            self.app.migration_jobs.active = 'synthetic-migration'
        try: self.assertEqual(self.request(prefix, body)[0], 409)
        finally:
            with self.app.migration_jobs.lock: self.app.migration_jobs.active = previous
        self.hub.snapshot_status = 404
        self.assertEqual(self.request(prefix, body)[0], 404)
        self.assertEqual(self.ok(prefix)['total'], 0)

    def test_immutability_and_same_key_retry_with_disabled_connection(self):
        _task, _run, connection, prefix, body = self.prepared()
        selected = self.ok(prefix, body)
        with self.app.store.connection() as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute('UPDATE ai_capability_selections SET snapshot_raw=? WHERE id=?', ('{}', selected['id']))
        self.app.ai_connections.put({'id': connection['id'], 'name': '合成曜核', 'provider': 'aihub-interop/1',
                                     'base_url': self.hub.url, 'enabled': False, 'expected_revision': 1})
        reads = len(self.hub.requests)
        self.assertEqual(self.ok(prefix, body)['id'], selected['id'])
        self.assertEqual(len(self.hub.requests), reads)
        self.assertEqual(self.request(prefix + '/' + selected['id'] + '/verify', {})[0], 409)

    def test_run_capacity_refuses_more_records_and_keeps_all_existing(self):
        _task, _run, _connection, prefix, body = self.prepared()
        original = self.ok(prefix, body)
        for n in range(1, 24):
            self.ok(prefix, {**body, 'idempotency_key': 'selection-' + str(n)})
        self.assertEqual(self.request(prefix, {**body, 'idempotency_key': 'selection-over'})[0], 409)
        self.assertEqual(self.ok(prefix)['total'], 24)
        self.assertEqual(self.ok(prefix + '/' + original['id'])['snapshot'], original['snapshot'])

    def test_verified_connection_change_is_not_reported_as_matching(self):
        _task, _run, connection, prefix, body = self.prepared()
        selected = self.ok(prefix, body)
        def change_connection():
            self.app.ai_connections.put({'id': connection['id'], 'name': 'changed', 'provider': 'aihub-interop/1',
                                         'base_url': self.hub.url, 'expected_revision': 1})
            self.hub.before_snapshot = None
        self.hub.before_snapshot = change_connection
        self.assertEqual(self.request(prefix + '/' + selected['id'] + '/verify', {})[0], 409)
        self.assertEqual(self.ok(prefix + '/' + selected['id'])['snapshot'], selected['snapshot'])


if __name__ == '__main__': unittest.main()
