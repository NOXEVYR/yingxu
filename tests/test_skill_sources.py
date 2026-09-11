import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yingxu.skills import SkillLibrary
from yingxu.store import Store, UserError
from yingxu.skill_sources import DiscoveryBudget, scan_roots


class SkillSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.home=self.root/'home';self.home.mkdir()
        self.home_patch=patch('yingxu.skills.Path.home',return_value=self.home)
        self.home_patch.start();self.addCleanup(self.home_patch.stop)
        self.store=Store(self.root/'data',self.root/'projects')
        self.project=self.store.create_project('合成项目')

    def skill(self,relative,name='合成技能'):
        path=self.home/relative/'SKILL.md';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('---\nname: '+name+'\n---\n不可修改的原稿',encoding='utf-8')
        return path

    def test_verified_roots_source_filters_and_counts(self):
        self.skill('.dsh/skills/a','DSH 测试');self.skill('.workbuddy/skills/b','WorkBuddy 测试')
        self.skill('.agents/skills/c','共享测试')
        library=SkillLibrary(self.store)
        self.assertEqual(library.list()['total'],3)
        selected=library.list(q='测试',source='dsh',source_id='dsh')
        self.assertEqual(selected['total'],1);self.assertEqual(selected['all_total'],3)
        self.assertEqual(selected['skills'][0]['source_group'],'dsh')
        self.assertFalse(selected['skills'][0]['editable'])
        self.assertEqual(library.list(source='workbuddy',source_id='dsh')['total'],0)
        sources={s['id']:s for s in selected['sources']}
        self.assertEqual(sources['claude']['status'],'missing')
        self.assertFalse((self.home/'.claude/skills').exists())
        self.assertEqual(sources['dsh']['count'],1)
        self.assertTrue(sources['dsh']['readonly'])

    def test_zcode_observed_layout_is_scanned_without_marketplace_duplicates(self):
        self.skill('.zcode/cli/plugins/cache/official/plugin/1.0/skills/demo')
        self.skill('.zcode/cli/plugins/marketplaces/official/plugin/skills/demo')
        self.skill('.zcode/cli/plugins/cache/arbitrary/deep/file')
        library=SkillLibrary(self.store)
        rows=library.list(source='zcode')['skills'];self.assertEqual(len(rows),1)
        self.assertIn('缓存',rows[0]['source_label'])

    def test_workbuddy_uses_installed_manifest_not_old_cache(self):
        current=self.skill('.workbuddy/plugins/cache/official/demo/2.0/skills/demo','当前')
        self.skill('.workbuddy/plugins/cache/official/demo/1.0/skills/demo','旧版')
        manifest=self.home/'.workbuddy/plugins/installed_plugins.json'
        manifest.write_text(json.dumps({'plugins':{'demo':[{'installPath':str(current.parents[2])}]}}))
        library=SkillLibrary(self.store)
        rows=library.list(source='workbuddy')['skills']
        self.assertEqual([row['name'] for row in rows],['当前'])
        self.assertEqual(rows[0]['source_id'],'workbuddy_plugins')
        self.assertEqual(current.read_text(encoding='utf-8').splitlines()[-1],'不可修改的原稿')

    def test_manifest_escape_and_invalid_json_do_not_scan_untrusted_files(self):
        outside=self.skill('private/a')
        cache=self.home/'.workbuddy/plugins/cache';cache.mkdir(parents=True)
        manifest=cache.parent/'installed_plugins.json'
        for raw in ('{broken',json.dumps({'plugins':{'bad':[{'installPath':str(outside.parent.parent)}]}})):
            manifest.write_text(raw)
            library=SkillLibrary(self.store)
            self.assertEqual(library.list()['total'],0)
            self.assertEqual(next(s for s in library.source_list()['sources'] if s['id']=='workbuddy_plugins')['status'],'error')

    def test_custom_toggle_remove_readd_preserves_original_binding_and_id(self):
        path=self.skill('chosen/demo');before=path.read_bytes()
        library=SkillLibrary(self.store)
        added=library.add_source({'path':str(path.parent.parent),'label':'自选'})
        source=next(s for s in added['sources'] if s['custom']);row=added['skills'][0]
        library.bind(self.project['id'],row['id'],True)
        disabled=library.update_source(source['id'],{'enabled':False})
        self.assertEqual(disabled['total'],0)
        self.assertFalse(library.bound_skills(self.project['id'])[0]['available'])
        with self.assertRaises(UserError):library.get(row['id'])
        reopened=SkillLibrary(self.store)
        enabled=reopened.update_source(source['id'],{'enabled':True})
        self.assertEqual(enabled['skills'][0]['id'],row['id'])
        reopened.update_source(source['id'],{},remove=True)
        again=reopened.add_source({'path':str(path.parent.parent)})
        self.assertEqual(again['skills'][0]['id'],row['id'])
        self.assertTrue(reopened.list(project_id=self.project['id'])['skills'][0]['bound'])
        with self.assertRaises(UserError):reopened.save(row['id'],{'etag':row['etag'],'content':'overwrite'})
        self.assertEqual(path.read_bytes(),before)

    def test_overlap_has_one_id_but_both_source_memberships(self):
        path=self.skill('.dsh/skills/nested/demo')
        library=SkillLibrary(self.store);original=library.list()['skills'][0]
        result=library.add_source({'path':str(path.parents[1]),'label':'嵌套'})
        custom=next(s for s in result['sources'] if s['custom'])
        self.assertEqual(result['total'],1);self.assertEqual(result['all_total'],1)
        self.assertEqual(len(result['skills'][0]['source_ids']),2)
        self.assertEqual(library.list(source_id=custom['id'])['skills'][0]['id'],original['id'])
        library.update_source('dsh',{'enabled':False})
        self.assertEqual(library.get(original['id'])['path'],str(path))
        self.assertEqual(library.list(source='custom')['total'],1)

    def test_interrupted_refresh_keeps_all_unscanned_records_and_bindings(self):
        self.skill('.dsh/skills/a');self.skill('.workbuddy/skills/b')
        library=SkillLibrary(self.store);ids={row['id'] for row in library.list()['skills']}
        for key in ids:library.bind(self.project['id'],key,True)
        with patch('yingxu.skills.MAX_SCAN_ENTRIES',0):result=library.refresh()
        self.assertTrue(result['truncated']);self.assertEqual({row['id'] for row in result['skills']},ids)
        self.assertTrue(all(row['available'] for row in library.bound_skills(self.project['id'])))

    def test_completed_missing_source_only_marks_its_own_records_missing(self):
        dsh=self.skill('.dsh/skills/a');self.skill('.workbuddy/skills/b')
        library=SkillLibrary(self.store);dsh.unlink();library.refresh()
        self.assertEqual(library.list(source='dsh')['total'],0)
        self.assertEqual(library.list(source='workbuddy')['total'],1)

    def test_read_failure_preserves_old_index_and_project_binding(self):
        path=self.skill('.dsh/skills/a')
        library=SkillLibrary(self.store);skill=library.list()['skills'][0]
        library.bind(self.project['id'],skill['id'],True)
        path.write_text('正文已被外部更新，需要重新读取',encoding='utf-8')
        with patch('yingxu.skills._read',side_effect=PermissionError('synthetic sharing lock')):
            result=library.refresh()
        self.assertEqual(result['total'],1)
        self.assertEqual(result['skills'][0]['id'],skill['id'])
        self.assertTrue(library.bound_skills(self.project['id'])[0]['available'])
        self.assertEqual(next(s for s in result['sources'] if s['id']=='dsh')['status'],'truncated')

    def test_discovery_shares_global_deadline_and_preserves_old_index(self):
        self.skill('.dsh/skills/a');library=SkillLibrary(self.store)
        old=library.list()['skills'][0]['id']
        calls=[]
        def exhausted(location,check,deadline=None):
            calls.append(deadline)
            raise DiscoveryBudget('synthetic deadline')
        with patch('yingxu.skills.scan_roots',side_effect=exhausted):result=library.refresh()
        self.assertEqual(result['skills'][0]['id'],old)
        self.assertTrue(result['truncated']);self.assertTrue(calls)
        self.assertTrue(all(value==calls[0] for value in calls))
        with patch('yingxu.skill_sources.time.monotonic',return_value=10),self.assertRaises(DiscoveryBudget):
            scan_roots({'path':str(self.home),'mode':'plugins'},lambda path:Path(path),deadline=9)

    def test_hardlink_source_switch_preserves_identity_and_binding(self):
        path=self.skill('.dsh/skills/demo')
        alias=self.home/'.agents/skills/demo/SKILL.md';alias.parent.mkdir(parents=True)
        try:os.link(path,alias)
        except OSError:self.skipTest('hard links unavailable')
        library=SkillLibrary(self.store);row=library.list()['skills'][0]
        library.bind(self.project['id'],row['id'],True)
        result=library.update_source('dsh',{'enabled':False})
        self.assertEqual(result['total'],1);self.assertEqual(result['skills'][0]['id'],row['id'])
        self.assertEqual(library.get(row['id'])['path'],str(alias))
        self.assertTrue(library.list(project_id=self.project['id'])['skills'][0]['bound'])
        self.assertFalse(result['skills'][0]['editable'])
        self.assertEqual(path.read_bytes(),alias.read_bytes())

    def test_concurrent_registry_reads_and_source_toggles_are_consistent(self):
        self.skill('.dsh/skills/demo');library=SkillLibrary(self.store)
        def toggle():
            for enabled in [False,True]*5:library.update_source('dsh',{'enabled':enabled})
        def read():
            for _ in range(20):
                result=library.list(source='dsh')
                dsh=next(s for s in result['sources'] if s['id']=='dsh')
                self.assertEqual(result['total'],dsh['count'])
                self.assertEqual(result['all_total'],result['total'])
                library.source_list()
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures=[pool.submit(toggle),pool.submit(read),pool.submit(read)]
            for future in futures:future.result(timeout=10)

    def test_custom_input_is_bounded_and_builtin_cannot_be_removed(self):
        path=self.skill('chosen/demo');library=SkillLibrary(self.store)
        for value in ({'path':str(self.home)},{'path':'relative'},{'path':str(self.home.anchor)},
                      {'path':str(path.parent),'execute':'script'},{'path':str(path.parent),'label':'x'*81}):
            with self.subTest(value=value),self.assertRaises(UserError):library.add_source(value)
        with self.assertRaises(UserError):library.update_source('yingxu',{'enabled':False})
        with self.assertRaises(UserError):library.update_source('dsh',{},remove=True)
        with self.assertRaises(UserError):library.update_source('dsh',{'enabled':'yes'})
        library.add_source({'path':str(path.parent)})
        with self.assertRaises(UserError):library.add_source({'path':str(path.parent)})

    def test_symlink_source_is_rejected(self):
        target=self.home/'real';target.mkdir();link=self.home/'alias'
        try:link.symlink_to(target,target_is_directory=True)
        except OSError:self.skipTest('symbolic-link permission unavailable')
        library=SkillLibrary(self.store)
        with self.assertRaises(UserError):library.add_source({'path':str(link)})
