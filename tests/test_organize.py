"""All fixtures are temporary; physical moves never involve user projects."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from yingxu.organize import Organize,MoveConflict
from yingxu.store import Store,UserError,now,uid
from yingxu.project_layout import category_paths
from yingxu.jobs import Jobs


class OrganizeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        self.store=Store(self.base/'data',self.base/'projects')
        self.project=self.store.create_project('整理测试')
        self.org=Organize(self.store)

    def folder(self,name='第01集',category='scripts',parent=None):
        return self.org.create_folder(self.project['id'],category,name,parent)

    def item(self,name='剧本',category='scripts',folder=None):
        return self.store.create_item({'project_id':self.project['id'],'category':category,
            'folder_id':folder,'name':name,'content':'# '+name+'\n\n原始内容雨夜。','metadata':{'prompt':'手写提示词'}})

    def reference(self,path,project=None,source_path=None):
        project=project or self.project
        source=self.store.register_source(project['id'],source_path or path,'references')
        self.store.index_files(source,[path])
        with self.store.connection() as db:
            iid=db.execute('SELECT id FROM items WHERE project_id=? AND path=?',(project['id'],str(path))).fetchone()[0]
        return source,self.store.get_item(iid)

    def test_nested_real_folders_and_direct_or_recursive_listing(self):
        first=self.folder();child=self.folder('场次01',parent=first['id'])
        root=self.item('分类根');inside=self.item('第一集',folder=first['id']);deep=self.item('场次剧本',folder=child['id'])
        self.assertTrue(Path(child['path']).is_dir())
        self.assertEqual(child['folder_path'],'第01集/场次01')
        self.assertEqual(Path(deep['path']).parent,Path(child['path']))
        self.assertEqual(self.store.list_items(self.project['id'],category='scripts')['total'],3)
        self.assertEqual(self.store.list_items(self.project['id'],category='scripts',folder='root')['items'][0]['id'],root['id'])
        direct=self.store.list_items(self.project['id'],category='scripts',folder=first['id'])
        self.assertEqual([item['id'] for item in direct['items']],[inside['id']])
        self.assertEqual(direct['items'][0]['folder_path'],'第01集')
        self.assertEqual(self.org.get_folder(first['id'])['count'],1)
        with self.assertRaises(UserError):self.folder(parent=child['id'],category='scenes')
        with self.assertRaises(UserError):self.folder()
        second=self.store.create_project('另一个项目')
        with self.assertRaises(UserError):self.org.create_folder(second['id'],'scripts','越界',first['id'])
        with self.assertRaises(UserError):self.store.list_items(second['id'],folder=child['id'])

    def test_owned_move_updates_other_project_references_and_keeps_original_bytes(self):
        item=self.item();old=Path(item['path']);before=old.read_bytes()
        second=self.store.create_project('共用资料的项目')
        _source,related=self.reference(old,second,old.parent)
        destination=self.folder('第02集','scenes')
        result=self.org.move_items([item['id']],'scenes',destination['id'])
        self.assertEqual(result['stats'],{'moved':1,'referenced':0,'unchanged':0,'copied':0})
        self.assertNotIn('metadata',result['items'][0], 'Move result stays small; UI merges with existing details.')
        new=Path(result['items'][0]['path'])
        self.assertFalse(old.exists());self.assertEqual(new.parent,Path(destination['path']))
        self.assertEqual(new.read_bytes(),before)
        updated=self.store.get_item(related['id'])
        self.assertEqual(self.store.resolve_item_path(updated),new)
        self.assertEqual(updated['category'],'references')
        self.assertEqual(self.store.get_item(item['id'])['metadata']['prompt'],'手写提示词')
        self.assertEqual(self.store.list_items(self.project['id'],category='scenes',folder=destination['id'])['total'],1)

    def test_external_organisation_and_import_assignment_keep_paths(self):
        path=self.base/'外部素材.md';path.write_text('外部原文',encoding='utf-8')
        source,item=self.reference(path);before=path.read_bytes()
        destination=self.folder('第03集','characters')
        with self.assertRaisesRegex(UserError,'外部引用'):
            self.org.move_items([item['id']],'characters',destination['id'])
        self.assertEqual(self.store.get_item(item['id'])['category'],'references')
        self.assertIsNone(self.store.get_item(item['id'])['folder_id'])
        # Explicit legacy organisation is separate from the physical move API.
        result=self.org.move_items([item['id']],'characters',destination['id'],move_files=False)
        self.assertEqual(result['stats']['referenced'],1)
        self.assertEqual(result['stats']['moved'],0)
        self.assertEqual(self.store.get_item(item['id'])['path'],str(path))
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(list(Path(destination['path']).iterdir()),[])
        # Batch assignment must never resolve/read every source file or return its metadata.
        with patch.object(self.store,'resolve_item_path',side_effect=AssertionError('unexpected file access')):
            assigned=self.org.assign_imported(source,[path],'scripts',None)
        self.assertEqual(assigned['items'],[])
        current=self.store.get_item(item['id']);self.assertIsNone(current['folder_id'])
        self.assertEqual(current['category'],'scripts');self.assertEqual(current['path'],str(path))

    def test_batch_collision_is_all_or_nothing_and_projects_are_isolated(self):
        a=self.item('A');b=self.item('B')
        dest=self.org.folder_path(self.project['id'],'scenes')
        collision=dest/Path(b['path']).name;collision.write_text('已有内容',encoding='utf-8')
        with self.assertRaises(UserError) as error:self.org.move_items([a['id'],b['id']],'scenes')
        self.assertEqual(error.exception.status,409)
        self.assertTrue(Path(a['path']).exists());self.assertTrue(Path(b['path']).exists())
        self.assertFalse((dest/Path(a['path']).name).exists())
        self.assertEqual(collision.read_text(encoding='utf-8'),'已有内容')
        other=self.store.create_project('不同项目')
        foreign=self.store.create_item({'project_id':other['id'],'name':'其他'})
        with self.assertRaises(UserError):self.org.move_items([a['id'],foreign['id']],'scenes')
        with self.assertRaises(UserError):self.org.delete_items([a['id'],foreign['id']])
        with self.assertRaises(UserError):self.org.move_items([a['id'],a['id']],'scenes')

    def test_aggregate_selection_skip_conflict_moves_disk_and_preserves_ids(self):
        folder=self.folder('女主')
        existing=self.item('图片节点 2',folder=folder['id'])
        conflict=self.item('图片节点 2','characters')
        move=self.item('另一张','scenes')
        original={i['id']:Path(i['path']).read_bytes() for i in (existing,conflict,move)}
        ids=[move['id'],conflict['id'],existing['id']]
        with self.assertRaises(MoveConflict) as caught:self.org.move_items(ids,'scripts',folder['id'])
        self.assertEqual(caught.exception.conflicts,[{'id':conflict['id'],'name':Path(conflict['path']).name,'source':conflict['path'],'target':existing['path']}])
        self.assertTrue(Path(move['path']).exists())
        result=self.org.move_items(ids,'scripts',folder['id'],conflict='skip')
        self.assertEqual(result['skipped_ids'],[conflict['id']])
        self.assertEqual(result['stats']['moved'],1);self.assertEqual(result['stats']['unchanged'],1)
        self.assertEqual(result['stats']['skipped'],1)
        self.assertEqual({r['id'] for r in result['items']},{move['id'],existing['id']})
        self.assertFalse(Path(move['path']).exists())
        for item in (existing,conflict,move):
            current=self.store.get_item(item['id'])
            self.assertEqual(Path(current['path']).read_bytes(),original[item['id']])
        self.assertEqual(self.store.get_item(conflict['id'])['path'],conflict['path'])
        # Rescan and reopen cannot restore the old physical location or duplicate IDs.
        source=self.store.sources(self.project['id'])[0]
        paths=[Path(self.store.get_item(iid)['path']) for iid in ids];self.store.index_files(source,paths)
        reopened=Store(self.store.data_root,self.store.project_root)
        self.assertEqual({r['id'] for r in reopened.list_items(self.project['id'])['items']},set(ids))
        self.assertEqual(Path(reopened.get_item(move['id'])['path']).parent,Path(folder['path']))

    def test_keep_both_reserves_batch_names_and_never_overwrites(self):
        folder=self.folder('目标','scenes');existing=self.item('同名','scenes',folder['id'])
        one=self.item('同名','scripts');two=self.item('同名','characters')
        result=self.org.move_items([one['id'],two['id']],'scenes',folder['id'],conflict='rename')
        self.assertEqual(result['stats']['moved'],2);self.assertEqual(result['stats']['renamed'],2)
        self.assertEqual({r['name'] for r in result['items']},{'同名 (2)','同名 (3)'})
        self.assertEqual(Path(existing['path']).read_text(encoding='utf-8'),'# 同名\n\n原始内容雨夜。')
        self.assertFalse(Path(one['path']).exists());self.assertFalse(Path(two['path']).exists())
        for item in result['items']:self.assertEqual(Path(item['path']).stem,item['name'])

    def test_skip_all_returns_no_false_destination_and_new_collision_is_preserved(self):
        one=self.item('A');destination=self.org.folder_path(self.project['id'],'scenes')
        occupied=destination/Path(one['path']).name;occupied.write_bytes(b'different')
        result=self.org.move_items([one['id']],'scenes',conflict='skip')
        self.assertEqual(result['items'],[]);self.assertEqual(result['stats']['moved'],0)
        self.assertEqual(self.store.get_item(one['id'])['path'],one['path'])
        self.assertEqual(occupied.read_bytes(),b'different')
        for invalid in (None,True,{},'overwrite'):
            with self.assertRaises(UserError):self.org.move_items([one['id']],'scenes',conflict=invalid)

    def test_stale_target_index_is_a_preflight_conflict_including_skip_and_rename(self):
        one=self.item('first');two=self.item('same')
        stale=self.item('same','scenes');Path(stale['path']).unlink()
        with patch('yingxu.organize._rename',side_effect=AssertionError('preflight must not move')):
            with self.assertRaises(MoveConflict):self.org.move_items([one['id'],two['id']],'scenes')
        skipped=self.org.move_items([one['id'],two['id']],'scenes',conflict='skip')
        self.assertEqual(skipped['skipped_ids'],[two['id']]);self.assertEqual(skipped['stats']['moved'],1)
        kept=self.org.move_items([two['id']],'scenes',conflict='rename')
        self.assertEqual(Path(kept['items'][0]['path']).name,'same (2).md')
        self.assertEqual(self.store.get_item(stale['id'])['path'],stale['path'])

    def test_mid_batch_filesystem_failure_rolls_back_paths_and_database(self):
        import yingxu.organize as module
        a=self.item('A');b=self.item('B');original=module._rename;calls=[]
        def fail_second(source,target):
            calls.append((source,target))
            if len(calls)==2:raise OSError('synthetic second move failure')
            return original(source,target)
        with patch.object(module,'_rename',side_effect=fail_second),self.assertRaises(OSError):
            self.org.move_items([a['id'],b['id']],'scenes')
        self.assertEqual(len(calls),3, 'The successful first move must be rolled back.')
        for item in (a,b):
            self.assertTrue(Path(item['path']).is_file())
            current=self.store.get_item(item['id']);self.assertEqual(current['path'],item['path'])
            self.assertEqual(current['category'],'scripts')
        self.assertEqual(list(self.org.folder_path(self.project['id'],'scenes').iterdir()),[])

    def test_folder_rename_updates_nested_and_removed_items_and_external_sources(self):
        parent=self.folder();child=self.folder('子场次',parent=parent['id'])
        item=self.item(folder=child['id']);old=Path(item['path']);original=old.read_bytes()
        second=self.store.create_project('引用项目');_source,other=self.reference(old,second)
        deletion=self.org.delete_folder(child['id'])
        renamed=self.org.rename_folder(parent['id'],'第一集修订')
        self.assertFalse(Path(parent['path']).exists())
        new=Path(renamed['path'])/'子场次'/old.name
        self.assertEqual(new.read_bytes(),original)
        self.assertEqual(self.store.resolve_item_path(self.store.get_item(other['id'])),new)
        self.org.restore(deletion['batch_id'])
        self.assertEqual(self.store.get_item(item['id'])['path'],str(new))
        self.assertEqual(self.org.get_folder(child['id'])['folder_path'],'第一集修订/子场次')

    def test_folder_rename_database_failure_rolls_back_the_real_directory(self):
        folder=self.folder();item=self.item(folder=folder['id']);original=self.org._repoint_directory
        def fail_after_database_change(*args):
            original(*args);raise sqlite3.OperationalError('synthetic database failure')
        with patch.object(self.org,'_repoint_directory',side_effect=fail_after_database_change),self.assertRaises(sqlite3.OperationalError):
            self.org.rename_folder(folder['id'],'不应生效')
        self.assertTrue(Path(item['path']).exists())
        self.assertEqual(self.org.get_folder(folder['id'])['name'],'第01集')
        self.assertEqual(self.store.get_item(item['id'])['path'],item['path'])

    def test_folder_restore_does_not_resurrect_an_earlier_deletion(self):
        parent=self.folder();child=self.folder('子场次',parent=parent['id'])
        first=self.item('本次资料',folder=parent['id']);nested=self.item('本次子资料',folder=child['id'])
        earlier=self.item('更早删除',folder=parent['id']);old_batch=self.org.delete_items([earlier['id']])
        batch=self.org.delete_folder(parent['id'])
        for item in (first,nested,earlier):self.assertTrue(Path(item['path']).exists())
        self.assertEqual(self.org.folders(self.project['id'])['total'],0)
        source=self.store.sources(self.project['id'])[0]
        self.assertEqual(self.store.index_files(source,[Path(first['path']),Path(earlier['path'])],restore_removed=True),(0,2))
        with self.assertRaises(UserError):self.org.restore(old_batch['batch_id'])
        self.org.restore(batch['batch_id'])
        self.assertEqual(self.store.get_item(first['id'])['folder_id'],parent['id'])
        self.assertEqual(self.store.get_item(nested['id'])['folder_id'],child['id'])
        with self.assertRaises(UserError):self.store.get_item(earlier['id'])
        self.org.restore(old_batch['batch_id']);self.assertEqual(self.store.get_item(earlier['id'])['id'],earlier['id'])

    def test_project_delete_restore_and_display_rename_preserve_files_and_prior_trash(self):
        folder=self.folder();active=self.item('仍有效',folder=folder['id']);earlier=self.item('已删除')
        self.org.delete_items([earlier['id']]);old_root=self.project['root']
        changed=self.org.update_project(self.project['id'],{'name':'仅显示名称改变','description':'更新简介'})
        self.assertEqual(changed['root'],old_root)
        batch=self.org.delete_project(self.project['id'])
        self.assertEqual(self.store.list_projects(),[])
        with self.assertRaises(UserError):self.store.get_project(self.project['id'])
        with self.assertRaises(UserError):self.store.read_content(active['id'])
        self.assertTrue(Path(active['path']).exists());self.assertTrue(Path(old_root).is_dir())
        self.org.restore(batch['batch_id'])
        self.assertEqual(self.store.get_project(self.project['id'])['name'],'仅显示名称改变')
        self.assertEqual(self.store.get_item(active['id'])['folder_id'],folder['id'])
        with self.assertRaises(UserError):self.store.get_item(earlier['id'])

    def test_legacy_removed_rows_migrate_into_restorable_batches(self):
        item=self.item('旧版本移出')
        with self.store.connection() as db:
            db.execute('UPDATE items SET removed=1,removed_batch=NULL WHERE id=?',(item['id'],))
            self.store._search_row(db,item['id'])
        reopened=Store(self.store.data_root,self.store.project_root);org=Organize(reopened)
        entry=next(e for e in org.trash()['entries'] if e['target_id']==item['id'])
        org.restore(entry['id']);self.assertEqual(reopened.get_item(item['id'])['id'],item['id'])
        self.assertTrue(Path(item['path']).exists())

    def test_folder_deleted_during_index_parse_is_not_repopulated(self):
        folder=self.folder();path=Path(folder['path'])/'尚未索引.md';path.write_text('新正文',encoding='utf-8')
        source=self.store.sources(self.project['id'])[0];original=self.store.inspect_file
        def delete_after_parse(value):
            result=original(value);self.org.delete_folder(folder['id']);return result
        with patch.object(self.store,'inspect_file',side_effect=delete_after_parse):
            self.assertEqual(self.store.index_files(source,[path]),(0,1))
        self.assertEqual(self.store.list_items(self.project['id'])['total'],0)
        self.assertTrue(path.exists())

    def test_many_folder_lookup_is_bounded_by_path_depth_and_tree_is_capped(self):
        stamp=now();base=category_paths(self.project)['scripts']
        with self.store.connection() as db:
            db.executemany('INSERT INTO folders(id,project_id,category,name,relative_path,created,updated) VALUES(?,?,?,?,?,?,?)',
                [(uid(),self.project['id'],'scripts',f'集{i:04d}',f'{base}/集{i:04d}',stamp,stamp) for i in range(5001)])
        path=Path(self.project['root'])/base/'集1234'/'剧本.md';path.parent.mkdir();path.write_text('大量目录测试',encoding='utf-8')
        original=Path.is_relative_to;calls=[]
        def tracked(value,*other):calls.append(1);return original(value,*other)
        source=self.store.sources(self.project['id'])[0]
        with patch.object(Path,'is_relative_to',tracked):self.store.index_files(source,[path])
        self.assertLess(len(calls),50,'Folder lookup must walk ancestors rather than every folder.')
        item=self.store.list_items(self.project['id'])['items'][0]
        self.assertEqual(item['folder_path'],'集1234')
        tree=self.org.folders(self.project['id']);self.assertEqual(tree['total'],5001)
        self.assertEqual(len(tree['folders']),5000);self.assertTrue(tree['truncated'])

    def test_trash_pagination_and_restore_idempotence(self):
        batches=[self.org.delete_items([self.item(str(i))['id']]) for i in range(3)]
        first=self.org.trash(self.project['id'],limit=2)
        self.assertEqual(first['total'],3);self.assertEqual(len(first['entries']),2);self.assertTrue(first['truncated'])
        last=self.org.trash(self.project['id'],limit=2,offset=2)
        self.assertEqual(len(last['entries']),1);self.assertFalse(last['truncated'])
        self.org.restore(batches[0]['id']);self.assertEqual(self.org.restore(batches[0]['id'])['count'],0)
        self.assertEqual(self.org.trash(self.project['id'])['total'],2)


if __name__=='__main__':unittest.main()
