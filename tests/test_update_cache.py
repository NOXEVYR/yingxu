"""Cleanup uses owned temporary caches, completed journals and real replacement."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest

from test_incremental_support import Fixture
from yingxu.incremental_install import apply_incremental, validate_plan, write_json
from yingxu.incremental_update import _json_bytes
from yingxu.store import UserError
from yingxu.update_cache import UpdateCache


class UpdateCacheTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture(old_build='updater.0', new_build='updater.1')
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        f = self.fixture
        manager = f.manager()
        self.assertEqual(f.plan(manager)['state'], 'planned')
        self.assertEqual(f.download(manager)['state'], 'ready')
        self.plan_id = manager.status()['plan_id']
        self.folder = f.data / 'updates/incremental' / self.plan_id
        self.plan = json.loads((self.folder / 'plan.json').read_bytes())
        snapshot = validate_plan(f.data, f.install, self.plan_id)
        self.job = f.data / 'updates/incremental-install' / ('b' * 32)
        self.job.mkdir(parents=True)
        write_json(self.job / 'job.json', dict(ticket=self.job.name, install_root=str(f.install), snapshot=snapshot))
        apply_incremental(snapshot, self.job)
        write_json(self.job / 'outcome.json', dict(ticket=self.job.name, state='installed', version=f.version))
        self.cache = UpdateCache(f.data, f.install, f.version)

    def test_preview_is_read_only_then_cleanup_preserves_program_backups_and_private_files(self):
        f = self.fixture
        (f.data / 'private.md').write_bytes(b'private')
        before_program = (f.install / 'RELEASE_MANIFEST.json').read_bytes()
        before_backup = (self.job / 'backup/server.py').read_bytes()
        preview = self.cache.preview(set())
        self.assertEqual(preview['cleanable_plans'], 1)
        self.assertTrue(self.folder.exists())
        result = self.cache.clean(preview['preview_id'], set())
        self.assertEqual(result['cleaned_plans'], 1)
        self.assertFalse(self.folder.exists())
        self.assertEqual((f.install / 'RELEASE_MANIFEST.json').read_bytes(), before_program)
        self.assertEqual((self.job / 'backup/server.py').read_bytes(), before_backup)
        self.assertEqual((f.data / 'private.md').read_bytes(), b'private')

    def test_active_and_future_plans_are_retained(self):
        self.assertEqual(self.cache.preview({self.plan_id})['cleanable_plans'], 0)
        older = UpdateCache(self.fixture.data, self.fixture.install, '0.4.18')
        self.assertEqual(older.preview(set())['cleanable_plans'], 0)

    def test_missing_or_failed_success_receipt_cannot_authorize_deletion(self):
        (self.job / 'outcome.json').unlink()
        self.assertEqual(self.cache.preview(set())['cleanable_plans'], 0)
        write_json(self.job / 'outcome.json', dict(ticket=self.job.name, state='rolled_back', version=self.fixture.version))
        self.assertEqual(self.cache.preview(set())['cleanable_plans'], 0)

    def test_changed_unknown_and_hardlinked_files_are_retained(self):
        stage = self.folder / 'stage'
        target = stage / 'server.py'
        original = target.read_bytes()
        target.write_bytes(b'changed')
        self.assertEqual(self.cache.preview(set())['cleanable_plans'], 0)
        target.write_bytes(original)
        (stage / 'private.txt').write_bytes(b'unknown')
        self.assertEqual(self.cache.preview(set())['cleanable_plans'], 0)
        (stage / 'private.txt').unlink()
        (stage / 'empty-unknown').mkdir()
        self.assertEqual(self.cache.preview(set())['cleanable_plans'], 0)
        (stage / 'empty-unknown').rmdir()
        alias = self.fixture.root / 'alias'
        os.link(target, alias)
        self.assertEqual(self.cache.preview(set())['cleanable_plans'], 0)

    def test_modified_cache_after_preview_rejects_before_any_deletion_and_consumes_confirmation(self):
        preview = self.cache.preview(set())
        unknown = self.folder / 'stage/private.txt'
        unknown.write_bytes(b'private')
        with self.assertRaises(UserError):
            self.cache.clean(preview['preview_id'], set())
        self.assertTrue((self.folder / 'plan.json').exists())
        self.assertEqual(unknown.read_bytes(), b'private')
        unknown.unlink()
        with self.assertRaises(UserError):
            self.cache.clean(preview['preview_id'], set())

    def test_pending_recovery_prevents_cleanup(self):
        journal = json.loads((self.job / 'journal.json').read_bytes())
        journal['state'] = 'recovery_required'
        write_json(self.job / 'journal.json', journal)
        with self.assertRaises(UserError):
            self.cache.preview(set())
        self.assertTrue(self.folder.exists())

    def test_tampered_plan_hash_and_protected_change_invalidate_confirmation(self):
        preview = self.cache.preview(set())
        with self.assertRaises(UserError):
            self.cache.clean(preview['preview_id'], {self.plan_id})
        plan = copy.deepcopy(self.plan)
        plan['asset']['size'] += 1
        (self.folder / 'plan.json').write_bytes(_json_bytes(plan))
        config = json.loads((self.job / 'job.json').read_bytes())
        config['snapshot']['plan'] = plan
        write_json(self.job / 'job.json', config)
        self.assertEqual(self.cache.preview(set())['cleanable_plans'], 0)

    def test_sixteen_completed_caches_can_be_explicitly_reclaimed(self):
        f = self.fixture
        base = self.folder.parent
        for index in range(15):
            plan = copy.deepcopy(self.plan)
            plan['asset']['id'] = index + 100
            identity = {key: value for key, value in plan.items() if key not in ('id', 'state')}
            plan['id'] = hashlib.sha256(_json_bytes(identity)).hexdigest()[:32]
            folder = base / plan['id']
            shutil.copytree(self.folder, folder)
            (folder / 'plan.json').write_bytes(_json_bytes(plan))
            job = self.job.parent / f'{index:032x}'
            shutil.copytree(self.job, job)
            config = json.loads((job / 'job.json').read_bytes())
            config.update(ticket=job.name)
            config['snapshot']['plan'] = plan
            write_json(job / 'job.json', config)
            journal = json.loads((job / 'journal.json').read_bytes())
            journal['ticket'] = job.name
            write_json(job / 'journal.json', journal)
            write_json(job / 'outcome.json', dict(ticket=job.name, state='installed', version=f.version))
        manager = f.manager()
        with self.assertRaises(ValueError):
            manager._folder('f' * 32)
        preview = self.cache.preview(set())
        self.assertEqual(preview['cleanable_plans'], 16)
        self.cache.clean(preview['preview_id'], set())
        self.assertTrue(manager._folder('f' * 32).is_dir())
        self.assertEqual(len(list(self.job.parent.glob('*/journal.json'))), 16)
