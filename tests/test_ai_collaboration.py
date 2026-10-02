import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from yingxu.ai_tasks import AITaskService, hash_file
from yingxu.skills import SkillLibrary
from yingxu.store import Store, UserError


class AICollaborationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.store = Store(self.root / 'data', self.root / 'projects')
        self.project = self.store.create_project('合成协作测试')
        self.service = AITaskService(self.store)
        self.task = self.service.create_task({'project_id': self.project['id'], 'title': '测试视频', 'goal': '生成合成结果', 'acceptance': ['内容正确']})

    def freeze(self, **extra):
        data = {'expected_revision': self.service.get_task(self.task['id'])['revision'], 'client_id': 'codex', 'conversation_id': 'conversation-A'}
        data.update(extra)
        return self.service.freeze_run(self.task['id'], data)

    def receive(self, run, name='result.txt', content=b'first', key='receipt-1', **extra):
        path = Path(run['directories']['generated']['path']) / name
        path.write_bytes(content)
        result = self.service.receipts.receive(self.task['id'], run['id'], {'idempotency_key': key, 'files': [{'relative_path': name, **extra}]})
        return path, result

    def review(self, aid, decision='accepted'):
        artifact = next(a for a in self.service.get_task(self.task['id'])['artifacts'] if a['id'] == aid)
        return self.service.review_artifact(self.task['id'], aid, {'decision': decision, 'expected_revision': artifact['revision'], 'notes': '用户合成审核'})

    def test_full_receipt_review_next_delta_and_restart(self):
        first = self.freeze()
        handoff = self.service.create_handoff(self.task['id'], first['id'], {'client_id': 'codex', 'conversation_id': 'conversation-A'})
        self.assertEqual(handoff['mode'], 'full')
        self.service.acknowledge_handoff(self.task['id'], handoff['id'])
        path, receipt = self.receive(first)
        self.assertEqual(receipt['state'], 'completed')
        self.assertEqual(receipt['results'][0]['sha256'], hashlib.sha256(b'first').hexdigest())
        aid = receipt['results'][0]['artifact_id']
        self.review(aid)
        self.assertNotEqual(self.service.get_task(self.task['id'])['status'], '已完成')
        second = self.freeze(goal='下一轮修正')
        delta = self.service.create_handoff(self.task['id'], second['id'], {'client_id': 'codex', 'conversation_id': 'conversation-A'})
        self.assertEqual(delta['mode'], 'delta')
        self.assertEqual(delta['base_snapshot_id'], handoff['id'])
        self.assertIn('下一轮修正', delta['prompt'])
        restarted = AITaskService(Store(self.root / 'data', self.root / 'projects'))
        self.assertEqual(restarted.get_run(self.task['id'], first['id'])['input_snapshot']['goal'], '生成合成结果')
        self.assertEqual(restarted.receipts.get(self.task['id'], receipt['id']), receipt)
        self.assertEqual(restarted.list_handoffs(self.task['id'], 'codex', 'conversation-A')['total'], 2)
        self.assertTrue(path.exists())

    def test_idempotent_receipt_lost_response_and_conflicting_key(self):
        run = self.freeze()
        _path, first = self.receive(run)
        request = {'idempotency_key': 'receipt-1', 'files': [{'relative_path': 'result.txt'}]}
        self.assertEqual(self.service.receipts.receive(self.task['id'], run['id'], request), first)
        self.assertEqual(len(self.service.get_task(self.task['id'])['artifacts']), 1)
        request['files'][0]['role'] = 'other'
        with self.assertRaises(UserError): self.service.receipts.receive(self.task['id'], run['id'], request)

    def test_complete_requires_actual_latest_verified_accepted_results_and_confirmation(self):
        run = self.freeze()
        _path, receipt = self.receive(run)
        revision = self.service.get_task(self.task['id'])['revision']
        with self.assertRaises(UserError): self.service.complete_task(self.task['id'], {'expected_revision': revision, 'confirmed': True})
        self.review(receipt['results'][0]['artifact_id'])
        revision = self.service.get_task(self.task['id'])['revision']
        with self.assertRaises(UserError): self.service.complete_task(self.task['id'], {'expected_revision': revision, 'confirmed': False})
        done = self.service.complete_task(self.task['id'], {'expected_revision': revision, 'confirmed': True})
        self.assertEqual(done['status'], '已完成')
        self.assertEqual(self.service.receipts.receive(self.task['id'], run['id'], {'idempotency_key': 'receipt-1', 'files': [{'relative_path': 'result.txt'}]}), receipt)
        with self.assertRaises(UserError): self.freeze()

    def test_file_change_invalidates_old_review_and_requires_new_round(self):
        run = self.freeze()
        path, receipt = self.receive(run)
        aid = receipt['results'][0]['artifact_id']
        self.review(aid)
        path.write_bytes(b'other')
        with self.assertRaises(UserError): self.review(aid)
        artifact = self.service.get_task(self.task['id'])['artifacts'][0]
        self.assertEqual(artifact['verification_status'], 'changed')
        self.assertEqual(artifact['review_sha256'], hashlib.sha256(b'first').hexdigest())
        with self.assertRaises(UserError):
            self.service.complete_task(self.task['id'], {'confirmed': True, 'expected_revision': self.service.get_task(self.task['id'])['revision']})
        next_run = self.freeze()
        second = self.service.receipts.receive(self.task['id'], next_run['id'], {'idempotency_key': 'new-version', 'files': [{'item_id': artifact['item_id']}]})
        self.assertEqual(second['state'], 'completed')
        self.assertNotEqual(second['results'][0]['artifact_id'], aid)

    def test_stale_revision_and_unknown_fields_rejected_and_draft_edit_keeps_frozen_input(self):
        run = self.freeze()
        with self.assertRaises(UserError): self.service.freeze_run(self.task['id'], {'expected_revision': 1, 'client_id': 'x', 'conversation_id': 'y'})
        with self.assertRaises(UserError): self.service.create_task({'project_id': self.project['id'], 'title': 'wrong', 'execute': 'x'})
        current = self.service.get_task(self.task['id'])
        self.service.update_task(self.task['id'], {'expected_revision': current['revision'], 'goal': '新草稿'})
        self.assertEqual(self.service.get_run(self.task['id'], run['id'])['input_snapshot']['goal'], '生成合成结果')

    def test_no_unacknowledged_delta_and_client_conversation_task_isolation(self):
        run = self.freeze()
        data = {'client_id': 'codex', 'conversation_id': 'conversation-A'}
        first = self.service.create_handoff(self.task['id'], run['id'], data)
        self.assertEqual(self.service.create_handoff(self.task['id'], run['id'], data)['mode'], 'full')
        self.service.acknowledge_handoff(self.task['id'], first['id'])
        newer = self.service.create_handoff(self.task['id'], run['id'], data)
        self.service.acknowledge_handoff(self.task['id'], newer['id'])
        with self.assertRaises(UserError): self.service.acknowledge_handoff(self.task['id'], first['id'])
        self.assertEqual(self.service.create_handoff(self.task['id'], run['id'], {'client_id': 'other', 'conversation_id': 'conversation-A'})['mode'], 'full')
        self.assertEqual(self.service.create_handoff(self.task['id'], run['id'], {'client_id': 'codex', 'conversation_id': 'new'})['mode'], 'full')
        other = self.service.create_task({'project_id': self.project['id'], 'title': '另一任务', 'goal': 'test'})
        with self.assertRaises(UserError): self.service.acknowledge_handoff(other['id'], first['id'])

    def test_per_file_errors_and_latest_failure_blocks_completion(self):
        first = self.freeze()
        _path, result = self.receive(first)
        self.review(result['results'][0]['artifact_id'])
        second = self.freeze()
        path = Path(second['directories']['generated']['path']) / 'good.txt'
        path.write_bytes(b'good')
        receipt = self.service.receipts.receive(self.task['id'], second['id'], {'idempotency_key': 'mixed', 'files': [{'relative_path': 'good.txt'}, {'relative_path': 'missing.txt'}]})
        self.assertEqual(receipt['state'], 'partial')
        self.assertEqual([r['status'] for r in receipt['results']], ['verified', 'error'])
        self.review(receipt['results'][0]['artifact_id'])
        with self.assertRaises(UserError): self.service.complete_task(self.task['id'], {'confirmed': True, 'expected_revision': self.service.get_task(self.task['id'])['revision']})

    def test_path_escape_private_files_foreign_and_recycled_items(self):
        run = self.freeze()
        directory = Path(run['directories']['generated']['path'])
        (directory / '.env.txt').write_bytes(b'private')
        receipt = self.service.receipts.receive(self.task['id'], run['id'], {'idempotency_key': 'bad-paths', 'files': [{'relative_path': '../outside.txt'}, {'relative_path': '.env.txt'}]})
        self.assertTrue(all(r['status'] == 'error' for r in receipt['results']))
        other = self.store.create_project('foreign')
        item = self.store.create_item({'project_id': other['id'], 'name': 'foreign'})
        with self.assertRaises(UserError): self.freeze(input_item_ids=[item['id']])
        source = self.store.register_source(self.project['id'], self.project['root'], 'generated')
        file = directory / 'removed.txt'
        file.write_bytes(b'keep')
        self.store.index_files(source, [file])
        with self.store.connection() as db:
            db.execute('UPDATE items SET removed=1 WHERE path=?', (str(file),))
        receipt = self.service.receipts.receive(self.task['id'], run['id'], {'idempotency_key': 'removed', 'files': [{'relative_path': 'removed.txt'}]})
        self.assertEqual(receipt['results'][0]['status'], 'error')
        self.assertTrue(file.exists())

    def test_hardlink_rejected_and_hash_runs_without_store_lock(self):
        run = self.freeze()
        directory = Path(run['directories']['generated']['path'])
        source = directory / 'source.txt'
        source.write_bytes(b'independent')
        linked = directory / 'linked.txt'
        try: os.link(source, linked)
        except OSError: self.skipTest('filesystem does not support hard links')
        receipt = self.service.receipts.receive(self.task['id'], run['id'], {'idempotency_key': 'linked', 'files': [{'relative_path': 'linked.txt'}]})
        self.assertEqual(receipt['results'][0]['status'], 'error')
        linked.unlink()
        def check_unlocked(path):
            acquired = []
            def probe():
                ok = self.store.lock.acquire(timeout=1)
                acquired.append(ok)
                if ok: self.store.lock.release()
            thread = threading.Thread(target=probe)
            thread.start(); thread.join()
            self.assertEqual(acquired, [True])
            return hash_file(path)
        with patch('yingxu.ai_receipts.hash_file', side_effect=check_unlocked):
            receipt = self.service.receipts.receive(self.task['id'], run['id'], {'idempotency_key': 'unlocked', 'files': [{'relative_path': 'source.txt'}]})
        self.assertEqual(receipt['state'], 'completed')

    def test_project_archive_blocks_read_and_write(self):
        run = self.freeze()
        with self.store.connection() as db: db.execute('UPDATE projects SET removed=1 WHERE id=?', (self.project['id'],))
        with self.assertRaises(UserError): self.service.get_task(self.task['id'])
        with self.assertRaises(UserError): self.service.receipts.preview(self.task['id'], run['id'])
        with self.assertRaises(UserError): self.service.create_task({'project_id': self.project['id'], 'title': 'bad'})

    def test_interrupted_receipt_survives_restart_without_automatic_replay(self):
        run = self.freeze()
        rid = 'a' * 32
        plan = '[{"relative_path":"result.txt"}]'
        result = {'id': rid, 'task_id': self.task['id'], 'run_id': run['id'], 'state': 'receiving', 'results': []}
        with self.store.connection() as db:
            db.execute('INSERT INTO ai_receipts VALUES(?,?,?,?,?,?,?,?,?)', (rid, self.task['id'], run['id'], 'interrupted', plan, 'receiving', json.dumps(result), 'now', 'now'))
        restarted = AITaskService(self.store)
        saved = restarted.receipts.get(self.task['id'], rid)
        self.assertEqual(saved['state'], 'interrupted')
        self.assertEqual(saved['results'], [])
        self.assertEqual(restarted.receipts.receive(self.task['id'], run['id'], {'idempotency_key': 'interrupted', 'files': [{'relative_path': 'result.txt'}]})['state'], 'interrupted')

    def test_legacy_category_paths_and_registered_note(self):
        with self.store.connection() as db: db.execute('UPDATE projects SET layout_version=0 WHERE id=?', (self.project['id'],))
        run = self.freeze()
        self.assertIn('40_Runs', run['directories']['generated']['path'])
        self.assertIn('10_References', run['directories']['references']['path'])
        with self.store.connection() as db:
            self.assertIsNotNone(db.execute('SELECT id FROM folders WHERE id=?', (run['directories']['generated']['folder_id'],)).fetchone())
            self.assertIsNotNone(db.execute('SELECT id FROM items WHERE project_id=? AND path=?', (self.project['id'], str(Path(run['directories']['references']['path']) / '本轮说明.md'))).fetchone())

    def test_fixed_skill_pin_survives_source_and_project_binding_changes(self):
        home = self.root / 'home'
        skill_dir = home / '.codex' / 'skills' / 'test-skill'
        skill_dir.mkdir(parents=True)
        source_file = skill_dir / 'SKILL.md'
        source_file.write_text('---\nname: 测试技能\n---\n\nfirst fixed guide\n', encoding='utf-8')
        with patch('yingxu.skills.Path.home', return_value=home): library = SkillLibrary(self.store)
        source = library.list()['skills'][0]
        preview = library.collections.preview(source['id'])
        saved = library.collections.collect(preview['token'])
        self.service.skills = library
        run = self.freeze(skill_pins=[{'collection_id': saved['id'], 'version': saved['version']}])
        source_file.write_text('changed mutable source', encoding='utf-8')
        with patch.object(library.collections, '_verify_version', side_effect=AssertionError('read must not hash whole package')):
            result = self.service.read_run_skill(self.task['id'], run['id'], saved['id'])
        self.assertIn('first fixed guide', result['content'])
        self.assertEqual(result['version'], saved['version'])
        with self.assertRaises(UserError): self.service.read_run_skill(self.task['id'], run['id'], 'col_' + '0' * 32)
        Path(saved['path'], 'SKILL.md').write_text('tampered', encoding='utf-8')
        with self.assertRaises(UserError): self.service.read_run_skill(self.task['id'], run['id'], saved['id'])

    def test_failed_receipt_resolves_only_when_same_file_is_verified_later(self):
        run = self.freeze()
        first = self.service.receipts.receive(self.task['id'], run['id'], {'idempotency_key': 'missing-first', 'files': [{'relative_path': 'late.txt'}]})
        self.assertEqual(first['state'], 'partial')
        _path, other = self.receive(run, name='other.txt', key='unrelated')
        self.review(other['results'][0]['artifact_id'])
        with self.assertRaises(UserError): self.service.complete_task(self.task['id'], {'confirmed': True, 'expected_revision': self.service.get_task(self.task['id'])['revision']})
        _path, recovered = self.receive(run, name='late.txt', key='explicit-retry')
        self.review(recovered['results'][0]['artifact_id'])
        self.assertEqual(self.service.complete_task(self.task['id'], {'confirmed': True, 'expected_revision': self.service.get_task(self.task['id'])['revision']})['status'], '已完成')
        self.assertEqual(self.service.receipts.get(self.task['id'], first['id'])['state'], 'partial')

    def test_rejected_candidate_can_be_excluded_pending_and_revision_cannot(self):
        run = self.freeze()
        _path, first = self.receive(run, name='adopted.txt', key='adopted')
        self.review(first['results'][0]['artifact_id'])
        _path, second = self.receive(run, name='candidate.txt', key='candidate')
        aid = second['results'][0]['artifact_id']
        with self.assertRaises(UserError): self.service.complete_task(self.task['id'], {'confirmed': True, 'expected_revision': self.service.get_task(self.task['id'])['revision']})
        self.review(aid, 'needs_revision')
        with self.assertRaises(UserError): self.service.complete_task(self.task['id'], {'confirmed': True, 'expected_revision': self.service.get_task(self.task['id'])['revision']})
        self.review(aid, 'rejected')
        self.assertEqual(self.service.complete_task(self.task['id'], {'confirmed': True, 'expected_revision': self.service.get_task(self.task['id'])['revision']})['status'], '已完成')

    def test_moved_project_root_uses_current_folder_identity_and_relative_path(self):
        import shutil
        run = self.freeze()
        old_root = Path(self.project['root'])
        moved = self.root / 'moved-project'
        shutil.copytree(old_root, moved)
        with self.store.connection() as db:
            db.execute('UPDATE projects SET root=? WHERE id=?', (str(moved), self.project['id']))
            sources = db.execute('SELECT id,path FROM sources WHERE project_id=?', (self.project['id'],)).fetchall()
            for row in sources:
                db.execute('UPDATE sources SET path=? WHERE id=?', (str(moved / Path(row['path']).relative_to(old_root)), row['id']))
            items = db.execute('SELECT id,path FROM items WHERE project_id=?', (self.project['id'],)).fetchall()
            for row in items:
                db.execute('UPDATE items SET path=? WHERE id=?', (str(moved / Path(row['path']).relative_to(old_root)), row['id']))
        current = self.service.get_run(self.task['id'], run['id'])
        self.assertTrue(Path(current['directories']['generated']['path']).is_relative_to(moved))
        self.assertEqual(current['directories']['generated']['folder_id'], run['directories']['generated']['folder_id'])
        self.assertEqual(self.service.receipts.preview(self.task['id'], run['id'])['files'], [])
        _path, result = self.receive(current, key='after-move')
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(self.service.get_run(self.task['id'], run['id'])['input_digest'], run['input_digest'])

    def test_preview_continues_after_unreadable_entry(self):
        run = self.freeze()
        directory = Path(run['directories']['generated']['path'])
        bad = directory / 'bad.txt'
        bad.write_bytes(b'bad')
        (directory / 'good.txt').write_bytes(b'good')
        from yingxu.store import clean_path as original
        def fake_clean(path):
            if Path(path) == bad: raise UserError('mock inaccessible entry')
            return original(path)
        with patch('yingxu.ai_receipts.clean_path', side_effect=fake_clean):
            preview = self.service.receipts.preview(self.task['id'], run['id'])
        self.assertEqual(preview['files'][0]['relative_path'], 'good.txt')
        self.assertTrue(preview['errors'])
        self.assertFalse(preview['truncated'])

    def test_old_run_review_preserves_latest_task_status(self):
        run = self.freeze()
        _path, receipt = self.receive(run)
        latest = self.freeze()
        before = self.service.get_task(self.task['id'])
        self.assertEqual(before['status'], '进行中')
        self.review(receipt['results'][0]['artifact_id'])
        after = self.service.get_task(self.task['id'])
        self.assertEqual(after['status'], '进行中')
        self.assertGreater(after['revision'], before['revision'])
        self.assertEqual(after['runs'][0]['id'], latest['id'])

    def test_delta_only_contains_changed_entities_and_summary_reads_are_lightweight(self):
        one = self.store.create_item({'project_id': self.project['id'], 'name': 'one'})
        two = self.store.create_item({'project_id': self.project['id'], 'name': 'two'})
        run = self.freeze(input_item_ids=[one['id'], two['id']])
        scope = {'client_id': 'codex', 'conversation_id': 'conversation-A'}
        initial = self.service.create_handoff(self.task['id'], run['id'], scope)
        self.service.acknowledge_handoff(self.task['id'], initial['id'])
        next_run = self.freeze(input_item_ids=[one['id']])
        delta = self.service.create_handoff(self.task['id'], next_run['id'], scope)
        self.assertEqual(delta['changes']['inputs']['added'], [])
        self.assertEqual(delta['changes']['inputs']['changed'], [])
        self.assertEqual(delta['changes']['inputs']['removed_from_scope'][0]['item_id'], two['id'])
        self.assertNotIn(one['id'], delta['prompt'])
        self.assertIn('metadata_only', delta['prompt'])
        detail = self.service.get_task(self.task['id'])
        self.assertTrue(all('input_snapshot' not in r for r in detail['runs']))
        card = self.service.list_tasks(self.project['id'])['tasks'][0]
        self.assertNotIn('goal', card)
        self.assertNotIn('acceptance', card)
        self.assertIn('input_snapshot', self.service.get_run(self.task['id'], run['id']))

    def test_task_create_idempotency_survives_lost_response_restart_and_conflicts(self):
        request = {'project_id': self.project['id'], 'title': '幂等创建', 'goal': 'same request', 'idempotency_key': 'create-once'}
        first = self.service.create_task(request)
        restarted = AITaskService(Store(self.root / 'data', self.root / 'projects'))
        self.assertEqual(restarted.create_task(request)['id'], first['id'])
        self.assertEqual(restarted.list_tasks(self.project['id'])['total'], 2)
        request['title'] = 'changed'
        with self.assertRaises(UserError): restarted.create_task(request)
        other = self.store.create_project('other create scope')
        request['project_id'] = other['id']
        self.assertNotEqual(restarted.create_task(request)['id'], first['id'])

    def test_expected_sha_failure_is_persistent_and_does_not_create_artifact(self):
        run = self.freeze()
        _path, receipt = self.receive(run, expected_sha256='0' * 64)
        self.assertEqual(receipt['state'], 'partial')
        self.assertEqual(receipt['results'][0]['status'], 'error')
        self.assertEqual(self.service.get_run(self.task['id'], run['id'])['artifacts'], [])
        self.assertEqual(self.service.receipts.get(self.task['id'], receipt['id']), receipt)

    def test_freeze_refuses_input_changed_after_hash_before_commit(self):
        item = self.store.create_item({'project_id': self.project['id'], 'name': 'mutable-input'})
        original = hash_file
        def changed_after_hash(path):
            result = original(path)
            Path(path).write_bytes(b'changed before commit')
            return result
        with patch('yingxu.ai_tasks.hash_file', side_effect=changed_after_hash):
            with self.assertRaises(UserError): self.freeze(input_item_ids=[item['id']])
        self.assertEqual(self.service.get_task(self.task['id'])['runs'], [])


if __name__ == '__main__':
    unittest.main()
