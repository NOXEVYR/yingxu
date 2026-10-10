import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from yingxu import xmind
from yingxu.external import ExternalPreviews
from yingxu.jobs import Jobs
from yingxu.organize import Organize
from yingxu.store import Store, UserError


def archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as package:
        for name, value in entries:
            package.writestr(name, value)
    return output.getvalue()


class XmindTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yingxu-xmind-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / 'data', self.root / 'projects')
        self.project = self.store.create_project('合成思维导图项目')

    def item(self, **options):
        return self.store.create_item({'project_id': self.project['id'], 'category': 'scripts',
                                       'format': 'xmind', 'name': '剧情结构', **options})

    def test_new_file_has_standard_members_and_root_title_in_target_folder(self):
        folder = Organize(self.store).create_folder(self.project['id'], 'scripts', '第 1 集')
        item = self.item(folder_id=folder['id'], name='故事.xmind')
        self.assertEqual(item['kind'], 'xmind')
        self.assertEqual(item['folder_id'], folder['id'])
        with zipfile.ZipFile(item['path']) as package:
            self.assertEqual(package.testzip(), None)
            self.assertEqual(set(package.namelist()), {'content.json', 'metadata.json', 'manifest.json'})
        model = json.loads(self.store.read_content(item['id'])['content'])
        self.assertEqual(model[0]['rootTopic']['title'], '故事')
        self.assertEqual(self.store.list_items(self.project['id'], q='故事')['total'], 1)
        second = self.item(folder_id=folder['id'], name='故事.xmind')
        self.assertNotEqual(second['path'], item['path'])

    def test_save_keeps_unknown_fields_all_sheets_and_attachment_bytes(self):
        raw = xmind.create('中心')
        model = json.loads(xmind.read(raw)['content'])
        model[0]['rootTopic'].update(notes={'plain': {'content': '保留备注'}}, style={'id': 'theme'})
        model[0]['rootTopic']['children'] = {'summary': [{'id': 'summary', 'title': '保留概要'}]}
        model.append({'id': 'sheet2', 'title': '第二画布', 'rootTopic': {'id': 'root2', 'title': '另一中心'}})
        original = archive([('content.json', json.dumps(model)), ('metadata.json', b'{"extra":true}'),
                            ('resources/image.png', b'untouched image'), ('styles.json', b'custom styles')])
        model[0]['rootTopic']['title'] = '新的中心'
        model[0]['rootTopic']['children']['attached'] = [{'id': 'branch', 'title': '中文分支'}]
        edited = xmind.edit(original, json.dumps(model, ensure_ascii=False))
        with zipfile.ZipFile(io.BytesIO(edited)) as package:
            self.assertEqual(package.read('resources/image.png'), b'untouched image')
            self.assertEqual(package.read('metadata.json'), b'{"extra":true}')
            self.assertEqual(package.read('styles.json'), b'custom styles')
            self.assertIsNone(package.testzip())
        self.assertEqual(json.loads(xmind.read(edited)['content']), model)
        self.assertEqual(xmind.edit(edited, json.dumps(model)), edited)

    def test_atomic_save_backup_search_and_stale_etag(self):
        item = self.item(); path = Path(item['path']); before = path.read_bytes()
        opened = self.store.read_content(item['id']); model = json.loads(opened['content'])
        model[0]['rootTopic']['title'] = '雨夜重逢'
        saved = self.store.save_content(item['id'], {'etag': opened['etag'], 'content': json.dumps(model)})
        self.assertNotEqual(saved['etag'], opened['etag'])
        self.assertEqual(self.store.list_items(self.project['id'], q='雨夜重逢')['total'], 1)
        self.assertEqual(next((self.root / 'data/versions').rglob('*.xmind')).read_bytes(), before)
        with self.assertRaises(UserError) as error:
            self.store.save_content(item['id'], {'etag': opened['etag'], 'content': opened['content']})
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(json.loads(xmind.read(path.read_bytes())['content']), model)

    def test_annotation_batch_save_roundtrips_native_fields_and_preserves_unknown_data(self):
        item = self.item()
        path = Path(item['path'])
        model = json.loads(xmind.read(path.read_bytes())['content'])
        custom_marker = {'markerId': 'custom-review', 'groupId': 'vendorReview',
                         'vendor': {'color': '#123456', 'revision': 7}}
        model[0]['rootTopic']['children'] = {'attached': [
            {'id': 'first', 'title': '第一场', 'labels': ['旧标签'],
             'markers': [{'markerId': 'priority-4', 'groupId': 'priorityMarkers', 'vendor': 'keep'},
                         copy.deepcopy(custom_marker)],
             'notes': {'plain': {'content': '旧备注', 'vendor': {'language': 'zh-CN'}},
                       'vendor': {'revision': 2}},
             'href': 'https://example.invalid/old',
             'extensions': [{'provider': 'vendor.example', 'content': {'raw': ['原样', 42]}}]},
            {'id': 'second', 'title': '第二场',
             'markers': [{'markerId': 'task-half', 'groupId': 'taskMakers', 'vendor': 'keep'},
                         {'markerId': 'flag-red'}]},
            {'id': 'unselected', 'title': '复杂备注保持原样',
             'notes': {'plain': {'content': '富文本摘要'},
                       'html': {'content': '<p><strong>原始富文本</strong></p>'}},
             'markers': [copy.deepcopy(custom_marker)], 'labels': ['不修改']}
        ]}
        original_model = copy.deepcopy(model)
        retained_members = {
            'metadata.json': b'{ "dataStructureVersion": "2", "vendor": [ 7, true ] }\r\n',
            'manifest.json': b'{ "file-entries": {"content.json": {}, "resources/raw.bin": {}} }',
            'resources/raw.bin': bytes(range(256)) * 3,
            'styles.json': b'{"unknown-theme": { "preserve": true }}\n',
        }
        original = archive([('content.json', json.dumps(model, ensure_ascii=False)),
                            *retained_members.items()])
        path.write_bytes(original)
        opened = self.store.read_content(item['id'])
        edited = json.loads(opened['content'])
        first, second, _ = edited[0]['rootTopic']['children']['attached']
        first['labels'] = second['labels'] = ['主线', '待复核 🎬']
        first['markers'][0]['markerId'] = 'priority-1'
        first['markers'].append({'markerId': 'task-quarter'})
        second['markers'][0]['markerId'] = 'task-quarter'
        second['markers'].append({'markerId': 'priority-1'})
        first['notes']['plain']['content'] = '第一行中文\n第二行：保留换行\n'
        second['notes'] = {'plain': {'content': '批量设置后的独立备注'}}
        first['href'] = second['href'] = 'https://example.invalid/story?q=%E4%B8%AD%E6%96%87#scene'

        saved = self.store.save_content(item['id'], {'etag': opened['etag'],
                                                   'content': json.dumps(edited, ensure_ascii=False)})
        reopened = self.store.read_content(item['id'])
        self.assertEqual(reopened['etag'], saved['etag'])
        self.assertEqual(json.loads(reopened['content']), edited)
        actual_first, actual_second, actual_unselected = json.loads(reopened['content'])[0]['rootTopic']['children']['attached']
        self.assertEqual(actual_first['markers'][1], custom_marker)
        self.assertEqual(actual_first['notes']['vendor'], {'revision': 2})
        self.assertEqual(actual_first['notes']['plain']['vendor'], {'language': 'zh-CN'})
        self.assertEqual(actual_first['extensions'], original_model[0]['rootTopic']['children']['attached'][0]['extensions'])
        self.assertEqual(actual_second['markers'][1], {'markerId': 'flag-red'})
        self.assertEqual(actual_unselected, original_model[0]['rootTopic']['children']['attached'][2])
        self.assertEqual(next((self.root / 'data/versions').rglob('*.xmind')).read_bytes(), original)
        with zipfile.ZipFile(path) as package:
            self.assertIsNone(package.testzip())
            self.assertEqual(set(package.namelist()), {'content.json', *retained_members})
            for name, value in retained_members.items():
                with self.subTest(member=name):
                    self.assertEqual(package.read(name), value)

    def test_clearing_annotations_preserves_custom_markers_and_note_extensions(self):
        model = json.loads(xmind.read(xmind.create('中心'))['content'])
        topic = model[0]['rootTopic']
        custom_marker = {'markerId': 'task-vendor-review', 'groupId': 'vendorTasks',
                         'data': {'enabled': True, 'title': '未知标记'}}
        topic.update(labels=['待删除'], href='xmind:#other-topic',
                     markers=[{'markerId': 'priority-9'}, {'markerId': 'task-pause'}, custom_marker],
                     notes={'plain': {'content': '待删除备注'}, 'vendor': {'preserve': [1, 2, 3]}})
        original = archive([('content.json', json.dumps(model, ensure_ascii=False)),
                            ('metadata.json', b'{ "custom": true }\r\n'),
                            ('resources/note.bin', b'\x00\xffnote-resource')])
        edited_model = json.loads(xmind.read(original)['content'])
        edited_topic = edited_model[0]['rootTopic']
        del edited_topic['labels']
        del edited_topic['href']
        del edited_topic['notes']['plain']
        edited_topic['markers'] = [edited_topic['markers'][2]]
        edited = xmind.edit(original, json.dumps(edited_model, ensure_ascii=False))
        reopened = json.loads(xmind.read(edited)['content'])[0]['rootTopic']
        self.assertNotIn('labels', reopened)
        self.assertNotIn('href', reopened)
        self.assertEqual(reopened['markers'], [custom_marker])
        self.assertEqual(reopened['notes'], {'vendor': {'preserve': [1, 2, 3]}})
        with zipfile.ZipFile(io.BytesIO(edited)) as package:
            self.assertEqual(package.read('metadata.json'), b'{ "custom": true }\r\n')
            self.assertEqual(package.read('resources/note.bin'), b'\x00\xffnote-resource')
            self.assertIsNone(package.testzip())
        self.assertEqual(xmind.edit(edited, json.dumps(edited_model)), edited)

    def test_copy_and_reference_import_preserve_archive_not_extract_it(self):
        source = self.root / '外部.xmind'; raw = xmind.create('原文件'); source.write_bytes(raw)
        jobs = Jobs(self.store); self.addCleanup(lambda: jobs.pool.shutdown(wait=True))
        jid = jobs.submit(self.project['id'], 'references', [str(source)], mode='copy')
        jobs.pool.shutdown(wait=True)
        self.assertEqual(jobs.get(jid['job_id'])['state'], 'done')
        copied = self.store.list_items(self.project['id'])['items'][0]
        self.assertEqual(copied['kind'], 'xmind')
        self.assertEqual(Path(copied['path']).read_bytes(), raw)
        self.assertNotEqual(copied['path'], str(source))
        self.assertEqual(source.read_bytes(), raw)
        self.assertFalse(list(Path(self.project['root']).rglob('content.json')))
        registered = self.store.register_source(self.project['id'], str(source), 'references')
        self.store.index_files(registered, [source])
        self.assertEqual(self.store.list_items(self.project['id'])['total'], 2)

    def test_external_save_and_readonly_permissions_share_existing_protection(self):
        source = self.root / '外部.xmind'; raw = xmind.create('原主题'); source.write_bytes(raw)
        external = ExternalPreviews(self.root / 'data')
        iid = external.open({'paths': [str(source)]})['entries'][0]['id']
        content = external.detail(iid)['content']; model = json.loads(content['content'])
        model[0]['rootTopic']['title'] = '已编辑'
        saved = external.save(iid, {'etag': content['etag'], 'content': json.dumps(model)})
        self.assertTrue(saved['content']['editable'])
        self.assertEqual(next((self.root / 'data/external-versions').rglob('*.xmind')).read_bytes(), raw)
        with self.assertRaises(UserError): external.save(iid, {'etag': content['etag'], 'content': content['content']})

    def test_legacy_xml_preview_is_readonly_and_never_rewritten(self):
        xml = b'<xmap-content xmlns="urn:xmind:xmap:xmlns:content:2.0"><sheet id="s"><title>Old</title><topic id="r"><title>Root</title><children><topics type="attached"><topic id="t"><title>Child</title></topic></topics></children></topic></sheet></xmap-content>'
        raw = archive([('content.xml', xml)])
        preview = xmind.read(raw)
        self.assertFalse(preview['editable'])
        self.assertEqual(json.loads(preview['content'])[0]['rootTopic']['children']['attached'][0]['title'], 'Child')
        with self.assertRaises(UserError): xmind.edit(raw, preview['content'])

    def test_malformed_encrypted_traversal_duplicate_and_oversized_documents_rejected(self):
        content = xmind.read(xmind.create('中心'))['content']
        samples = [b'not a ZIP', archive([('content.json', 'no JSON')]), archive([('content.json', '[]')]),
                   archive([('content.json', content), ('../escape', b'x')]),
                   archive([('content.json', content), ('content.json', content)]),
                   archive([('content.xml', b'<!DOCTYPE x [<!ENTITY e "boom">]><x/>')]),
                   archive([('content.json', ' ' * (xmind.TEXT_LIMIT + 1))])]
        for raw in samples:
            with self.subTest(size=len(raw)), self.assertRaises(UserError): xmind.read(raw)
        model = json.loads(content); model[0]['rootTopic']['children'] = {'attached': [model[0]['rootTopic'].copy()]}
        with self.assertRaises(UserError): xmind.validate(json.dumps(model))
        with self.assertRaises(UserError): xmind.validate(json.dumps([{'rootTopic': {'id': 'r', 'title': 42}}]))

    def test_invalid_draft_leaves_file_and_versions_unchanged(self):
        item = self.item(); before = Path(item['path']).read_bytes(); opened = self.store.read_content(item['id'])
        with self.assertRaises(UserError): self.store.save_content(item['id'], {'etag': opened['etag'], 'content': '{}'})
        self.assertEqual(Path(item['path']).read_bytes(), before)
        self.assertFalse(list((self.root / 'data/versions').rglob('*.xmind')))


if __name__ == '__main__':
    unittest.main()
