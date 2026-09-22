import hashlib
from pathlib import Path
import tempfile
import unittest

from yingxu.store import Store
from yingxu.jobs import Jobs
from yingxu.organize import Organize
from yingxu.file_import import import_files
from yingxu.project_layout import category_paths


class MusicCategoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yingxu-music-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root/'data', self.root/'projects')
        self.project = self.store.create_project('音乐合成测试')
        self.pid = self.project['id']

    def test_music_directory_and_disk_sync_preserve_existing_categories(self):
        root = Path(self.project['root'])
        paths = category_paths(self.project)
        self.assertEqual(paths['music'], '音乐')
        self.assertTrue((root/'音乐').is_dir())
        sub = root/'音乐'/'配乐'; sub.mkdir()
        track = sub/'合成.mp3'; track.write_bytes(b'synthetic audio fixture')
        old = root/paths['references']/'原分类.mp3'; old.write_bytes(b'keep location')
        jobs = Jobs(self.store)
        job_id = jobs.submit(self.pid, owned_only=True)['job_id']
        jobs.pool.shutdown(wait=True)
        self.assertFalse(jobs.get(job_id)['errors'])
        result = self.store.list_items(self.pid, category='music')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['kind'], 'audio')
        self.assertEqual(result['items'][0]['folder_path'], '配乐')
        self.assertIn(str(old), [item['path'] for item in self.store.list_items(self.pid, category='references')['items']])
        self.assertTrue(old.is_file())

    def test_import_and_owned_drag_to_music_preserve_bytes_and_id(self):
        original = self.root/'输入.wav'; original.write_bytes(b'synthetic music')
        imported = import_files(self.store,original,self.pid,'references')
        self.assertFalse(imported['errors'])
        item = self.store.list_items(self.pid,category='references')['items'][0]
        before_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        moved = import_files(self.store,Path(item['path']),self.pid,'music',move_owned=True)
        self.assertFalse(moved['errors'])
        result = self.store.get_item(item['id'])
        self.assertEqual(result['category'], 'music')
        self.assertEqual(Path(result['path']).parent, Path(self.project['root'])/'音乐')
        self.assertEqual(hashlib.sha256(Path(result['path']).read_bytes()).hexdigest(),before_hash)
        self.assertTrue(original.is_file())
        self.assertFalse(Path(item['path']).exists())
        repeated = import_files(self.store,Path(result['path']),self.pid,'music',move_owned=True)
        self.assertFalse(repeated['errors'])
        self.assertEqual(self.store.list_items(self.pid)['total'],1)

    def test_legacy_music_path_does_not_relocate_legacy_text(self):
        legacy = dict(self.project, layout_version=0)
        self.assertEqual(category_paths(legacy)['music'],'20_Assets/音乐')
        self.assertEqual(category_paths(legacy)['scripts'],'00_Brief/剧本与文档')


if __name__ == '__main__':
    unittest.main()
