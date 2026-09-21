import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yingxu.store import Store, UserError
from yingxu.jobs import Jobs
from yingxu.organize import Organize
from yingxu.file_import import import_files
from yingxu.project_layout import category_paths
from yingxu.disk_layout import identity


class DiskLayoutSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='yingxu-disk-sync-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = Store(self.root/'data', self.root/'projects')
        self.project = self.store.create_project('合成项目')
        self.pid = self.project['id']
        self.source = self.store.sources(self.pid)[0]
        self.org = Organize(self.store)
        self.text = Path(self.project['root'])/category_paths(self.project)['scripts']

    def scan(self):
        jobs = Jobs(self.store)
        jid = jobs.submit(self.pid)['job_id']
        jobs.pool.shutdown(wait=True)
        self.assertEqual(jobs.get(jid)['errors'], [])

    def test_disk_drop_and_empty_nested_directories_are_visible_in_their_real_location(self):
        target = self.text/'第一集'/'场次一'; target.mkdir(parents=True)
        (self.text/'空文件夹').mkdir()
        document = target/'剧本.md'; document.write_text('原文', encoding='utf-8')
        self.scan()
        items = self.store.list_items(self.pid, category='scripts')['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['folder_path'], '第一集/场次一')
        folders = self.org.folders(self.pid, 'scripts')['folders']
        self.assertIn('空文件夹', [f['folder_path'] for f in folders])
        self.assertEqual(Path(self.org.get_folder(items[0]['folder_id'])['path']), target)
        self.assertEqual(document.read_text(encoding='utf-8'), '原文')

    def test_unchanged_wrong_category_is_repaired_without_reparsing_or_losing_metadata(self):
        item = self.store.create_item({'project_id':self.pid,'category':'scripts','name':'正文','content':'原文','tags':['保留']})
        self.scan()
        with self.store.connection() as db:
            db.execute("UPDATE items SET category='unclassified' WHERE id=?", (item['id'],))
        with patch.object(self.store, 'inspect_file', side_effect=AssertionError('must not reparse')):
            self.scan()
        current = self.store.get_item(item['id'])
        self.assertEqual(current['category'], 'scripts')
        self.assertEqual(current['tags'], ['保留'])
        result = self.org.move_items([item['id']], 'scripts')
        self.assertEqual(result['stats']['unchanged'], 1)

    def test_drag_own_file_to_its_directory_does_not_copy_or_report_self_collision(self):
        path = self.text/'正文.md'; path.write_text('same file', encoding='utf-8')
        result = import_files(self.store,path,self.pid,'scripts',move_owned=True)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(list(self.text.iterdir()), [path])
        self.assertEqual(self.store.list_items(self.pid)['total'], 1)

    def test_drag_own_file_moves_physical_path_and_keeps_identity_and_history(self):
        item = self.store.create_item({'project_id':self.pid,'category':'unclassified','name':'正文','content':'original'})
        original = Path(item['path'])
        opened = self.store.read_content(item['id'])
        self.store.save_content(item['id'], {'etag':opened['etag'],'content':'saved'})
        result = import_files(self.store,original,self.pid,'scripts',move_owned=True)
        self.assertEqual(result['done'], 1)
        current = self.store.get_item(item['id'], True)
        self.assertEqual(Path(current['path']).parent, self.text)
        self.assertFalse(original.exists())
        self.assertEqual(len(current['versions']), 1)
        self.assertEqual(Path(current['path']).read_text(encoding='utf-8'), 'saved')

    def test_actual_other_file_collision_preserves_both_originals(self):
        item = self.store.create_item({'project_id':self.pid,'category':'unclassified','name':'正文','content':'first'})
        other = self.text/'正文.md'; other.write_text('second', encoding='utf-8')
        with self.assertRaises(UserError):
            import_files(self.store,Path(item['path']),self.pid,'scripts',move_owned=True)
        self.assertEqual(other.read_text(encoding='utf-8'), 'second')
        self.assertEqual(Path(item['path']).read_text(encoding='utf-8'), 'first')

    def test_external_reference_classification_and_recycled_directories_are_preserved(self):
        external = self.root/'external.md'; external.write_text('external', encoding='utf-8')
        source = self.store.register_source(self.pid, external, 'characters')
        self.store.index_files(source, [external])
        folder = self.org.create_folder(self.pid,'scripts','已删除')
        item = self.store.create_item({'project_id':self.pid,'category':'scripts','folder_id':folder['id'],'name':'旧笔记'})
        self.org.delete_folder(folder['id'])
        self.scan()
        self.assertEqual(self.store.list_items(self.pid,category='characters')['total'], 1)
        self.assertEqual(self.store.list_items(self.pid,category='scripts')['total'], 0)
        self.assertTrue(Path(item['path']).exists())

    def test_explorer_move_preserves_observed_file_id_and_does_not_create_ghost_duplicate(self):
        item = self.store.create_item({'project_id':self.pid,'category':'unclassified','name':'正文','content':'same'})
        old = Path(item['path'])
        if identity(old) is None:self.skipTest('stable birth timestamp unavailable')
        self.scan()
        target = self.text/'新名字.md'; old.rename(target)
        self.scan()
        current = self.store.get_item(item['id'])
        self.assertEqual(current['path'], str(target))
        self.assertEqual(current['category'], 'scripts')
        self.assertEqual(current['name'], '新名字')
        self.assertEqual(len([row for row in self.store.list_items(self.pid)['items'] if row['path'] in (str(old),str(target))]), 1)

    def test_copy_of_identical_bytes_is_not_mistaken_for_a_move(self):
        item = self.store.create_item({'project_id':self.pid,'category':'unclassified','name':'正文','content':'same'})
        self.scan()
        (self.text/'副本.md').write_bytes(Path(item['path']).read_bytes())
        self.scan()
        self.assertEqual(self.store.get_item(item['id'])['path'], item['path'])
        self.assertEqual(self.store.list_items(self.pid,category='scripts')['total'], 1)
        self.assertEqual(self.store.list_items(self.pid,category='unclassified')['total'], 1)

    def test_focus_sync_never_scans_external_registered_sources(self):
        external=self.root/'external';external.mkdir()
        self.store.register_source(self.pid,external,'references')
        (external/'outside.md').write_text('outside',encoding='utf-8')
        (self.text/'inside.md').write_text('inside',encoding='utf-8')
        jobs=Jobs(self.store)
        jid=jobs.submit(self.pid,owned_only=True)['job_id'];jobs.pool.shutdown(wait=True)
        self.assertEqual(jobs.get(jid)['errors'],[])
        paths={row['path'] for row in self.store.list_items(self.pid)['items']}
        self.assertIn(str(self.text/'inside.md'),paths)
        self.assertNotIn(str(external/'outside.md'),paths)


if __name__ == '__main__':
    unittest.main()
