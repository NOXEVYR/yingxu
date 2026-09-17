"""Synthetic cross-project transfers only; no user directories or actual clipboard."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yingxu.cross_project import move_items
from yingxu.organize import Organize
from yingxu.resource_groups import ResourceGroups
from yingxu.store import Store, UserError


class CrossProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root/'data', self.root/'projects')
        self.organize = Organize(self.store); self.groups = ResourceGroups(self.store)
        self.source = self.store.create_project('原项目'); self.target = self.store.create_project('目标项目')

    def item(self, name='原文稿', content='保留内容', category='scripts'):
        return self.store.create_item({'project_id': self.source['id'], 'name': name, 'content': content,
                                      'category': category, 'metadata': {'prompt': '保留属性'}})

    def move(self, items, category='references', folder=None):
        return move_items(self.organize, [i['id'] for i in items], self.target['id'], category, folder)

    def test_owned_transfer_keeps_id_metadata_history_and_folder(self):
        item = self.item()
        content = self.store.read_content(item['id'])
        self.store.save_content(item['id'], {'etag': content['etag'], 'content': '更新内容'})
        before = self.store.get_item(item['id'], True)
        folder = self.organize.create_folder(self.target['id'], 'references', '子文件夹')
        result = self.move([item], folder=folder['id'])
        current = self.store.get_item(item['id'], True)
        self.assertEqual(current['project_id'], self.target['id'])
        self.assertEqual(current['id'], item['id'])
        self.assertEqual(current['metadata'], before['metadata'])
        self.assertEqual(current['versions'], before['versions'])
        self.assertEqual(Path(current['path']).parent, Path(folder['path']))
        self.assertFalse(Path(item['path']).exists())
        self.assertEqual(self.store.read_content(item['id'])['content'], '更新内容')
        self.assertEqual(result['stats']['moved'], 1)
        self.assertEqual(self.store.list_items(self.source['id'])['total'], 0)

    def test_external_reference_original_is_preserved(self):
        path = self.root/'outside.txt'; path.write_text('external', encoding='utf-8')
        source = self.store.register_source(self.source['id'], path, 'references')
        self.store.index_files(source, [path])
        item = self.store.list_items(self.source['id'])['items'][0]
        result = self.move([item]); moved = self.store.get_item(item['id'])
        self.assertEqual(self.store.resolve_item_path(moved), path)
        self.assertEqual(path.read_text(), 'external')
        self.assertEqual(result['stats']['referenced'], 1)
        self.assertEqual(moved['project_id'], self.target['id'])

    def test_collision_whole_batch_unchanged(self):
        a, b = self.item('A'), self.item('B')
        destination = self.organize.folder_path(self.target['id'], 'references')
        collision = destination/Path(b['path']).name; collision.write_text('do not overwrite')
        with self.assertRaises(UserError):self.move([a, b])
        for item in (a, b):
            self.assertTrue(Path(item['path']).exists())
            self.assertEqual(self.store.get_item(item['id'])['project_id'], self.source['id'])
        self.assertEqual(collision.read_text(), 'do not overwrite')
        self.assertFalse((destination/Path(a['path']).name).exists())

    def test_db_failure_rolls_back_published_copies(self):
        a, b = self.item('A'), self.item('B')
        with patch.object(self.store, '_search_row', side_effect=RuntimeError('synthetic transaction failure')):
            with self.assertRaises(RuntimeError):self.move([a, b])
        for item in (a, b):
            self.assertTrue(Path(item['path']).exists())
            self.assertEqual(self.store.get_item(item['id'])['project_id'], self.source['id'])
        self.assertEqual(list(self.organize.folder_path(self.target['id'], 'references').iterdir()), [])

    def test_partial_copy_failure_cleans_only_owned_temporary(self):
        item = self.item()
        def fail(source, destination):
            destination.write_bytes(b'partial'); raise OSError('synthetic disk full')
        with patch('yingxu.cross_project._copy', side_effect=fail), self.assertRaises(OSError):self.move([item])
        self.assertTrue(Path(item['path']).exists())
        self.assertEqual(list(self.organize.folder_path(self.target['id'], 'references').iterdir()), [])

    def test_group_and_relations_transfer_together(self):
        a, b = self.item('A'), self.item('B')
        group = self.groups.create({'project_id': self.source['id'], 'item_ids': [a['id'], b['id']]})
        self.store.add_relation(a['id'], b['id'], '关联')
        with self.assertRaises(UserError):self.move([a])
        self.move([a, b])
        self.assertEqual(self.groups.get(group['id'])['project_id'], self.target['id'])
        self.assertEqual(self.store.get_item(a['id'], True)['relations'][0]['item']['id'], b['id'])
        self.assertEqual(self.groups.get(group['id'])['member_ids'], [a['id'], b['id']])

    def test_partial_group_and_cross_boundary_relation_rejected(self):
        a, b = self.item('A'), self.item('B')
        self.groups.create({'project_id': self.source['id'], 'item_ids': [a['id'], b['id']]})
        with self.assertRaisesRegex(UserError, '素材组'):self.move([a])
        c = self.item('C'); self.store.add_relation(b['id'], c['id'], '关联')
        with self.assertRaisesRegex(UserError, '关联'):self.move([a, b])

    def test_shared_owned_file_is_not_removed_from_other_project(self):
        item = self.item(); path = Path(item['path'])
        source = self.store.register_source(self.target['id'], path, 'references')
        self.store.index_files(source, [path])
        with self.assertRaisesRegex(UserError, '引用'):self.move([item])
        self.assertTrue(path.exists())

    def test_moved_markdown_rewrites_links_and_creates_version(self):
        b = self.item('附件', category='references')
        relative = Path(b['path']).relative_to(Path(self.source['root'])).as_posix()
        a = self.item('文稿', content='[附件](../' + relative + ')')
        original = Path(a['path']).read_bytes()
        result = self.move([a, b])
        self.assertEqual(result['content_changed'], [a['id']])
        self.assertEqual(self.store.read_content(a['id'])['content'], '[附件](%E9%99%84%E4%BB%B6.md)')
        versions = self.store.get_item(a['id'], True)['versions']
        with self.store.connection() as db:
            path = db.execute('SELECT path FROM versions WHERE id=?', (versions[0]['id'],)).fetchone()[0]
        self.assertEqual(Path(path).read_bytes(), original)

    def test_remaining_markdown_link_blocks_move(self):
        a = self.item('链接目标')
        self.item('保留文稿', content='[引用](链接目标.md)')
        with self.assertRaisesRegex(UserError, '仍有文稿引用'):self.move([a])
        self.assertTrue(Path(a['path']).exists())

    def test_moving_markdown_without_local_dependency_is_rejected(self):
        self.item('链接目标'); a = self.item('文稿', content='[引用](链接目标.md)')
        with self.assertRaisesRegex(UserError, '本地链接'):self.move([a])

    def test_limits_invalid_folder_deleted_project_and_duplicate_ids(self):
        a = self.item()
        for ids in ([], [a['id']]*2, ['bad']*201):
            with self.assertRaises(UserError):move_items(self.organize, ids, self.target['id'], 'references')
        folder = self.organize.create_folder(self.source['id'], 'references', 'wrong')
        with self.assertRaises(UserError):self.move([a], folder=folder['id'])
        with patch('yingxu.cross_project.MAX_BYTES', 1), self.assertRaises(UserError):self.move([a])
        self.organize.delete_project(self.target['id'])
        with self.assertRaises(UserError):self.move([a])

    def test_commit_response_failure_does_not_delete_committed_destination(self):
        item = self.item()
        def uncertain_commit(db):
            db.commit(); raise OSError('synthetic post-commit response failure')
        with patch('yingxu.cross_project._commit', side_effect=uncertain_commit):result = self.move([item])
        self.assertTrue(result['ok']); self.assertTrue(result['warnings'])
        self.assertEqual(self.store.get_item(item['id'])['project_id'], self.target['id'])
        self.assertEqual(self.store.read_content(item['id'])['content'], '保留内容')

    def test_cleanup_failure_reports_committed_state_and_retains_both_copies(self):
        item = self.item(); original = Path(item['path']); unlink = Path.unlink
        def fail_source(path, *args, **kwargs):
            if path == original:raise PermissionError('synthetic busy source')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_source):result = self.move([item])
        self.assertTrue(result['ok']); self.assertTrue(result['warnings'])
        self.assertTrue(original.exists())
        self.assertTrue(Path(self.store.get_item(item['id'])['path']).exists())


if __name__ == '__main__':unittest.main()
