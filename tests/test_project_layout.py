"""Synthetic storage/layout regressions; never migrate real project files."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yingxu.store import Store, CATEGORIES, UserError
from yingxu.project_layout import category_paths
from yingxu.project_library import ProjectLibrary
from yingxu.organize import Organize


class ProjectLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='yingxu-layout-')
        self.root = Path(self.tmp.name).resolve()
        self.store = Store(self.root/'data', self.root/'项目库')
        self.library = ProjectLibrary(self.store)
        self.org = Organize(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_project_and_notes_follow_library_ancestors_and_visible_categories(self):
        parent = self.library.create_folder({'name':'个人作品'})
        folder = self.library.create_folder({'name':'练习','parent_id':parent['id']})
        project = self.store.create_project('短片', folder_id=folder['id'])
        expected = self.store.project_root/'个人作品'/'练习'/'短片'
        self.assertEqual(Path(project['root']), expected)
        note = self.store.create_item({'project_id':project['id'],'category':'scripts','name':'剧本'})
        self.assertEqual(Path(note['path']), expected/'文本'/'剧本.md')
        self.assertTrue(Path(note['path']).is_file())
        self.assertFalse((expected/'00_Brief').exists())
        self.assertEqual(project['layout_version'], 1)
        nested = self.org.create_folder(project['id'], 'scripts', '第1集')
        page = self.store.create_item({'project_id':project['id'],'category':'scripts','folder_id':nested['id'],'name':'场景'})
        self.assertEqual(Path(page['path']), expected/'文本'/'第1集'/'场景.md')
        self.assertEqual(page['folder_path'], '第1集')
        self.assertEqual(self.store.list_items(project['id'], folder=nested['id'])['items'][0]['folder_path'], '第1集')
        canvas = self.store.create_item({'project_id':project['id'],'category':'shots','name':'画板','format':'excalidraw'})
        self.assertEqual(Path(canvas['path']), expected/'素材'/'画板.excalidraw')

    def test_collision_does_not_reuse_unrelated_directories_and_keeps_stable_parent(self):
        unrelated = self.store.project_root/'练习'
        unrelated.mkdir(); (unrelated/'keep.txt').write_text('keep')
        folder = self.library.create_folder({'name':'练习'})
        first = self.store.create_project('作品',folder_id=folder['id'])
        second = self.store.create_project('作品',folder_id=folder['id'])
        a,b = Path(first['root']),Path(second['root'])
        self.assertNotEqual(a.parent,unrelated)
        self.assertEqual(a.parent,b.parent)
        self.assertEqual(a.name,'作品')
        self.assertNotEqual(a,b)
        self.assertEqual((unrelated/'keep.txt').read_text(),'keep')
        self.library.update_folder(folder['id'],{'name':'改分类名'})
        third = self.store.create_project('新作品',folder_id=folder['id'])
        self.assertEqual(Path(third['root']).parent,a.parent)
        self.assertTrue(a.exists())

    def test_sanitized_name_collision_cannot_merge_two_library_folders(self):
        a = self.library.create_folder({'name':'片/场'})
        b = self.library.create_folder({'name':'片:场'})
        first = Path(self.store.create_project('作品',folder_id=a['id'])['root'])
        second = Path(self.store.create_project('作品',folder_id=b['id'])['root'])
        self.assertNotEqual(first.parent,second.parent)
        self.assertEqual(first.parent.name,'片_场')

    def test_legacy_layout_survives_reopen_and_new_notes_and_folder_paths(self):
        project = self.store.create_project('旧项目')
        root = Path(project['root'])
        for child in root.iterdir():
            if child.is_dir():child.rmdir()
        for _,relative in CATEGORIES.values():(root/relative).mkdir(parents=True,exist_ok=True)
        with self.store.connection() as db:
            db.execute('UPDATE projects SET layout_version=0 WHERE id=?',(project['id'],))
        self.store = Store(self.root/'data',self.root/'项目库')
        self.org = Organize(self.store)
        folder = self.org.create_folder(project['id'],'scripts','第一集')
        note = self.store.create_item({'project_id':project['id'],'category':'scripts','folder_id':folder['id'],'name':'剧本'})
        self.assertEqual(Path(note['path']),root/'00_Brief/剧本与文档/第一集/剧本.md')
        self.assertEqual(note['folder_path'],'第一集')
        self.assertEqual(self.store.list_items(project['id'])['items'][0]['folder_path'],'第一集')
        self.assertFalse((root/'文本').exists())

    def test_missing_catalogued_directory_is_not_reused_by_new_project(self):
        project = self.store.create_project('同名项目')
        old = Path(project['root'])
        for child in old.iterdir():
            if child.is_dir():child.rmdir()
            else:child.unlink()
        old.rmdir()
        created = self.store.create_project('同名项目')
        self.assertNotEqual(created['root'],project['root'])
        self.assertFalse(old.exists())
        self.assertEqual(self.store.get_project(project['id'])['root'],str(old))

    def test_missing_storage_root_is_not_recreated_or_redirected(self):
        missing = self.root/'已拔出磁盘'
        self.store.project_root = missing
        with self.assertRaises(UserError):self.store.create_project('不应创建')
        self.assertFalse(missing.exists())
        self.assertEqual(self.store.list_projects(),[])

    def test_failed_project_creation_cleans_only_its_own_empty_directories(self):
        parent = self.library.create_folder({'name':'一层'})
        folder = self.library.create_folder({'name':'二层','parent_id':parent['id']})
        with patch('yingxu.store.now',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.store.create_project('短片',folder_id=folder['id'])
        self.assertEqual(list(self.store.project_root.iterdir()),[])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM managed_project_library_paths').fetchone()[0],0)

    def test_scan_and_move_managed_file_use_modern_categories(self):
        project = self.store.create_project('分类')
        source = self.store.sources(project['id'])[0]
        path = Path(project['root'])/'角色'/'图.png'
        from PIL import Image
        Image.new('RGB',(2,2)).save(path)
        self.store.index_files(source,[path])
        image = self.store.list_items(project['id'])['items'][0]
        self.assertEqual(image['category'],'characters')
        moved = self.org.move_items([image['id']],'scenes')['items'][0]
        self.assertEqual(Path(moved['path']),Path(project['root'])/'场景'/'图.png')
        self.assertFalse(path.exists())
        self.assertTrue(Path(moved['path']).exists())


if __name__ == '__main__':unittest.main()
