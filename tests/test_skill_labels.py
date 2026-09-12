import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from yingxu.skills import SkillLibrary
from yingxu.store import Store, UserError


class SkillLabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.home=self.root/'home'
        self.source=self.home/'.codex/skills/demo/SKILL.md';self.source.parent.mkdir(parents=True)
        self.source.write_text('---\nname: 中文规范\n---\n保留原文',encoding='utf-8')
        self.home_patch=patch('yingxu.skills.Path.home',return_value=self.home);self.home_patch.start();self.addCleanup(self.home_patch.stop)
        self.store=Store(self.root/'data',self.root/'projects');self.library=SkillLibrary(self.store)

    def test_preset_removal_restore_and_custom_filter_preserve_sources(self):
        before=self.source.read_bytes();skill=self.library.list()['skills'][0]
        project=self.store.create_project('测试')['id'];self.library.bind(project,skill['id'],True)
        with patch.object(self.library,'refresh',side_effect=AssertionError('labels must not scan')):
            result=self.library.remove_filter_label('codex')
            self.assertTrue(next(g for g in result['groups'] if g['id']=='codex')['hidden'])
            self.assertEqual(self.library.list()['total'],1)
            result=self.library.add_filter_label({'label':'编剧工具','source_ids':['codex','codex']})
            label=next(g for g in result['groups'] if g['label']=='编剧工具')
            self.assertEqual(label['count'],1)
            selected=self.library.list(q='中文',project_id=project,source=label['id'])
            self.assertEqual(selected['total'],1);self.assertTrue(selected['skills'][0]['bound'])
            self.library.remove_filter_label(label['id']);self.library.remove_filter_label('codex',restore=True)
            self.assertFalse(next(g for g in self.library.source_list()['groups'] if g['id']=='codex')['hidden'])
        self.assertEqual(self.source.read_bytes(),before);self.assertTrue(self.library.locations['codex']['enabled'])

    def test_labels_persist_and_validate_without_creating_unknown_sources(self):
        result=self.library.add_filter_label({'label':'我的标签','source_ids':['codex']})
        label=next(g for g in result['groups'] if g['label']=='我的标签')
        self.library.remove_filter_label('claude')
        reopened=SkillLibrary(self.store)
        self.assertEqual(reopened.list(source=label['id'])['total'],1)
        self.assertTrue(next(g for g in reopened.source_list()['groups'] if g['id']=='claude')['hidden'])
        for data in [{'label':'Codex','source_ids':['codex']},{'label':'a'*41,'source_ids':['codex']},{'label':'新标签','source_ids':[]},{'label':'新标签','source_ids':['unknown']}]:
            with self.subTest(data=data),self.assertRaises(UserError):reopened.add_filter_label(data)
        self.assertNotIn('unknown',reopened.locations)
