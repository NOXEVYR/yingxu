from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import quote

import test_markdown_assets as fixtures
from test_markdown_assets import PNG
from yingxu.store import UserError


class ObsidianImageTests(unittest.TestCase):
    setUp = fixtures.MarkdownAssetTests.setUp
    make_image = fixtures.MarkdownAssetTests.make_image

    def test_short_names_subpaths_and_source_preservation(self):
        image = self.image
        before = Path(self.note['path']).read_bytes()
        with self.assets.open_image(self.note['id'], quote(Path(image['path']).name), wiki=True) as f:
            self.assertEqual(f.read(), PNG)
        self.assertEqual(Path(self.note['path']).read_bytes(), before)
        self.assertTrue(self.assets.file_link(self.note['id'], image['id'])['image_markdown'].startswith('!['))
        with patch('yingxu.markdown_assets.MAX_IMAGE_BYTES', 1):
            self.assertNotIn('image_markdown', self.assets.file_link(self.note['id'], image['id']))

    def test_duplicate_names_require_subpath_unless_local(self):
        name = Path(self.image['path']).name
        (Path(self.project['root'])/'attachments').mkdir()
        second = self.make_image(self.project, 'attachments/'+name)
        # Project root is an explicit preferred location.
        with self.assets.open_image(self.note['id'], quote(name), wiki=True) as f:
            self.assertEqual(f.read(), PNG)
        with self.assets.open_image(self.note['id'], 'attachments/'+quote(name), wiki=True) as f:
            self.assertEqual(f.read(), PNG)
        with self.store.connection() as db:
            db.execute('UPDATE items SET removed=1 WHERE id=?', (self.image['id'],))
        (Path(self.project['root'])/'other').mkdir()
        self.make_image(self.project, 'other/'+name)
        with self.assertRaises(UserError) as error:
            with self.assets.open_image(self.note['id'], quote(name), wiki=True): pass
        self.assertEqual(error.exception.status, 409)

    def test_wiki_never_falls_back_to_disk_other_projects_or_traversal(self):
        self.make_image(self.store.create_project('其他'), 'only-other.png')
        (Path(self.project['root'])/'unindexed.png').write_bytes(PNG)
        for value in ['only-other.png', 'unindexed.png', '../'+Path(self.image['path']).name,
                      'https://example.invalid/a.png', '%2e%2e/a.png']:
            with self.subTest(value=value), self.assertRaises(UserError):
                with self.assets.open_image(self.note['id'], quote(value, safe='/%'), wiki=True): pass
        with self.assertRaises(UserError):
            with self.assets.open_image(self.note['id'], quote(Path(self.image['path']).name)): pass
