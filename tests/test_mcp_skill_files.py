"""Synthetic fixed-reference acceptance; retain all fixtures as evidence."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yingxu.ai_tasks import AITaskService
from yingxu.mcp import ProjectMCP, RPCError, TOOLS, MAX_LIST_BYTES
from yingxu.mcp_skill_files import FixedSkillReader, bounded_read, file_name
from yingxu.skill_collections import _canonical
from yingxu.skills import SkillLibrary
from yingxu.store import Store, UserError


class SkillFileTests(unittest.TestCase):
    def setUp(self):
        evidence = Path(__file__).resolve().parents[1] / 'test-evidence' / 'mcp-skill-files'
        evidence.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='fixture-', dir=evidence)).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = patch.dict(os.environ, {k: str(self.home) for k in ('HOME', 'USERPROFILE', 'CODEX_HOME', 'APPDATA', 'LOCALAPPDATA')})
        self.env.start(); self.addCleanup(self.env.stop)
        self.source = self.home / '.codex/skills/synthetic'
        self.source.mkdir(parents=True)
        self.put('SKILL.md', b'---\nname: synthetic\n---\nentry version one')
        self.put('references/guide.md', b'guide version one\napi_key="synthetic-private-value"\nBearer synthetic-auth-value')
        self.put('scripts/no-run.py', b'raise AssertionError("MUST NEVER EXECUTE")')
        self.put('assets/preview.png', b'\x89PNG\0\1')
        self.put('config/local.json', b'{"password":"private-config"}')
        self.put('logs/session.txt', b'private-log')
        self.put('.env.example', b'example-private-env')
        self.put('client-config/connection.json', b'private-client')
        self.put('harness_config.json', b'private-harness')
        self.put('mcp-access.json', b'private-mcp')
        self.put('mcp-listener.json', b'private-mcp-listener')
        self.store = Store(self.root / 'data', self.root / 'projects')
        self.project = self.store.create_project('synthetic authorized')
        self.other = self.store.create_project('synthetic other')
        with patch('yingxu.skills.Path.home', return_value=self.home): self.skills = SkillLibrary(self.store)
        self.skill = self.skills.list()['skills'][0]
        self.tasks = AITaskService(self.store, self.skills)
        self.mcp = ProjectMCP(self.store, self.skills, self.tasks)
        self.mcp._project_id = self.project['id']
        self.saved = self.collect()
        self.skills.bind(self.project['id'], self.skill['id'], True)
        self.version_root = self.skills.collections._version_path(self.saved['id'], self.saved['version'])
        self.task = self.tasks.create_task({'project_id': self.project['id'], 'title': 'fixed round', 'goal': 'synthetic'})
        self.run = self.freeze()

    def put(self, name, data):
        p = self.source / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)

    def collect(self):
        p = self.skills.collections.preview(self.skill['id'])
        return self.skills.collections.collect(p['token'])

    def freeze(self):
        return self.tasks.freeze_run(self.task['id'], {'expected_revision': self.tasks.get_task(self.task['id'])['revision'],
            'client_id': 'test', 'conversation_id': 'synthetic',
            'skill_pins': [{'collection_id': self.saved['id'], 'version': self.saved['version']}]})

    def bound(self, **kw):
        return self.mcp._read_skill({'skill_id': self.skill['id'], **kw})

    def round(self, **kw):
        return self.mcp._read_run_skill({'task_id': self.task['id'], 'run_id': self.run['id'], 'collection_id': self.saved['id'], **kw})

    def manifest(self):
        return json.loads((self.version_root / 'manifest.json').read_text(encoding='utf-8'))

    def mutate_manifest(self, change, disk=True):
        m = self.manifest(); change(m); text = _canonical(m)
        with self.store.connection() as db:
            db.execute('UPDATE yx_skill_collection_versions SET manifest=? WHERE collection_id=? AND version=?', (text, self.saved['id'], self.saved['version']))
        if disk: (self.version_root / 'manifest.json').write_text(text, encoding='utf-8')

    def test_default_entry_and_seven_tools(self):
        self.assertEqual(len(TOOLS), 7)
        self.assertIn('entry version one', self.bound()['text'])
        self.assertIn('entry version one', self.round()['text'])
        self.assertIn('entry version one', self.tasks.read_run_skill(self.task['id'], self.run['id'], self.saved['id'])['content'])
        self.assertEqual(self.bound(file='skill.md')['file'], 'SKILL.md')

    def test_reference_redaction_before_pagination(self):
        result = self.bound(file='references/guide.md')
        self.assertIn('guide version one', result['text'])
        self.assertNotIn('synthetic-private-value', result['text'])
        self.assertNotIn('synthetic-auth-value', result['text'])
        chunks, offset = [], 0
        while True:
            part = self.round(file='references/guide.md', offset=offset, limit=7)
            chunks.append(part['text'])
            if 'next_offset' not in part: break
            offset = part['next_offset']
        self.assertEqual(''.join(chunks), result['text'])

    def test_files_pagination_privacy_and_no_disk_walk(self):
        with patch('os.scandir', side_effect=AssertionError('no scan')), patch.object(self.skills.collections, 'get', side_effect=AssertionError('no get')):
            first = self.bound(mode='files', limit=2)
            all_files, offset = [], 0
            while True:
                page = self.round(mode='files', limit=2, offset=offset)
                all_files.extend(page['files'])
                if 'next_offset' not in page: break
                offset = page['next_offset']
        self.assertEqual(len(first['files']), 2)
        names = [x['file'] for x in all_files]
        self.assertEqual(set(names), {'SKILL.md', 'references/guide.md', 'scripts/no-run.py', 'assets/preview.png'})
        self.assertLessEqual(len(json.dumps(first).encode()), MAX_LIST_BYTES)
        serialized = json.dumps(first)
        self.assertNotIn(str(self.source), serialized)
        self.assertNotIn('origin', serialized)
        self.assertTrue(all(e['content_verified'] is False for e in all_files))
        for name in ('config/local.json', 'logs/session.txt', '.env.example', 'client-config/connection.json', 'harness_config.json', 'mcp-access.json', 'mcp-listener.json'):
            with self.subTest(name=name), self.assertRaises(UserError): self.bound(file=name)

    def test_script_and_binary_metadata_without_execution(self):
        for name in ('scripts/no-run.py', 'assets/preview.png'):
            with self.subTest(name=name):
                result = self.round(file=name)
                self.assertFalse(result['content_available'])
                self.assertNotIn('text', result)

    def test_source_and_new_project_pin_cannot_change_frozen_round(self):
        self.put('references/guide.md', b'new mutable source')
        self.skills.refresh(); newer = self.collect()
        with self.store.connection() as db:
            db.execute('UPDATE yx_project_skills SET collection_version=? WHERE project_id=? AND skill_id=?', (newer['version'], self.project['id'], self.skill['id']))
        self.assertEqual(self.bound(file='references/guide.md')['text'], 'new mutable source')
        self.assertIn('guide version one', self.round(file='references/guide.md')['text'])
        self.assertEqual(self.round(file='references/guide.md')['version'], self.saved['version'])

    def test_other_project_run_and_unpinned_collection_refused(self):
        other = self.tasks.create_task({'project_id': self.other['id'], 'title': 'private other', 'goal': 'synthetic'})
        with self.assertRaises(UserError): self.mcp._read_run_skill({'task_id': other['id'], 'run_id': self.run['id'], 'collection_id': self.saved['id']})
        with self.assertRaises(UserError): self.mcp._read_run_skill({'task_id': self.task['id'], 'run_id': self.run['id'], 'collection_id': 'col_' + '0' * 32})
        self.mcp._project_id = self.other['id']
        with self.assertRaises(UserError): self.bound()

    def test_uncollected_legacy_entry_only_needs_collection(self):
        with self.store.connection() as db:
            db.execute("UPDATE yx_project_skills SET collection_id='',collection_version='' WHERE project_id=?", (self.project['id'],))
        self.assertIn('entry version one', self.bound()['text'])
        with patch.object(self.skills, '_trusted_path', side_effect=AssertionError('no live read')):
            self.assertTrue(self.bound(file='references/guide.md')['needs_collection'])
            self.assertTrue(self.bound(mode='files')['needs_collection'])

    def test_original_path_spelling_rejected(self):
        invalid = ['/absolute.md', 'C:/drive.md', '//host/share', '../escape.md', './SKILL.md', 'a//b.md',
                   'a\\b.md', 'a.md:stream', 'a/CON.md', 'COM0', 'a/LPT¹.txt', 'a/b. ', 'a/b.', 'a\x00b', 'a\x7fb', 'a\x85b', 'x' * 513]
        for name in invalid:
            with self.subTest(name=repr(name)), self.assertRaises(RPCError): self.bound(file=name)
        with self.assertRaises(UserError): self.bound(file='References/guide.md')
        with self.assertRaises(UserError): self.bound(file='missing.md')

    def test_bad_mode_and_page_limits(self):
        for options in ({'mode': 'all'}, {'mode': []}, {'mode': 'files', 'file': 'SKILL.md'}, {'mode': 'files', 'limit': 49}, {'limit': 16001}, {'offset': -1}, {'limit': True}):
            with self.subTest(options=options), self.assertRaises(RPCError): self.bound(**options)

    def test_selected_hash_tamper_rejected_but_unrelated_tamper_does_not_force_full_hash(self):
        p = self.version_root / 'files/references/guide.md'; p.write_bytes(b'changed')
        self.assertIn('entry version one', self.bound()['text'])
        self.assertTrue(self.bound(mode='files')['files'])
        with self.assertRaises(UserError): self.bound(file='references/guide.md')
        with self.assertRaises(UserError): self.round(file='references/guide.md')

    def test_disk_manifest_tamper_and_hardlink_rejected(self):
        target = self.version_root / 'manifest.json'
        target.write_text('{}', encoding='utf-8')
        with self.assertRaises(UserError): self.bound()

    def test_hardlink_selected_file_rejected(self):
        target = self.version_root / 'files/references/guide.md'
        os.link(target, self.root / 'shared.md')
        with self.assertRaises(UserError): self.round(file='references/guide.md')

    def test_hardlink_manifest_rejected(self):
        os.link(self.version_root / 'manifest.json', self.root / 'shared-manifest.json')
        with self.assertRaises(UserError): self.bound(mode='files')

    def test_manifest_schema_and_identity_rejected(self):
        self.mutate_manifest(lambda m: m.update(schema='wrong', id='col_' + '0' * 32))
        with self.assertRaises(UserError): self.bound()

    def test_manifest_noncanonical_path_rejected(self):
        self.mutate_manifest(lambda m: m['files'][0].update(path='a//b.md'))
        with self.assertRaises(UserError): self.bound(mode='files')

    def test_manifest_boolean_size_rejected(self):
        self.mutate_manifest(lambda m: m['files'][0].update(size=True))
        with self.assertRaises(UserError): self.bound()

    def test_manifest_casefold_collision_rejected(self):
        self.mutate_manifest(lambda m: m['files'].append(dict(m['files'][0], path=m['files'][0]['path'].upper())))
        with self.assertRaises(UserError): self.bound(mode='files')

    def test_manifest_database_path_and_package_hash_rejected(self):
        with self.store.connection() as db:
            db.execute('UPDATE yx_skill_collection_versions SET path=?,package_sha256=? WHERE collection_id=?', (str(self.source), '0' * 64, self.saved['id']))
        with self.assertRaises(UserError): self.bound()

    def test_frozen_manifest_digest_rejects_identity_preserving_change(self):
        self.mutate_manifest(lambda m: m.update(name='changed public title'))
        self.assertIn('entry version one', self.bound()['text'])
        with self.assertRaises(UserError): self.round()

    def test_read_only_no_get_scan_export_or_database_write(self):
        with self.store.connection() as db: before = '\n'.join(db.iterdump())
        with patch.object(self.skills, 'get', side_effect=AssertionError('no get')), patch.object(self.skills, 'refresh', side_effect=AssertionError('no refresh')), patch.object(self.skills.collections, '_verify_version', side_effect=AssertionError('no package hash')):
            self.bound(file='references/guide.md'); self.round(mode='files'); self.bound()
        with self.store.connection() as db: after = '\n'.join(db.iterdump())
        self.assertEqual(before, after)

    def test_nul_reference_rejected(self):
        self.put('references/null-text.txt', b'hello\0world')
        newer = self.collect()
        with self.store.connection() as db:
            db.execute('UPDATE yx_project_skills SET collection_version=? WHERE project_id=?', (newer['version'], self.project['id']))
        with self.assertRaises(UserError): self.bound(file='references/null-text.txt')

    def test_large_reference_has_list_metadata_but_no_body(self):
        self.put('references/large.txt', b'x' * (1024 * 1024 + 1)); newer = self.collect()
        with self.store.connection() as db:
            db.execute('UPDATE yx_project_skills SET collection_version=? WHERE project_id=?', (newer['version'], self.project['id']))
        entries = self.bound(mode='files')['files']
        self.assertFalse(next(e for e in entries if e['file'] == 'references/large.txt')['content_available'])
        with self.assertRaises(UserError): self.bound(file='references/large.txt')

    def test_read_race_rejected(self):
        target = self.version_root / 'files/references/guide.md'
        real_fstat = os.fstat
        called = 0
        def race(fd):
            nonlocal called
            called += 1
            if called == 2: target.write_bytes(b'raced change')
            return real_fstat(fd)
        with patch('yingxu.mcp_skill_files.os.fstat', side_effect=race), self.assertRaises(UserError): bounded_read(target)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction acceptance')
    def test_ancestor_junction_rejected(self):
        import _winapi
        source_dir = self.version_root / 'files/references'
        retained = self.version_root / 'files/retained-references'
        self.assertTrue(source_dir.resolve().is_relative_to(self.root))
        self.assertTrue(retained.absolute().is_relative_to(self.root))
        source_dir.rename(retained)
        _winapi.CreateJunction(str(retained), str(source_dir))
        with self.assertRaises(UserError): self.bound(file='references/guide.md')

    def test_none_skill_service_keeps_user_error_after_scope_validation(self):
        self.tasks.skills = None
        with self.assertRaises(UserError) as caught:
            self.tasks.read_run_skill(self.task['id'], self.run['id'], self.saved['id'])
        self.assertEqual(caught.exception.status, 409)

    def test_lowercase_entry_manifest_compatibility(self):
        entry = self.source / 'SKILL.md'
        temporary = self.source / 'temporary-entry.md'
        entry.rename(temporary); temporary.rename(self.source / 'skill.md')
        self.skills.refresh(); newer = self.collect()
        with self.store.connection() as db:
            db.execute('UPDATE yx_project_skills SET collection_version=? WHERE project_id=?', (newer['version'], self.project['id']))
        self.assertEqual(self.bound()['file'], 'skill.md')


if __name__ == '__main__': unittest.main()
