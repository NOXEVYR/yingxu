import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yingxu.store import Store, UserError
from yingxu.organize import Organize
from yingxu.project_library import ProjectLibrary
from yingxu.skills import SkillLibrary
from yingxu.trash import TrashDeletion


class CategoryDeleteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='yingxu-category-delete-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = Store(self.root/'data', self.root/'projects')
        self.library = ProjectLibrary(self.store)
        self.org = Organize(self.store)
        self.parent = self.library.create_folder({'name': '取消的项目'})
        self.child = self.library.create_folder({'name': '子分类', 'parent_id': self.parent['id']})
        self.projects = [self.store.create_project(name, folder_id=folder['id']) for name, folder in
                         [('第一集', self.parent), ('第二集', self.child)]]
        self.other = self.store.create_project('保留项目')
        self.items = [self.store.create_item({'project_id': p['id'], 'category': 'scripts',
                     'name': '正文', 'content': '合成测试正文'}) for p in self.projects]

    def delete(self, plan=None):
        plan = plan or self.library.preview_delete_contents(self.parent['id'])
        return self.library.delete_contents(self.parent['id'], {'token': plan['token']}, self.org)

    def test_entire_tree_moves_to_recoverable_batches_and_unrelated_project_survives(self):
        plan = self.library.preview_delete_contents(self.parent['id'])
        self.assertEqual((plan['folder_count'], plan['project_count']), (2, 2))
        self.assertEqual(len(self.store.list_projects()), 3)
        result = self.delete(plan)
        self.assertEqual([p['id'] for p in self.store.list_projects()], [self.other['id']])
        self.assertEqual(self.library.snapshot()['folders'], [])
        for item in self.items:
            self.assertEqual(Path(item['path']).read_text(encoding='utf-8'), '合成测试正文')
        for entry in result['entries']:
            self.org.restore(entry['id'])
        self.assertEqual(len(self.store.list_projects()), 3)
        self.assertTrue(all(p['folder_id'] is None for p in self.library.snapshot()['projects']))

    def test_new_project_or_member_invalidates_preview_without_partial_deletion(self):
        for change in ('project', 'item', 'reparent'):
            plan = self.library.preview_delete_contents(self.parent['id'])
            if change == 'project':
                self.store.create_project('晚加入', folder_id=self.child['id'])
            elif change == 'item':
                self.store.create_item({'project_id': self.projects[0]['id'], 'category': 'scripts', 'name': '新增'})
            else:
                self.library.update_folder(self.child['id'], {'parent_id': None})
            before = self.library.snapshot()
            with self.assertRaises(UserError):
                self.delete(plan)
            self.assertEqual(before, self.library.snapshot())
            self.assertEqual(self.org.trash()['total'], 0)

    def test_expired_foreign_replayed_and_malformed_confirmation_rejected(self):
        plan = self.library.preview_delete_contents(self.parent['id'])
        with self.assertRaises(UserError):
            self.library.delete_contents(self.child['id'], {'token': plan['token']}, self.org)
        plan = self.library.preview_delete_contents(self.parent['id'])
        self.library.delete_plans[plan['token']]['expires'] = 0
        with self.assertRaises(UserError):
            self.delete(plan)
        for body in ({}, {'token': []}, {'token': 'x', 'all': True}):
            with self.assertRaises(UserError):
                self.library.delete_contents(self.parent['id'], body, self.org)
        plan = self.library.preview_delete_contents(self.parent['id'])
        self.delete(plan)
        with self.assertRaises(UserError):
            self.delete(plan)

    def test_mid_transaction_failure_rolls_back_all_projects_and_categories(self):
        original = self.org._mark_batch
        calls = []
        def fail_second(*args):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('synthetic failure')
            return original(*args)
        before = self.library.snapshot()
        with patch.object(self.org, '_mark_batch', side_effect=fail_second), self.assertRaises(RuntimeError):
            self.delete()
        self.assertEqual(before, self.library.snapshot())
        self.assertEqual(self.org.trash()['total'], 0)

    def test_scoped_disk_preview_recycles_only_selected_project_roots(self):
        result = self.delete()
        with patch('yingxu.skills.Path.home', return_value=self.root/'home'):
            deletion = TrashDeletion(self.store, SkillLibrary(self.store))
        plan = deletion.preview({'entries': result['entries']})
        self.assertTrue(all('error' not in entry for entry in plan['entries']), plan)
        self.assertEqual(set(plan['paths']), {p['root'] for p in self.projects})
        recycled = self.root/'synthetic-recycle'; recycled.mkdir()
        def recycle(path):
            Path(path).rename(recycled/Path(path).name)
        with patch('yingxu.trash.recycle_path', side_effect=recycle):
            outcome = deletion.delete({'token': plan['token']})
        self.assertEqual(outcome['deleted'], 2)
        self.assertTrue(Path(self.other['root']).is_dir())
        self.assertTrue(all(not Path(p['root']).exists() for p in self.projects))

    def test_disk_change_after_preview_keeps_files_and_recovery_records(self):
        result = self.delete()
        with patch('yingxu.skills.Path.home', return_value=self.root/'home'):
            deletion = TrashDeletion(self.store, SkillLibrary(self.store))
        plan = deletion.preview({'entries': result['entries']})
        Path(self.items[0]['path']).write_text('changed', encoding='utf-8')
        with patch('yingxu.trash.recycle_path') as recycle, self.assertRaises(UserError):
            deletion.delete({'token': plan['token']})
        recycle.assert_not_called()
        self.assertEqual(self.org.trash()['total'], 2)


if __name__ == '__main__':
    unittest.main()
