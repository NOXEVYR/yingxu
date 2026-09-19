import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from yingxu.store import Store, UserError
from yingxu.project_library import ProjectLibrary
from yingxu.jobs import Jobs
from yingxu.project_folder_import import copy_project
from yingxu.organize import Organize


class ProjectFolderImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='yingxu-folder-import-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = Store(self.root/'data', self.root/'projects')
        self.library = ProjectLibrary(self.store)
        self.jobs = Jobs(self.store)
        self.addCleanup(self.jobs.pool.shutdown, wait=True)
        self.source = self.root/'外部项目'
        self.source.mkdir()

    def file(self, name, content=b'example'):
        path = self.source/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def wait(self, result):
        for _ in range(300):
            job = self.jobs.get(result['job_id'])
            if job['state'] in ('done','error'):
                return job
            time.sleep(.02)
        self.fail('job did not finish')

    def test_copies_classifies_nested_empty_and_unknown_without_touching_source(self):
        category = self.library.create_folder({'name':'个人作品'})
        self.file('剧本.md', '# 剧本\n![角色](角色/主角/立绘.png)\n'.encode())
        self.file('角色/主角/立绘.png')
        self.file('其他/素材.jpg')
        self.file('其他/sidecar.bin', b'unsupported but preserved')
        (self.source/'场景'/'空文件夹').mkdir(parents=True)
        before = {p.relative_to(self.source):p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        job = self.wait(self.jobs.open_project_folder(str(self.source), category['id']))
        self.assertEqual(job['state'], 'done', job)
        self.assertEqual(job['errors'], [])
        project = self.store.get_project(job['project_id']); root = Path(project['root'])
        self.assertEqual(root.parent.name, '个人作品')
        self.assertEqual(root.parent.parent, self.store.project_root)
        self.assertEqual((root/'未分类/其他/sidecar.bin').read_bytes(), b'unsupported but preserved')
        from urllib.parse import unquote
        self.assertIn('../角色/主角/立绘.png', unquote((root/'文本/剧本.md').read_text('utf-8')))
        items = {i['name']:i for i in self.store.list_items(project['id'])['items']}
        self.assertEqual(items['剧本']['category'], 'scripts')
        self.assertEqual(items['立绘']['category'], 'characters')
        self.assertEqual(items['立绘']['folder_path'], '主角')
        self.assertEqual(items['素材']['category'], 'unclassified')
        self.assertEqual(before, {p.relative_to(self.source):p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        folders = Organize(self.store).folders(project['id'])['folders']
        self.assertTrue(any(f['name']=='空文件夹' and f['category']=='scenes' for f in folders))
        self.assertEqual(self.library.snapshot()['projects'][0]['folder_id'], category['id'])

    def test_legacy_layout_and_alias_merge(self):
        self.file('00_Brief/剧本与文档/a.md'); self.file('20_Assets/角色/b.png')
        self.file('文本/c.txt'); self.file('白模预演/test.mp4')
        job = self.wait(self.jobs.open_project_folder(str(self.source)))
        self.assertEqual(job['state'], 'done', job)
        root=Path(self.store.get_project(job['project_id'])['root'])
        for path in ['文本/a.md','角色/b.png','文本/c.txt','预演/test.mp4']:
            self.assertTrue((root/path).is_file(),path)

    def test_retry_reopens_copy_and_keeps_edits_removed_items_and_assignment(self):
        self.file('a.md')
        first=self.wait(self.jobs.open_project_folder(str(self.source)))
        item=self.store.list_items(first['project_id'])['items'][0]
        Path(item['path']).write_text('user edit',encoding='utf-8')
        Organize(self.store).delete_items([item['id']])
        second=self.wait(self.jobs.open_project_folder(str(self.source)))
        self.assertEqual(first['project_id'],second['project_id'])
        self.assertFalse(second['created'])
        self.assertEqual(len(self.store.list_projects()),1)
        self.assertEqual(self.store.list_items(first['project_id'])['total'],0)
        self.assertEqual(Path(item['path']).read_text('utf-8'),'user edit')

    def test_same_names_never_overwrite_other_projects(self):
        original=self.store.create_project(self.source.name)
        self.file('a.md')
        job=self.wait(self.jobs.open_project_folder(str(self.source)))
        self.assertNotEqual(self.store.get_project(job['project_id'])['root'],original['root'])
        self.assertEqual(len(self.store.list_projects()),2)

    def test_alias_conflict_leaves_no_project_or_copy(self):
        self.file('文本/a.md'); self.file('剧本/a.md')
        job=self.wait(self.jobs.open_project_folder(str(self.source)))
        self.assertEqual(job['state'],'error')
        self.assertIn('同名',job['message'])
        self.assertEqual(self.store.list_projects(),[])
        self.assertEqual(list(self.store.project_root.iterdir()),[])

    def test_copy_failure_rolls_back_only_own_output(self):
        self.file('a.md')
        from yingxu.project_migration import _digest
        def digest(path):
            if Path(path).is_relative_to(self.store.project_root):return 'bad digest'
            return _digest(path)
        with patch('yingxu.project_migration._digest',side_effect=digest):
            with self.assertRaisesRegex(UserError,'校验失败'):
                copy_project(self.store,str(self.source),None,lambda message:None)
        self.assertEqual(self.store.list_projects(),[])
        self.assertEqual(list(self.store.project_root.iterdir()),[])
        self.assertEqual((self.source/'a.md').read_bytes(),b'example')

    def test_rejects_invalid_sources_and_category_without_project(self):
        single=self.file('a.md')
        for path,folder in [(single,None),(self.store.data_root,None),(self.root,None),(self.source,'missing')]:
            with self.subTest(path=path),self.assertRaises(UserError):
                self.jobs.open_project_folder(str(path),folder)
        self.assertEqual(self.store.list_projects(),[])

    def test_source_mutation_is_not_committed(self):
        path=self.file('a.md')
        def progress(message):
            if message.startswith('已复制'):path.write_bytes(b'changed')
        with self.assertRaisesRegex(UserError,'发生变化'):
            copy_project(self.store,str(self.source),None,progress)
        self.assertEqual(self.store.list_projects(),[])
        self.assertEqual(list(self.store.project_root.iterdir()),[])

    def test_empty_project_and_new_document(self):
        job=self.wait(self.jobs.open_project_folder(str(self.source)))
        self.assertEqual(job['state'],'done',job)
        item=self.store.create_item({'project_id':job['project_id'],'category':'scripts','name':'新文稿','content':''})
        self.assertEqual(Path(item['path']).parent.name,'文本')

    def test_queue_duplicate_has_single_job(self):
        import threading
        entered=threading.Event();release=threading.Event()
        def blocked(*args):
            entered.set();release.wait(5)
            return copy_project(*args)
        with patch('yingxu.project_folder_import.copy_project',side_effect=blocked):
            first=self.jobs.open_project_folder(str(self.source));self.assertTrue(entered.wait(3))
            second=self.jobs.open_project_folder(str(self.source))
            self.assertEqual(first,second)
            release.set();self.assertEqual(self.wait(first)['state'],'done')

    def test_copied_project_supports_existing_storage_migration(self):
        from yingxu.project_storage import ProjectStorage
        from yingxu.project_migration import ProjectMigration
        from yingxu.settings import Settings
        self.file('剧本/a.md');self.file('其他/sub/b.png')
        job=self.wait(self.jobs.open_project_folder(str(self.source)))
        storage=ProjectStorage(self.store,Settings(self.store.data_root))
        migration=ProjectMigration(self.store,storage)
        target=self.root/'new-storage';target.mkdir()
        preview=migration.preview({'root':str(target),'project_ids':[job['project_id']]})
        migration.execute({'token':preview['token']})
        current=self.store.get_project(job['project_id'])
        self.assertTrue(Path(current['root']).is_relative_to(target))
        second=self.wait(self.jobs.open_project_folder(str(self.source)))
        self.assertEqual(second['project_id'],job['project_id'])
        self.assertEqual(self.store.list_items(current['id'])['total'],2)


if __name__=='__main__':unittest.main()
