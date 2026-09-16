import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yingxu.file_import import import_files, validate_source
from yingxu.jobs import Jobs
from yingxu.organize import Organize
from yingxu.store import Store, UserError


class FileImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='yingxu-managed-import-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.store=Store(self.root/'data',self.root/'projects')
        self.project=self.store.create_project('合成项目')
        self.pid=self.project['id']
        self.external=self.root/'outside';self.external.mkdir()
        self.organize=Organize(self.store)

    def source(self,name='草稿.md',content='# 原文件'):
        path=self.external/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(content,encoding='utf-8')
        return path

    def items(self):
        with self.store.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM items WHERE project_id=?',(self.pid,))]

    def test_external_markdown_copied_into_managed_category_only(self):
        source=self.source();before=source.read_bytes()
        result=import_files(self.store,source,self.pid,'scripts')
        self.assertEqual(result,{'done':1,'skipped':0,'errors':[]})
        item=self.items()[0]
        self.assertEqual(Path(item['path']).parent,self.organize.folder_path(self.pid,'scripts'))
        self.assertEqual(item['category'],'scripts')
        self.assertEqual(Path(item['path']).read_bytes(),before)
        self.assertEqual(source.read_bytes(),before)
        self.assertEqual(len(self.store.sources(self.pid)),1)

    def test_nested_and_empty_folders_preserved_in_selected_folder(self):
        source=self.source('素材/分集/一.md')
        (self.external/'素材'/'空目录').mkdir()
        selected=self.organize.create_folder(self.pid,'scripts','收集')
        result=import_files(self.store,self.external/'素材',self.pid,'scripts',selected['id'])
        target=Path(selected['path'])/'素材'/'分集'/'一.md'
        self.assertEqual(result['done'],1)
        self.assertEqual(target.read_bytes(),source.read_bytes())
        self.assertTrue((Path(selected['path'])/'素材'/'空目录').is_dir())
        item=self.items()[0]
        folder=self.organize.get_folder(item['folder_id'])
        self.assertEqual(Path(folder['path']),target.parent)

    def test_duplicate_file_and_directory_names_never_overwrite(self):
        source=self.source('一.md','原始')
        first=import_files(self.store,source,self.pid,'scripts')
        source.write_text('第二次',encoding='utf-8')
        second=import_files(self.store,source,self.pid,'scripts')
        parent=self.organize.folder_path(self.pid,'scripts')
        self.assertEqual(first['done']+second['done'],2)
        self.assertEqual((parent/'一.md').read_text('utf-8'),'原始')
        self.assertEqual((parent/'一 (2).md').read_text('utf-8'),'第二次')
        self.source('目录/a.md')
        import_files(self.store,self.external/'目录',self.pid,'scripts')
        import_files(self.store,self.external/'目录',self.pid,'scripts')
        self.assertTrue((parent/'目录'/'a.md').exists())
        self.assertTrue((parent/'目录 (2)'/'a.md').exists())

    def test_copy_write_failure_removes_partial_and_preserves_original(self):
        source=self.source();before=source.read_bytes()
        destination=self.organize.folder_path(self.pid,'scripts')
        sentinel=destination/'existing.md';sentinel.write_text('keep',encoding='utf-8')
        with patch('yingxu.file_import.os.fsync',side_effect=OSError('disk full')):
            result=import_files(self.store,source,self.pid,'scripts')
        self.assertEqual(result['done'],0)
        self.assertTrue(result['errors'])
        self.assertEqual(source.read_bytes(),before)
        self.assertEqual(list(destination.iterdir()),[sentinel])
        self.assertEqual(self.items(),[])

    def test_rejects_appdata_ancestor_and_recursive_source(self):
        destination=self.organize.folder_path(self.pid,'scripts')
        for source in (self.store.data_root,self.root,Path(self.project['root']),destination):
            with self.subTest(source=source),self.assertRaises(UserError):
                validate_source(self.store,source,destination)

    def test_top_level_reparse_and_nested_link_are_rejected_or_reported(self):
        from yingxu.store import has_link
        source=self.source('nested/a.md')
        with patch('yingxu.store.has_link',side_effect=lambda value:Path(value)==self.external/'nested' or has_link(value)):
            with self.assertRaises(UserError):
                import_files(self.store,self.external/'nested',self.pid,'scripts')
        with patch('yingxu.jobs.has_link',side_effect=lambda value:Path(value)==source or has_link(value)):
            result=import_files(self.store,self.external/'nested',self.pid,'scripts')
        self.assertEqual(result['done'],0)
        self.assertTrue(any('链接' in error for error in result['errors']))

    def test_hardlink_and_file_size_limits_report_errors(self):
        source=self.source()
        with patch('yingxu.file_import.MAX_FILE_BYTES',1):
            result=import_files(self.store,source,self.pid,'scripts')
        self.assertEqual(result['done'],0);self.assertTrue(result['errors'])
        alias=self.external/'alias.md'
        os.link(source,alias)
        with self.assertRaises(UserError):
            import_files(self.store,alias,self.pid,'scripts')

    def test_jobs_copy_never_registers_external_source_and_legacy_reference_kept(self):
        source=self.source()
        jobs=Jobs(self.store)
        copy_id=jobs.submit(self.pid,'scripts',[str(source)],mode='copy')['job_id']
        jobs.pool.shutdown(wait=True)
        result=jobs.get(copy_id)
        self.assertEqual(result['state'],'done');self.assertEqual(result['done'],1)
        self.assertEqual(len(self.store.sources(self.pid)),1)
        jobs=Jobs(self.store)
        reference_id=jobs.submit(self.pid,'references',[str(source)])['job_id']
        jobs.pool.shutdown(wait=True)
        self.assertEqual(jobs.get(reference_id)['done'],1)
        self.assertTrue(any(row['path']==str(source) for row in self.store.sources(self.pid)))

    def test_invalid_mode_and_copy_without_paths_rejected_before_enqueue(self):
        jobs=Jobs(self.store);self.addCleanup(jobs.pool.shutdown,wait=True)
        for kwargs in ({'mode':'invalid'}, {'mode':'copy'}):
            with self.subTest(kwargs=kwargs),self.assertRaises(UserError):
                jobs.submit(self.pid,**kwargs)
        self.assertEqual(jobs.jobs,{})

    def test_directory_scan_limit_is_reported(self):
        self.source('one.md');self.source('two.md')
        with patch('yingxu.file_import.MAX_ENTRIES',1):
            result=import_files(self.store,self.external,self.pid,'scripts')
        self.assertLessEqual(result['done'],1)
        self.assertTrue(any('条目过多' in error for error in result['errors']))

    def test_removed_destination_during_copy_is_reported_not_indexed(self):
        from yingxu.file_import import _copy_file
        source=self.source();before=source.read_bytes()
        selected=self.organize.create_folder(self.pid,'scripts','正在导入')
        def copy_then_remove(path,parent):
            copied=_copy_file(path,parent)
            with self.store.connection() as db:
                db.execute('UPDATE folders SET removed=1 WHERE id=?',(selected['id'],))
            return copied
        with patch('yingxu.file_import._copy_file',side_effect=copy_then_remove):
            result=import_files(self.store,source,self.pid,'scripts',selected['id'])
        self.assertEqual(result['done'],0)
        self.assertTrue(any('文件已复制' in message for message in result['errors']))
        self.assertEqual(source.read_bytes(),before)
        self.assertEqual(self.items(),[])


if __name__=='__main__':
    unittest.main()
