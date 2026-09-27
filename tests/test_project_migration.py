import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yingxu.project_library import ProjectLibrary
from yingxu.project_migration import ProjectMigration, recover_pending
from yingxu.project_storage import ProjectStorage
from yingxu.disk_layout import identity
from yingxu.cross_project import move_items
from yingxu.organize import Organize
from yingxu.settings import Settings
from yingxu.store import CATEGORIES, Store, UserError


class ProjectMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='yingxu-migrate-tests-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.store=Store(self.root/'data',self.root/'old-projects')
        self.settings=Settings(self.store.data_root)
        self.storage=ProjectStorage(self.store,self.settings)
        self.store.project_storage=self.storage
        self.library=ProjectLibrary(self.store)
        self.migration=ProjectMigration(self.store,self.storage)
        self.target=self.root/'destination';self.target.mkdir()

    def project(self,name='项目',legacy=False,folder_id=None):
        project=self.store.create_project(name,folder_id=folder_id)
        if legacy:
            root=Path(project['root'])
            for child in root.iterdir():
                if child.is_dir():child.rmdir()
            for _,relative in CATEGORIES.values():(root/relative).mkdir(parents=True,exist_ok=True)
            (root/'30_Workflows').mkdir()
            with self.store.connection() as db:db.execute('UPDATE projects SET layout_version=0 WHERE id=?',(project['id'],))
            project=self.store.get_project(project['id'])
        return project

    def preview(self,*projects):
        return self.migration.preview({'root':str(self.target),'project_ids':[project['id'] for project in projects]})

    def test_preview_readonly_then_migration_repoints_and_retains_original(self):
        top=self.library.create_folder({'name':'作品'})
        folder=self.library.create_folder({'name':'练习','parent_id':top['id']})
        project=self.project(legacy=True,folder_id=folder['id'])
        note=self.store.create_item({'project_id':project['id'],'category':'scripts','name':'文稿','content':'# 完整内容'})
        old=Path(note['path']);before=old.read_bytes()
        extra=Path(project['root'])/'private.bin';extra.write_bytes(b'unknown kept')
        preview=self.preview(project)
        self.assertEqual(list(self.target.iterdir()),[])
        self.assertFalse((self.store.data_root/'project-migrations').exists())
        result=self.migration.execute({'token':preview['token']})
        new=self.store.get_project(project['id'])
        self.assertEqual(Path(new['root']),self.target/'作品'/'练习'/'项目')
        self.assertEqual(new['layout_version'],1)
        current=self.store.get_item(note['id'])
        self.assertEqual(Path(current['path']),Path(new['root'])/'文本'/'文稿.md')
        self.assertEqual(Path(current['path']).read_bytes(),before)
        self.assertEqual(old.read_bytes(),before)
        self.assertEqual((Path(new['root'])/'private.bin').read_bytes(),b'unknown kept')
        self.assertEqual(self.store.project_root,self.target)
        self.assertEqual(self.settings.get()['project_storage_root'],str(self.target))
        self.assertTrue(Path(result['database_backup']).is_file())
        self.assertEqual(json.loads(Path(result['recovery_manifest']).read_text('utf-8'))['state'],'complete')
        self.assertTrue(result['originals_retained'])
        self.assertEqual(list(self.target.glob('.yingxu-migration-*')),[])

    def test_first_sync_after_migration_follows_external_rename_without_new_item(self):
        project=self.project()
        note=self.store.create_item({'project_id':project['id'],'category':'scripts','name':'文稿','content':'原文'})
        opened=self.store.read_content(note['id'])
        self.store.save_content(note['id'],{'etag':opened['etag'],'content':'带历史的正文'})
        if identity(Path(note['path'])) is None:self.skipTest('stable file identity unavailable')
        preview=self.preview(project);self.migration.execute({'token':preview['token']})
        migrated=Path(self.store.get_item(note['id'])['path'])
        with self.store.connection() as db:
            tracked=db.execute('SELECT path,device,file_id,birth FROM disk_file_identities WHERE item_id=?',(note['id'],)).fetchone()
        self.assertEqual(tracked['path'],str(migrated))
        self.assertEqual(tuple(tracked)[1:],identity(migrated))
        renamed=migrated.with_name('新名字.md');migrated.rename(renamed)
        source=next(row for row in self.store.sources(project['id']) if not row['is_file'])
        self.store.index_files(source,[renamed])
        self.assertEqual(self.store.get_item(note['id'])['path'],str(renamed))
        self.assertEqual(self.store.list_items(project['id'])['total'],1)
        self.assertEqual(len(self.store.get_item(note['id'],True)['versions']),1)

    def test_migration_repoints_cross_project_residual_guard(self):
        first=self.project('来源');second=self.project('中间');third=self.project('目标')
        note=self.store.create_item({'project_id':first['id'],'category':'scripts','name':'文稿','content':'原文'})
        first_copy=Path(note['path']);unlink=Path.unlink
        def busy(path,*args,**kwargs):
            if path in (first_copy,second_copy):raise PermissionError('synthetic busy source')
            return unlink(path,*args,**kwargs)
        second_copy=None
        with patch.object(Path,'unlink',busy):
            first_result=move_items(Organize(self.store),[note['id']],second['id'],'scripts')
            second_copy=Path(self.store.get_item(note['id'])['path'])
            second_result=move_items(Organize(self.store),[note['id']],third['id'],'scripts')
        self.assertTrue(first_result['warnings']);self.assertTrue(second_result['warnings'])
        renamed=first_copy.with_name('改名的残留.md');first_copy.rename(renamed)
        preview=self.preview(first,second,third);self.migration.execute({'token':preview['token']})
        moved_first=Path(self.store.get_project(first['id'])['root'])/'文本'/'改名的残留.md'
        moved_second=Path(self.store.get_project(second['id'])['root'])/'文本'/'文稿.md'
        self.assertTrue(moved_first.is_file());self.assertTrue(moved_second.is_file())
        with self.store.connection() as db:
            residuals={row['source_project_id']:row['path'] for row in db.execute(
                'SELECT source_project_id,path FROM cross_project_residuals WHERE item_id=?',(note['id'],))}
        self.assertEqual(residuals,{first['id']:str(moved_first),second['id']:str(moved_second)})
        source=next(row for row in self.store.sources(first['id']) if not row['is_file'])
        self.store.index_files(source,[moved_first])
        self.assertEqual(self.store.list_items(first['id'])['total'],0)
        source=next(row for row in self.store.sources(second['id']) if not row['is_file'])
        self.store.index_files(source,[moved_second])
        self.assertEqual(self.store.list_items(second['id'])['total'],0)

    def test_internal_markdown_links_updated_only_in_copy_and_external_preserved(self):
        project=self.project(legacy=True)
        target=self.store.create_item({'project_id':project['id'],'category':'characters','name':'角色','content':'role'})
        note=self.store.create_item({'project_id':project['id'],'category':'scripts','name':'文稿','content':'[角色](../../20_Assets/角色/角色.md)\n'})
        old=Path(note['path']);before=old.read_bytes()
        external=self.root/'external.md';external.write_text('external',encoding='utf-8')
        source=self.store.register_source(project['id'],str(external),'references');self.store.index_files(source,[external])
        preview=self.preview(project)
        self.assertEqual(preview['projects'][0]['external_references'],1)
        self.migration.execute({'token':preview['token']})
        updated=self.store.get_item(note['id'])
        content=Path(updated['path']).read_text('utf-8')
        self.assertIn('../%E8%A7%92%E8%89%B2/',content)
        self.assertEqual(old.read_bytes(),before)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT path FROM items WHERE source_id=?',(source['id'],)).fetchone()[0],str(external))

    def test_stale_files_catalogue_and_used_token_rejected(self):
        project=self.project();note=self.store.create_item({'project_id':project['id'],'category':'scripts','name':'文稿'})
        preview=self.preview(project);Path(note['path']).write_text('changed',encoding='utf-8')
        with self.assertRaises(UserError):self.migration.execute({'token':preview['token']})
        with self.assertRaises(UserError):self.migration.execute({'token':preview['token']})
        preview=self.preview(project)
        with self.store.connection() as db:db.execute('UPDATE projects SET description=? WHERE id=?',('changed',project['id']))
        with self.assertRaises(UserError):self.migration.execute({'token':preview['token']})
        self.assertEqual(self.store.get_project(project['id'])['root'],project['root'])
        self.assertEqual(list(self.target.iterdir()),[])

    def test_all_active_projects_required_and_new_project_invalidates_preview(self):
        first=self.project('一');second=self.project('二')
        with self.assertRaises(UserError):self.preview(first)
        preview=self.preview(first,second);self.project('三')
        with self.assertRaises(UserError):self.migration.execute({'token':preview['token']})

    def test_removed_item_paths_and_ids_preserved(self):
        project=self.project(legacy=True)
        note=self.store.create_item({'project_id':project['id'],'category':'scripts','name':'删除文稿'})
        with self.store.connection() as db:db.execute('UPDATE items SET removed=1 WHERE id=?',(note['id'],))
        preview=self.preview(project);self.migration.execute({'token':preview['token']})
        with self.store.connection() as db:row=dict(db.execute('SELECT * FROM items WHERE id=?',(note['id'],)).fetchone())
        self.assertEqual(row['removed'],1);self.assertTrue(Path(row['path']).is_file())
        self.assertEqual(Path(row['path']).parent.name,'文本')
        self.assertTrue(Path(note['path']).is_file())

    def test_missing_indexed_and_hardlinked_files_refused(self):
        project=self.project();note=self.store.create_item({'project_id':project['id'],'category':'scripts','name':'文稿'})
        original=Path(note['path']);contents=original.read_bytes();original.unlink()
        with self.assertRaises(UserError):self.preview(project)
        original.write_bytes(contents);alias=self.root/'alias.md';os.link(original,alias)
        with self.assertRaises(UserError):self.preview(project)

    def test_settings_failure_rolls_back_catalogue_and_keeps_both_originals(self):
        first=self.project('一');second=self.project('二')
        preview=self.preview(first,second)
        actual_update=self.settings.update
        def fail_new(values):
            if values.get('project_storage_root')==str(self.target):raise OSError('synthetic settings write failure')
            return actual_update(values)
        with patch.object(self.settings,'update',side_effect=fail_new):
            with self.assertRaises(OSError):self.migration.execute({'token':preview['token']})
        self.assertEqual(self.store.get_project(first['id'])['root'],first['root'])
        self.assertEqual(self.store.get_project(second['id'])['root'],second['root'])
        self.assertEqual(self.settings.get()['project_storage_root'],'')
        self.assertEqual(self.store.project_root,self.root/'old-projects')
        self.assertTrue(Path(first['root']).is_dir());self.assertTrue(Path(second['root']).is_dir())

    def test_existing_target_is_never_overwritten(self):
        project=self.project();occupied=self.target/project['name'];occupied.mkdir()
        keep=occupied/'keep.bin';keep.write_bytes(b'keep')
        preview=self.preview(project)
        self.assertNotEqual(preview['projects'][0]['target_root'],str(occupied))
        self.migration.execute({'token':preview['token']})
        self.assertEqual(keep.read_bytes(),b'keep')

    def test_interrupted_settings_before_db_commit_recovered_without_file_deletion(self):
        project=self.project();recovery=self.store.data_root/'project-migrations';recovery.mkdir()
        manifest=recovery/'operation.json'
        record={'state':'committing','old_configured_root':'','old_effective_root':str(self.store.project_root),'new_root':str(self.target),
                'projects':[{'id':project['id'],'source_root':project['root'],'target_root':str(self.target/'copy')}]}
        manifest.write_text(json.dumps(record),encoding='utf-8')
        self.settings.update({'project_storage_root':str(self.target)});self.store.project_root=self.target
        result=recover_pending(self.store,self.settings)
        self.assertEqual(result,[str(manifest)])
        self.assertEqual(self.settings.get()['project_storage_root'],'')
        self.assertEqual(self.store.project_root,self.root/'old-projects')
        self.assertTrue(Path(project['root']).is_dir())

    def test_interrupted_after_db_commit_finishes_preference(self):
        project=self.project();preview=self.preview(project);result=self.migration.execute({'token':preview['token']})
        path=Path(result['recovery_manifest']);record=json.loads(path.read_text('utf-8'));record['state']='committing'
        path.write_text(json.dumps(record),encoding='utf-8')
        self.settings.update({'project_storage_root':''});self.store.project_root=self.root/'old-projects'
        recover_pending(self.store,self.settings)
        self.assertEqual(self.settings.get()['project_storage_root'],str(self.target))
        self.assertEqual(self.store.project_root,self.target)

    def test_corrupt_copy_leaves_database_and_sources_unchanged(self):
        from yingxu.project_migration import _digest
        project=self.project();note=self.store.create_item({'project_id':project['id'],'category':'scripts','name':'完整','content':'keep'})
        source=Path(note['path']);before=source.read_bytes();preview=self.preview(project)
        def bad_copy(path):
            return 'bad' if any(part.startswith('.yingxu-migration-') for part in Path(path).parts) else _digest(path)
        with patch('yingxu.project_migration._digest',side_effect=bad_copy):
            with self.assertRaises(UserError):self.migration.execute({'token':preview['token']})
        self.assertEqual(source.read_bytes(),before)
        self.assertEqual(self.store.get_project(project['id'])['root'],project['root'])
        self.assertEqual(list(self.target.iterdir()),[])
        self.assertEqual(self.settings.get()['project_storage_root'],'')

    def test_change_during_copy_is_rejected_before_atomic_switch(self):
        project=self.project();note=self.store.create_item({'project_id':project['id'],'category':'scripts','name':'变更','content':'old'})
        preview=self.preview(project);changed=False
        def change_original(progress):
            nonlocal changed
            if isinstance(progress,dict) and progress.get('completed') and not changed:
                changed=True;Path(note['path']).write_text('concurrent user change',encoding='utf-8')
        with self.assertRaises(UserError):self.migration.execute({'token':preview['token']},change_original)
        self.assertEqual(Path(note['path']).read_text('utf-8'),'concurrent user change')
        self.assertEqual(self.store.get_project(project['id'])['root'],project['root'])
        self.assertEqual(list(self.target.iterdir()),[])

    def test_expired_token_and_overlapping_roots_rejected(self):
        project=self.project();preview=self.preview(project)
        self.migration.tokens[preview['token']]['created']-=601
        with self.assertRaises(UserError):self.migration.execute({'token':preview['token']})
        second=self.project('嵌套')
        nested=Path(project['root'])/'child';nested.mkdir()
        with self.store.connection() as db:db.execute('UPDATE projects SET root=? WHERE id=?',(str(nested),second['id']))
        with self.assertRaises(UserError):self.preview(project,second)

    def test_more_than_twenty_active_projects_can_preview(self):
        projects=[self.project('项目'+str(number)) for number in range(21)]
        preview=self.preview(*projects)
        self.assertEqual(len(preview['projects']),21)
        self.assertEqual(list(self.target.iterdir()),[])

    def test_corrupt_folder_path_cannot_escape_migration_target(self):
        from yingxu.organize import Organize
        project=self.project();folder=Organize(self.store).create_folder(project['id'],'scripts','子目录')
        self.store.create_item({'project_id':project['id'],'category':'scripts','folder_id':folder['id'],'name':'文稿'})
        with self.store.connection() as db:db.execute('UPDATE folders SET relative_path=? WHERE id=?',('../../outside',folder['id']))
        with self.assertRaises(UserError):self.preview(project)
        self.assertEqual(list(self.target.iterdir()),[])

    def test_combined_entry_limit_blocks_before_any_writes(self):
        from yingxu.project_migration import _tree
        first=self.project('一');second=self.project('二')
        single_count=len(_tree(first['root']))
        self.assertLessEqual(len(_tree(second['root'])),single_count)
        with patch('yingxu.project_migration.MAX_MIGRATION_ENTRIES',single_count):
            with self.assertRaises(UserError):self.preview(first,second)
        self.assertEqual(list(self.target.iterdir()),[])
        self.assertFalse((self.store.data_root/'project-migrations').exists())
        self.assertFalse(self.settings.path.exists())
        self.assertEqual(self.migration.tokens,{})

    def test_only_latest_preview_snapshot_is_retained(self):
        project=self.project();first=self.preview(project);second=self.preview(project)
        self.assertEqual(list(self.migration.tokens),[second['token']])
        with self.assertRaises(UserError):self.migration.execute({'token':first['token']})
        self.assertEqual(list(self.target.iterdir()),[])


if __name__=='__main__':unittest.main()
