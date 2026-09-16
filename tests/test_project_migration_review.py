"""Independent synthetic regressions for migration lifetime and recovery edges."""
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from yingxu.context import ContextExporter
from yingxu.organize import Organize
from yingxu.project_library import ProjectLibrary
from yingxu.project_migration import ProjectMigration
from yingxu.project_storage import ProjectStorage
from yingxu.settings import Settings
from yingxu.store import Store, UserError


class MigrationReviewTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='yingxu-migration-review-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.store=Store(self.root/'data',self.root/'projects')
        self.settings=Settings(self.store.data_root)
        self.storage=ProjectStorage(self.store,self.settings);self.store.project_storage=self.storage
        self.library=ProjectLibrary(self.store)
        self.project=self.store.create_project('回归项目');self.pid=self.project['id']
        self.organize=Organize(self.store)
        self.service=ProjectMigration(self.store,self.storage)
        self.destination=self.root/'destination';self.destination.mkdir()

    def note(self,name='文稿'):
        return self.store.create_item({'project_id':self.pid,'category':'scripts','name':name,'content':'keep original'})

    def preview(self, ids=None):
        return self.service.preview({'root':str(self.destination),'project_ids':ids or [self.pid]})

    def test_purged_tombstone_with_missing_original_does_not_block_migration(self):
        removed=self.note('已清理');kept=self.note('保留')
        batch=self.organize.delete_items([removed['id']])
        # Model a successful OS recycle using a disposable file, never a real recycler.
        Path(removed['path']).unlink()
        with self.store.connection() as db:
            db.execute('UPDATE trash_batches SET purged=1,recycle_started=1 WHERE id=?',(batch['id'],))
        preview=self.preview();self.service.execute({'token':preview['token']})
        self.assertEqual(Path(self.store.get_item(kept['id'])['path']).read_text(),'keep original')
        with self.store.connection() as db:
            row=db.execute('SELECT removed,removed_batch FROM items WHERE id=?',(removed['id'],)).fetchone()
            self.assertEqual(tuple(row),(1,batch['id']))
        with self.assertRaises(UserError):self.organize.restore(batch['id'])

    def test_missing_unpurged_trash_still_blocks_without_changing_sources(self):
        removed=self.note('可恢复');self.organize.delete_items([removed['id']]);Path(removed['path']).unlink()
        with self.assertRaises(UserError):self.preview()
        self.assertEqual(self.store.get_project(self.pid)['root'],self.project['root'])
        self.assertEqual(list(self.destination.iterdir()),[])

    def test_deleted_folder_can_restore_to_new_path_with_ids_preserved(self):
        folder=self.organize.create_folder(self.pid,'scripts','隐藏集')
        note=self.store.create_item({'project_id':self.pid,'category':'scripts','folder_id':folder['id'],'name':'剧情'})
        before=Path(note['path']).read_bytes();batch=self.organize.delete_folder(folder['id'])
        preview=self.preview();self.service.execute({'token':preview['token']})
        self.organize.restore(batch['id'])
        current=self.store.get_item(note['id'])
        self.assertEqual(current['folder_id'],folder['id'])
        self.assertEqual(Path(current['path']).read_bytes(),before)
        self.assertTrue(Path(current['path']).is_relative_to(self.destination))
        self.assertEqual(Path(note['path']).read_bytes(),before)

    def test_context_dirty_marker_commits_with_move_before_job_callback(self):
        exporter=ContextExporter(self.store)
        exporter.export(self.pid);exporter.close()
        with self.store.connection() as db:
            old=db.execute('SELECT revision,exported_revision FROM context_exports WHERE project_id=?',(self.pid,)).fetchone()
            self.assertEqual(old[0],old[1])
        preview=self.preview();self.service.execute({'token':preview['token']})
        # No Application.changed() is invoked: emulate stopping immediately after
        # the database commit. A new worker must still see a pending export.
        with self.store.connection() as db:
            row=db.execute('SELECT revision,exported_revision,failed_revision FROM context_exports WHERE project_id=?',(self.pid,)).fetchone()
            self.assertGreater(row[0],row[1]);self.assertGreater(row[0],row[2])

    def test_owned_file_from_narrow_directory_source_remains_authorized_after_reclassification(self):
        loose=Path(self.project['root'])/'loose';loose.mkdir()
        path=loose/'输入.md';path.write_text('keep imported content',encoding='utf-8')
        source=self.store.register_source(self.pid,str(loose),'scripts')
        self.store.index_files(source,[path])
        item=self.store.list_items(self.pid)['items'][0]
        preview=self.preview();self.service.execute({'token':preview['token']})
        current=self.store.get_item(item['id'])
        self.assertEqual(Path(current['path']).parent.name,'文本')
        self.assertEqual(self.store.resolve_item_path(current).read_text('utf-8'),'keep imported content')

    def test_cross_project_reference_remains_readable_from_retained_backup(self):
        note=self.note();other=self.store.create_project('引用项目')
        source=self.store.register_source(other['id'],note['path'],'references')
        self.store.index_files(source,[Path(note['path'])]);reference=self.store.list_items(other['id'])['items'][0]
        preview=self.preview([self.pid,other['id']]);self.service.execute({'token':preview['token']})
        current=self.store.get_item(reference['id'])
        self.assertEqual(current['path'],note['path'])
        self.assertEqual(self.store.resolve_item_path(current).read_text(),'keep original')
        self.assertTrue(any('引用' in message for message in preview['warnings']))


class ContextRelocationReviewTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='yingxu-context-relocation-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.store=Store(self.root/'data',self.root/'projects')
        self.project=self.store.create_project('上下文项目');self.pid=self.project['id']
        self.exporter=ContextExporter(self.store);self.addCleanup(self.exporter.close)
        self.old_result=self.exporter.export(self.pid)
        self.old=Path(self.project['root']);self.new=self.root/'relocated';shutil.copytree(self.old,self.new)

    def test_get_queued_behind_migration_reads_root_inside_export_lock(self):
        delegate=self.exporter._export_lock;entered=threading.Event()
        class ObservedLock:
            def __enter__(inner):entered.set();delegate.acquire();return inner
            def __exit__(inner,*args):delegate.release()
        self.exporter._export_lock=ObservedLock()
        with ThreadPoolExecutor(max_workers=1) as pool:
            delegate.acquire()
            try:
                result=pool.submit(self.exporter.get,self.pid)
                self.assertTrue(entered.wait(3))
                with self.store.connection() as db:db.execute('UPDATE projects SET root=? WHERE id=?',(str(self.new),self.pid))
            finally:delegate.release()
            current=result.result(timeout=5)
        self.assertEqual(Path(current['path']).parent,self.new/'.yingxu')

    def test_archive_uses_current_root_instead_of_passed_old_project_snapshot(self):
        old_manifest=self.old/'.yingxu/progress.json';before=old_manifest.read_bytes()
        with self.store.connection() as db:
            db.execute('UPDATE projects SET root=?,removed=1 WHERE id=?',(str(self.new),self.pid))
        self.exporter.archive(self.project)
        self.assertEqual(old_manifest.read_bytes(),before)
        current=json.loads((self.new/'.yingxu/progress.json').read_text('utf-8'))
        self.assertTrue(current['archived']);self.assertEqual(current['project']['root'],str(self.new))


if __name__=='__main__':unittest.main()
