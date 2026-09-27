import os
import tempfile
import unittest
from pathlib import Path

from yingxu.file_import import _validate_portable_component, import_files, validate_source
from yingxu.markdown_assets import MarkdownAssets
from yingxu.organize import Organize
from yingxu.store import Store, UserError


class ImportNameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yingxu-import-names-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root/'data', self.root/'projects')
        self.project = self.store.create_project('合成导入项目')
        self.pid = self.project['id']
        self.source = self.root/'外部素材'
        self.source.mkdir()

    def items(self):
        with self.store.connection() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM items WHERE project_id=?', (self.pid,))]

    def test_folder_import_preserves_long_image_name_and_markdown_link(self):
        stem = 'p' * 150
        filename = stem + '.png'
        note = self.source/'说明.md'
        image = self.source/filename
        note.write_text(f'![预览]({filename})\n', encoding='utf-8')
        image.write_bytes(b'not decoded by the link resolver')
        source_before = {path.name: path.read_bytes() for path in self.source.iterdir()}

        result = import_files(self.store, self.source, self.pid, 'scripts')

        self.assertEqual(result, {'done': 2, 'skipped': 0, 'errors': []})
        items = self.items()
        imported_note = next(item for item in items if item['name'] == '说明')
        imported_image = next(item for item in items if item['kind'] == 'image')
        self.assertEqual(Path(imported_image['path']).name, filename)
        self.assertEqual(Path(imported_note['path']).read_text(encoding='utf-8'),
                         f'![预览]({filename})\n')
        resolved = MarkdownAssets(self.store).resolve_file(imported_note['id'], filename)
        self.assertEqual(resolved['id'], imported_image['id'])
        self.assertEqual(source_before,
                         {path.name: path.read_bytes() for path in self.source.iterdir()})

    def test_long_name_collision_uses_existing_suffix_without_overwrite(self):
        filename = 'p' * 150 + '.png'
        source = self.source/filename
        source.write_bytes(b'first')
        first = import_files(self.store, source, self.pid, 'scripts')
        source.write_bytes(b'second')
        second = import_files(self.store, source, self.pid, 'scripts')

        destination = Organize(self.store).folder_path(self.pid, 'scripts')
        self.assertEqual(first['done'], 1)
        self.assertEqual(second['done'], 1)
        self.assertEqual((destination/filename).read_bytes(), b'first')
        self.assertEqual((destination/('p' * 150 + ' (2).png')).read_bytes(), b'second')

    def test_unportable_file_names_are_rejected_without_sanitizing(self):
        for name in ('bad:name.png', 'CON.txt', 'trailing .png '):
            with self.subTest(name=name), self.assertRaises(UserError) as raised:
                _validate_portable_component(name, '文件')
            self.assertIn('已拒绝导入', str(raised.exception))

    def test_validate_source_rejects_trailing_space_or_dot_before_cleaning(self):
        destination = Organize(self.store).folder_path(self.pid, 'scripts')
        existing = self.source/'good.png'
        original = b'keep this file'
        existing.write_bytes(original)
        for name in ('good.png ', 'good.png.'):
            with self.subTest(name=name), self.assertRaises(UserError) as raised:
                validate_source(self.store, str(self.source/name), destination)
            self.assertIn('已拒绝导入', str(raised.exception))
            self.assertEqual(existing.read_bytes(), original)

    def test_validate_source_allows_a_normal_directory_trailing_separator(self):
        destination = Organize(self.store).folder_path(self.pid, 'scripts')
        self.assertEqual(validate_source(self.store, str(self.source)+os.sep, destination),
                         self.source)

    def test_folder_name_over_limit_is_reported_without_truncation(self):
        name = 'd' * 101
        nested = self.source/name
        nested.mkdir()
        (nested/'note.md').write_text('# note', encoding='utf-8')

        result = import_files(self.store, self.source, self.pid, 'scripts')

        self.assertEqual(result['done'], 0)
        self.assertTrue(any('100字符限制' in message for message in result['errors']))
        imported_root = Organize(self.store).folder_path(self.pid, 'scripts')/'外部素材'
        self.assertFalse((imported_root/name[:100]).exists())




if __name__ == '__main__':
    unittest.main()
