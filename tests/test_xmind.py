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
