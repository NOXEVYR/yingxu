"""Only synthetic temporary Store/grants; describe is mocked, no network."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

CANDIDATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANDIDATE))
from yingxu.store import Store, UserError
from yingxu.ai_hub_sources import HubSourceStore, SourceDescriptorProvider, MAX_GRANT_BYTES


class HubSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yingxu-source-test-')
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'private', self.root / 'projects')
        self.sources = HubSourceStore(self.store)
        self.token = 'SyntheticSourceSecret' + 'a' * 22
        assert len(self.token) == 43
        self.path = self.root / 'source.json'
        self.grant = {'schema': 'ai-hub-execution-grant/1', 'protocol': 'aihub-execution/1',
                      'grant_id': str(uuid.uuid4()), 'role': 'source',
                      'subject': self.sources.identity()['source_authority'], 'token': self.token,
                      'workspace_root': str(self.root / 'hub-work'), 'workspace_binding_revision': 'a' * 64,
                      'execution_authority_id': str(uuid.uuid4()), 'ledger_epoch': str(uuid.uuid4()),
                      'connection': {'scheme': 'http', 'host': '127.0.0.1', 'port': 28791, 'app': 'ai-hub',
                                     'install_root': str(self.root / 'hub-install'), 'service_instance_id': str(uuid.uuid4()),
                                     'control_protocol': 'ai-hub-local-control-v1', 'connection_revision': 'b' * 64}}
        self.write()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, grant=None):
        self.path.write_text(json.dumps(grant or self.grant, ensure_ascii=False), encoding='utf-8')

    def put(self, **changes):
        return self.sources.put({'name': '合成来源', 'grant_path': str(self.path), 'expected_revision': 0, **changes})

    def descriptor(self):
        g, c = self.grant, self.grant['connection']
        return {'protocol': 'aihub-execution/1', 'schema_version': 1,
                'identity': {'port': c['port'], 'service_instance_id': c['service_instance_id'],
                             'control_protocol': c['control_protocol'], 'server_version': '2.13.9',
                             'install_root': c['install_root'], 'app': 'ai-hub', 'status': 'available'},
                'workspace': {'status': 'available', 'root': g['workspace_root'],
                              'binding_revision': g['workspace_binding_revision'],
                              'revision_scope': 'local_path_and_directory_identity',
                              'full_configuration_revision': False, 'cross_machine_uuid': False},
                'workspace_root': g['workspace_root'], 'connection_revision': c['connection_revision'],
                'execution_authority_id': g['execution_authority_id'], 'ledger_epoch': g['ledger_epoch'],
                'operations': ['submit', 'status'], 'routing': 'declared_client',
                'accepted_transaction': 'one_collaboration_database_commit',
                'provider_execution': 'scoped_worker_adapter', 'native_cancel': False,
                'input_digest': 'sha256_exact_input_json_utf8', 'input_bytes': 16000}

    def mock_check(self, source, value=None, callback=None):
        value = value or self.descriptor()
        def describe(_provider):
            if callback:
                callback()
            return value, json.dumps(value, ensure_ascii=False).encode()
        with patch.object(SourceDescriptorProvider, 'describe', describe):
            return self.sources.check(source['id'])

    def assert_empty(self):
        self.assertEqual(self.sources.list()['items'], [])

    def test_identity_survives_restart_and_initialization_is_repeatable(self):
        identity = self.sources.identity()
        self.assertEqual(HubSourceStore(self.store).identity(), identity)
        self.assertEqual(HubSourceStore(Store(self.root / 'private', self.root / 'projects')).identity(), identity)

    def test_put_is_offline_and_resolve_keeps_secret_transient(self):
        with patch.object(SourceDescriptorProvider, 'describe', side_effect=AssertionError('no HTTP')):
            source = self.put()
            row, token = self.sources.resolve(source['id'], source['revision'])
        self.assertEqual(token, self.token)
        self.assertEqual(row['subject'], self.grant['subject'])
        self.assertEqual(row['authority'], self.grant['execution_authority_id'])
        self.assertFalse(source['checked'])

    def test_hex_service_instance_is_preserved_through_check_and_restart(self):
        instance = 'a1111111111141118111111111111111'
        self.grant['connection']['service_instance_id'] = instance
        self.write()
        source = self.mock_check(self.put())
        reopened = HubSourceStore(Store(self.root / 'private', self.root / 'projects'))
        row, _ = reopened.resolve(source['id'], source['revision'])
        self.assertEqual(row['connection']['service_instance_id'], instance)
        self.assertEqual(reopened.checked_descriptor(source['id'], source['revision'])
                         ['identity']['service_instance_id'], instance)

    def test_invalid_service_instance_shapes_rejected_without_write(self):
        for instance in [True, 32, None, '', 'a' * 31, 'a' * 33, 'A' * 32,
                         'g' * 32, ' ' + 'a' * 32, 'a' * 32 + '\n',
                         'A1111111-1111-4111-8111-111111111111',
                         '{a1111111-1111-4111-8111-111111111111}']:
            with self.subTest(instance=instance):
                grant = copy.deepcopy(self.grant)
                grant['connection']['service_instance_id'] = instance
                self.write(grant)
                with self.assertRaises(UserError):
                    self.put()
                self.assert_empty()

    def test_other_grant_ids_still_require_canonical_uuid(self):
        for field in ['grant_id', 'execution_authority_id', 'ledger_epoch']:
            with self.subTest(field=field):
                grant = copy.deepcopy(self.grant)
                grant[field] = uuid.UUID(grant[field]).hex
                self.write(grant)
                with self.assertRaises(UserError):
                    self.put()
                self.assert_empty()

    def test_service_instance_uuid_equivalence_does_not_relax_exact_binding(self):
        instance = 'a1111111111141118111111111111111'
        self.grant['connection']['service_instance_id'] = instance
        self.write()
        source = self.put()
        descriptor = self.descriptor()
        descriptor['identity']['service_instance_id'] = str(uuid.UUID(instance))
        with self.assertRaises(UserError):
            self.mock_check(source, descriptor)
        self.assertFalse(self.sources.get(source['id'])['checked'])

    def test_secret_and_private_paths_never_enter_public_or_sqlite(self):
        source = self.put()
        self.mock_check(source)
        public = json.dumps([self.sources.identity(), self.sources.list(), self.sources.get(source['id'])])
        self.assertNotIn(self.token, public)
        self.assertNotIn(str(self.path), public)
        self.assertNotIn(self.grant['workspace_root'], public)
        with self.store.connection() as db:
            dump = '\n'.join(db.iterdump())
        self.assertNotIn(self.token, dump)
        self.assertNotIn('"token"', dump)

    def test_source_read_stays_read_only_even_after_check(self):
        self.grant['role'] = 'source_read'
        self.write()
        source = self.mock_check(self.put())
        self.assertTrue(source['read_only'])
        self.assertFalse(source['execution_allowed'])
        self.assertEqual(self.sources.resolve(source['id'])[0]['role'], 'source_read')

    def test_owner_worker_and_wrong_subject_rejected(self):
        for field, value in [('role', 'owner'), ('role', 'worker'), ('subject', str(uuid.uuid4()))]:
            with self.subTest(field=field, value=value):
                grant = copy.deepcopy(self.grant)
                grant[field] = value
                self.write(grant)
                with self.assertRaises(UserError):
                    self.put()
                self.assert_empty()

    def test_control_token_record_is_not_a_grant(self):
        self.path.write_text(json.dumps({'token': self.token, 'port': 28791, 'instance_id': str(uuid.uuid4())}))
        with self.assertRaises(UserError):
            self.put()
        self.assert_empty()

    def test_disabled_and_revision_mismatch_refuse_resolve(self):
        source = self.put()
        for revision in [True, 0, 2, '1']:
            with self.subTest(revision=revision), self.assertRaises(UserError):
                self.sources.resolve(source['id'], revision)
        self.put(id=source['id'], expected_revision=1, enabled=False)
        with self.assertRaises(UserError):
            self.sources.resolve(source['id'])

    def test_cas_update_clears_old_descriptor(self):
        source = self.mock_check(self.put())
        self.assertTrue(source['checked'])
        with self.assertRaises(UserError):
            self.put(id=source['id'])
        updated = self.put(id=source['id'], expected_revision=1, name='新名称')
        self.assertEqual(updated['revision'], 2)
        self.assertFalse(updated['checked'])
        with self.assertRaises(UserError):
            self.sources.checked_descriptor(source['id'], 2)

    def test_current_descriptor_is_cached_without_network(self):
        source = self.mock_check(self.put())
        with patch.object(SourceDescriptorProvider, 'describe', side_effect=AssertionError('no probe')):
            self.assertEqual(self.sources.checked_descriptor(source['id'], 1), self.descriptor())
        reopened = HubSourceStore(self.store)
        self.assertEqual(reopened.checked_descriptor(source['id'], 1), self.descriptor())

    def test_unchecked_descriptor_is_not_implicitly_probed(self):
        source = self.put()
        with patch.object(SourceDescriptorProvider, 'describe', side_effect=AssertionError('no probe')):
            with self.assertRaises(UserError):
                self.sources.checked_descriptor(source['id'])

    def test_metadata_or_only_token_change_refuses_original_reference(self):
        source = self.put()
        for field, value in [('ledger_epoch', str(uuid.uuid4())), ('token', 'b' * 43)]:
            grant = copy.deepcopy(self.grant)
            grant[field] = value
            self.write(grant)
            with self.subTest(field=field), self.assertRaises(UserError):
                self.sources.resolve(source['id'])

    def test_reencoded_identical_grant_requires_explicit_revision(self):
        source = self.put()
        self.path.write_text(json.dumps(self.grant, indent=2), encoding='utf-8')
        with self.assertRaises(UserError):
            self.sources.resolve(source['id'])
        self.assertEqual(self.put(id=source['id'], expected_revision=1)['revision'], 2)

    def test_descriptor_scope_and_authority_mismatches_never_cache(self):
        source = self.put()
        changes = [('execution_authority_id', str(uuid.uuid4())), ('ledger_epoch', str(uuid.uuid4())),
                   ('workspace_root', str(self.root / 'other')), ('connection_revision', 'c' * 64),
                   ('routing', 'target_tool_not_declared_client'), ('input_bytes', True)]
        for field, value in changes:
            d = self.descriptor()
            d[field] = value
            with self.subTest(field=field), self.assertRaises(UserError):
                self.mock_check(source, d)
            self.assertFalse(self.sources.get(source['id'])['checked'])
        for field in ['service_instance_id', 'install_root', 'port']:
            d = self.descriptor()
            d['identity'][field] = str(uuid.uuid4()) if field == 'service_instance_id' else ('/other' if field == 'install_root' else 30000)
            with self.subTest(field=field), self.assertRaises(UserError):
                self.mock_check(source, d)

    def test_descriptor_echoing_secret_is_rejected_before_sql_write(self):
        source = self.put()
        d = self.descriptor()
        d['limitations'] = [self.token]
        with self.assertRaises(UserError) as raised:
            self.mock_check(source, d)
        self.assertNotIn(self.token, str(raised.exception))
        with self.store.connection() as db:
            self.assertNotIn(self.token, '\n'.join(db.iterdump()))

    def test_descriptor_secret_as_key_is_rejected_even_with_json_escapes(self):
        source = self.put()
        d = self.descriptor()
        d[self.token] = 'masked'
        raw = json.dumps(d).replace(self.token, '\\u0053' + self.token[1:]).encode()
        with patch.object(SourceDescriptorProvider, 'describe', return_value=(d, raw)):
            with self.assertRaises(UserError):
                self.sources.check(source['id'])
        self.assertFalse(self.sources.get(source['id'])['checked'])

    def test_configuration_race_during_check_refuses_cache(self):
        source = self.put()
        def update():
            HubSourceStore(self.store).put({'id': source['id'], 'name': '新版', 'grant_path': str(self.path), 'expected_revision': 1})
        with self.assertRaises(UserError):
            self.mock_check(source, callback=update)
        self.assertEqual(self.sources.get(source['id'])['revision'], 2)
        self.assertFalse(self.sources.get(source['id'])['checked'])

    def test_file_change_during_network_refuses_cache(self):
        source = self.put()
        def update():
            self.path.write_bytes(b'{}')
        with self.assertRaises(UserError):
            self.mock_check(source, callback=update)
        self.assertFalse(self.sources.get(source['id'])['checked'])

    def test_unknown_provider_error_has_fixed_message(self):
        source = self.put()
        with patch.object(SourceDescriptorProvider, 'describe', side_effect=RuntimeError(self.token + str(self.path))):
            with self.assertRaises(UserError) as raised:
                self.sources.check(source['id'])
        self.assertNotIn(self.token, str(raised.exception))
        self.assertNotIn(str(self.path), str(raised.exception))

    def test_unknown_storage_error_has_fixed_public_message(self):
        with patch.object(self.store, 'connection', side_effect=RuntimeError(self.token + str(self.path))):
            for operation in [self.sources.identity, self.sources.list]:
                with self.subTest(operation=operation.__name__), self.assertRaises(UserError) as raised:
                    operation()
                self.assertNotIn(self.token, str(raised.exception))
                self.assertNotIn(str(self.path), str(raised.exception))

    def test_descriptor_provider_has_one_whitelisted_get(self):
        p = SourceDescriptorProvider({'base_url': 'http://127.0.0.1:28791'}, self.token)
        for method, suffix, body in [('POST', '/api/execution/describe', None), ('GET', '/api/control', None),
                                     ('GET', '/api/execution/describe', {})]:
            with self.subTest(method=method, suffix=suffix), self.assertRaises(UserError):
                p._request(method, suffix, body)

    def test_duplicate_nonfinite_and_invalid_json_rejected(self):
        for raw in [b'{"schema":1,"schema":2}', b'{"n":NaN}', b'{"n":Infinity}', b'\xff', b'{}']:
            self.path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(UserError):
                self.put()
        self.assert_empty()

    def test_size_and_empty_file_bounds(self):
        for raw in [b'', b'x' * (MAX_GRANT_BYTES + 1)]:
            self.path.write_bytes(raw)
            with self.subTest(size=len(raw)), self.assertRaises(UserError):
                self.put()
        self.write()
        self.path.write_bytes(self.path.read_bytes() + b' ' * (MAX_GRANT_BYTES - self.path.stat().st_size))
        self.assertEqual(self.path.stat().st_size, MAX_GRANT_BYTES)
        self.put()

    def test_strict_grant_types_and_extra_keys(self):
        for field, value in [('token', 'x' * 42), ('grant_id', True), ('workspace_binding_revision', 'A' * 64),
                             ('workspace_root', 'relative'), ('schema', 'wrong'), ('extra', 'unexpected')]:
            g = copy.deepcopy(self.grant)
            g[field] = value
            self.write(g)
            with self.subTest(field=field), self.assertRaises(UserError):
                self.put()
        for field, value in [('port', True), ('host', 'localhost'), ('scheme', 'https'), ('extra', 'x')]:
            g = copy.deepcopy(self.grant)
            g['connection'][field] = value
            self.write(g)
            with self.subTest(field=field), self.assertRaises(UserError):
                self.put()

    def test_put_body_bool_int_and_fields_are_strict(self):
        for body in [{'enabled': 1}, {'expected_revision': True}, {'expected_revision': -1},
                     {'name': ''}, {'name': ' ' * 81 + 'a'}, {'id': ''}, {'owner_token': self.token}]:
            with self.subTest(body=body), self.assertRaises(UserError):
                self.put(**body)
        self.assert_empty()

    def test_source_limit_sixteen_and_updates_still_allowed(self):
        items = [self.put(name=f'来源{i}') for i in range(16)]
        with self.assertRaises(UserError):
            self.put()
        self.put(id=items[0]['id'], expected_revision=1)
        self.assertEqual(len(self.sources.list()['items']), 16)

    def test_concurrent_cas_has_one_winner(self):
        source = self.put()
        outcomes = []
        barrier = threading.Barrier(2)
        def update():
            other = HubSourceStore(self.store)
            barrier.wait()
            try:
                other.put({'id': source['id'], 'name': '并发', 'grant_path': str(self.path), 'expected_revision': 1})
                outcomes.append('ok')
            except UserError:
                outcomes.append('conflict')
        threads = [threading.Thread(target=update) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertCountEqual(outcomes, ['ok', 'conflict'])

    def test_hard_link_file_rejected(self):
        alias = self.root / 'hard.json'
        os.link(self.path, alias)
        with self.assertRaises(UserError):
            self.put()
        self.assert_empty()

    def test_reparse_file_and_ancestor_attributes_rejected(self):
        import yingxu.ai_hub_sources as module
        original = Path.lstat
        def fake(path):
            real = original(path)
            if path == self.path:
                class Reparse:
                    st_mode = real.st_mode
                    st_file_attributes = 1024
                return Reparse()
            return real
        with patch.object(Path, 'lstat', fake), self.assertRaises(UserError):
            self.put()
        def fake_parent(path):
            real = original(path)
            if path == self.path.parent:
                class Reparse:
                    st_mode = real.st_mode
                    st_file_attributes = 1024
                return Reparse()
            return real
        with patch.object(Path, 'lstat', fake_parent), self.assertRaises(UserError):
            self.put()

    def test_relative_unc_ads_and_device_paths_rejected_before_open(self):
        for path in ['source.json', r'\\server\share\grant.json', str(self.path) + ':stream',
                     r'\\?\C:\grant.json', str(self.root) + '/../source.json']:
            with self.subTest(path=path), self.assertRaises(UserError):
                self.put(grant_path=path)

    def test_file_identity_change_between_lstat_and_open_rejected(self):
        original_open = os.open
        def racing(path, flags, *args, **kwargs):
            if Path(path) == self.path:
                self.path.write_bytes(b'{}')
            return original_open(path, flags, *args, **kwargs)
        with patch('yingxu.ai_hub_sources.os.open', racing), self.assertRaises(UserError):
            self.put()

    def test_rewritten_grant_and_windows_creation_time_are_compatible(self):
        from types import SimpleNamespace
        import yingxu.ai_hub_sources as module
        a = SimpleNamespace(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=9,
                            st_ctime_ns=4, st_birthtime_ns=4)
        b = SimpleNamespace(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=9,
                            st_ctime_ns=9, st_birthtime_ns=4)
        with patch.object(module.os, 'name', 'nt'):
            self.assertEqual(module._stamp(a), module._stamp(b))
            b.st_mtime_ns = 10
            self.assertNotEqual(module._stamp(a), module._stamp(b))
            b.st_mtime_ns = 9
            b.st_birthtime_ns = 5
            self.assertNotEqual(module._stamp(a), module._stamp(b))
        with patch.object(module.os, 'name', 'posix'):
            self.assertNotEqual(module._stamp(a), module._stamp(b))
        # Real filesystem rewrite, not only mocked stat values.
        self.path.write_bytes(b'old placeholder')
        self.write()
        self.put()

    def test_unrelated_sibling_creation_during_open_is_allowed(self):
        original_open = os.open
        def open_with_sibling(path, flags, *args, **kwargs):
            if Path(path) == self.path:
                (self.root / 'unrelated.txt').write_bytes(b'unrelated')
            return original_open(path, flags, *args, **kwargs)
        with patch('yingxu.ai_hub_sources.os.open', open_with_sibling):
            self.put()

    def test_ancestor_identity_change_after_open_is_rejected(self):
        from types import SimpleNamespace
        original = Path.lstat
        seen = 0
        def replace_identity(path):
            nonlocal seen
            st = original(path)
            if path == self.path.parent:
                seen += 1
                if seen > 1:
                    return SimpleNamespace(st_mode=st.st_mode,
                        st_file_attributes=getattr(st, 'st_file_attributes', 0),
                        st_dev=st.st_dev, st_ino=st.st_ino + 1)
            return st
        with patch.object(Path, 'lstat', replace_identity), self.assertRaises(UserError):
            self.put()
        self.assert_empty()

    def test_file_replacement_after_read_is_rejected(self):
        original_fdopen = os.fdopen
        path = self.path
        class ReplacingReader:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def fileno(self):
                return self.stream.fileno()
            def read(self, length):
                raw = self.stream.read(length)
                path.replace(path.with_suffix('.old'))
                path.write_bytes(raw)
                return raw
        with patch('yingxu.ai_hub_sources.os.fdopen', lambda fd, mode: ReplacingReader(original_fdopen(fd, mode))):
            with self.assertRaises(UserError):
                self.put()
        self.assert_empty()

    def test_annex_initialization_does_not_change_original_tables(self):
        with self.store.connection() as db:
            before = [tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'ai_hub_%' ORDER BY name")]
        HubSourceStore(self.store)
        self.put()
        with self.store.connection() as db:
            after = [tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'ai_hub_%' ORDER BY name")]
        self.assertEqual(before, after)

    def test_secret_disguised_in_metadata_or_name_rejected(self):
        g = copy.deepcopy(self.grant)
        g['workspace_root'] = str(self.root / self.token)
        self.write(g)
        with self.assertRaises(UserError):
            self.put()
        self.write()
        with self.assertRaises(UserError):
            self.put(name=self.token)
        self.assert_empty()


if __name__ == '__main__':
    unittest.main()
