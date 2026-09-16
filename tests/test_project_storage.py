import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yingxu.project_storage import ProjectStorage
from yingxu.settings import Settings
from yingxu.store import Store, UserError


class ProjectStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yingxu-storage-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'data', self.root / 'original')
        self.settings = Settings(self.store.data_root)
        self.target = self.root / 'chosen'
        self.target.mkdir()
        self.service = ProjectStorage(self.store, self.settings)

    def test_default_uses_supplied_root_without_writing_preferences(self):
        result = self.service.snapshot()
        self.assertEqual(result['root'], str(self.root / 'original'))
        self.assertEqual(result['configured_root'], '')
        self.assertEqual(result['project_count'], 0)
        self.assertFalse(self.settings.path.exists())

    def test_application_opens_settings_without_recreating_offline_root_or_touching_old_default(self):
        from server import Application
        old=self.store.create_project('仍可读取的旧项目')
        self.service.configure({'root':str(self.target)})
        offline=self.target.with_name('temporarily-offline')
        self.target.rename(offline)
        blocked_default=self.root/'former-default-is-not-a-directory'
        blocked_default.write_text('do not touch',encoding='utf-8')
        with patch('yingxu.skills.Path.home',return_value=self.root/'empty-home'):
            app=Application(self.store.data_root,blocked_default)
        try:
            self.assertEqual(app.bootstrap()['project_root'],str(self.target))
            self.assertFalse(app.project_storage.snapshot()['available'])
            self.assertFalse(self.target.exists())
            self.assertEqual(app.store.get_project(old['id'])['root'],old['root'])
            with self.assertRaises(UserError):app.store.create_project('离线不能新建')
            alternative=self.root/'alternative';alternative.mkdir()
            app.project_storage.configure({'root':str(alternative)})
            project=app.store.create_project('修复后新建')
            self.assertEqual(Path(project['root']).parent,alternative)
            self.assertEqual(blocked_default.read_text('utf-8'),'do not touch')
        finally:
            app.close()

    def test_new_root_persists_and_only_affects_new_projects(self):
        old = self.store.create_project('原有项目')
        original_files = sorted(str(path.relative_to(old['root'])) for path in Path(old['root']).rglob('*'))
        result = self.service.configure({'root': str(self.target)})
        self.assertEqual(result['existing_roots'], [{'id': old['id'], 'name': old['name'], 'root': old['root']}])
        self.assertEqual(result['affects'], 'new_projects')
        new = self.store.create_project('新项目')
        self.assertEqual(Path(new['root']).parent, self.target)
        self.assertEqual(self.store.get_project(old['id'])['root'], old['root'])
        self.assertEqual(sorted(str(path.relative_to(old['root'])) for path in Path(old['root']).rglob('*')), original_files)
        self.assertFalse(list(self.target.glob('.yingxu-storage-check-*')))
        self.assertEqual(json.loads(self.settings.path.read_text('utf-8'))['project_storage_root'], str(self.target))
        reopened = Store(self.store.data_root, self.root / 'original')
        with patch.object(ProjectStorage, '_probe', side_effect=AssertionError('startup must not write a probe')):
            ProjectStorage(reopened, Settings(reopened.data_root))
        self.assertEqual(reopened.project_root, self.target)

    def test_missing_saved_root_opens_settings_without_silent_fallback(self):
        self.service.configure({'root': str(self.target)})
        offline = self.root / 'offline'
        self.target.rename(offline)
        reopened = Store(self.store.data_root, self.root / 'original')
        service = ProjectStorage(reopened, self.settings)
        self.assertEqual(reopened.project_root, self.target)
        snapshot = service.snapshot()
        self.assertEqual(snapshot['root'], str(self.target))
        self.assertFalse(snapshot['available'])
        self.assertTrue(snapshot['error'])
        with self.assertRaises(UserError):
            service.validated_root()
        self.assertEqual(self.settings.get()['project_storage_root'], str(self.target))
        self.assertFalse(self.target.exists())
        offline.rename(self.target)
        self.assertTrue(service.snapshot()['available'])
        self.assertEqual(service.snapshot()['error'], '')
        self.assertEqual(service.validated_root(), self.target)

    def test_saved_unsafe_root_opens_for_correction_but_unavailable(self):
        self.settings.update({'project_storage_root': str(self.store.data_root)})
        service = ProjectStorage(self.store, self.settings)
        self.assertEqual(self.store.project_root, self.store.data_root)
        self.assertFalse(service.snapshot()['available'])
        with self.assertRaises(UserError):
            service.validated_root()
        service.configure({'root': str(self.target)})
        self.assertTrue(service.snapshot()['available'])

    def test_rejects_invalid_body_and_relative_network_roots(self):
        for body in ({}, {'root': str(self.target), 'move': True}, None, [],
                     {'root': ''}, {'root': 'relative/path'}, {'root': True},
                     {'root': '//server/share'}, {'root': '\\\\server\\share'},
                     {'root': str(self.target) + '\n'}, {'root': 'x' * 2049}):
            with self.subTest(body=body), self.assertRaises(UserError):
                self.service.configure(body)
        self.assertFalse(self.settings.path.exists())

    def test_rejects_missing_file_disk_root_and_whole_home(self):
        file = self.root / 'ordinary.txt'
        file.write_text('keep', encoding='utf-8')
        for value in (str(self.target / 'missing'), str(file), self.target.anchor, str(Path.home())):
            with self.subTest(value=value), self.assertRaises(UserError):
                self.service.configure({'root': value})
        self.assertEqual(file.read_text('utf-8'), 'keep')

    def test_rejects_data_overlap_and_existing_project_descendants(self):
        old = self.store.create_project('项目')
        child = Path(old['root']) / '20_Assets'
        nested_data = self.store.data_root / 'nested'
        nested_data.mkdir()
        for value in (self.root, self.store.data_root, nested_data, Path(old['root']), child):
            with self.subTest(value=value), self.assertRaises(UserError):
                self.service.configure({'root': str(value)})
        # The normal root contains existing projects and remains a valid destination.
        self.service.configure({'root': str(self.store.project_root)})

    def test_removed_project_remains_protected(self):
        old = self.store.create_project('回收项目')
        with self.store.connection() as db:
            db.execute('UPDATE projects SET removed=1 WHERE id=?', (old['id'],))
        with self.assertRaises(UserError):
            self.service.configure({'root': old['root']})

    def test_rejects_link_component(self):
        link = self.root / 'alias'
        try:
            link.symlink_to(self.target, target_is_directory=True)
        except OSError:
            self.skipTest('Symbolic links unavailable for this account')
        with self.assertRaises(UserError):
            self.service.configure({'root': str(link)})
        self.assertFalse(self.settings.path.exists())

    def test_failed_write_probe_keeps_preference_and_root(self):
        with patch.object(ProjectStorage, '_probe', side_effect=UserError('磁盘只读', 409)):
            with self.assertRaises(UserError):
                self.service.configure({'root': str(self.target)})
        self.assertEqual(self.store.project_root, self.root / 'original')
        self.assertFalse(self.settings.path.exists())

    def test_reparse_component_is_rejected_without_symlink_privilege(self):
        from yingxu.store import has_link
        with patch('yingxu.store.has_link', side_effect=lambda path: Path(path) == self.target or has_link(path)):
            with self.assertRaises(UserError):
                self.service.configure({'root': str(self.target)})
        self.assertFalse(self.settings.path.exists())

    def test_unreadable_chosen_directory_is_not_persisted(self):
        with patch('yingxu.project_storage.os.scandir', side_effect=PermissionError('access denied')):
            with self.assertRaises(UserError):
                self.service.configure({'root': str(self.target)})
        self.assertFalse(self.settings.path.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows protected and mapped drive validation')
    def test_windows_system_and_mapped_drive_rejected(self):
        with patch.dict(os.environ, {'WINDIR': str(self.target)}):
            with self.assertRaises(UserError):
                self.service.configure({'root': str(self.target)})
        with patch('ctypes.windll.kernel32.GetDriveTypeW', return_value=4):
            with self.assertRaises(UserError):
                self.service.configure({'root': str(self.target)})
        self.assertFalse(self.settings.path.exists())

    def test_probe_fsync_failure_cleans_only_owned_file(self):
        keep = self.target / '.yingxu-storage-check-existing.tmp'
        keep.write_text('keep', encoding='utf-8')
        with patch('yingxu.project_storage.os.fsync', side_effect=OSError('disk full')):
            with self.assertRaises(UserError):
                self.service.configure({'root': str(self.target)})
        self.assertEqual(list(self.target.iterdir()), [keep])
        self.assertEqual(keep.read_text('utf-8'), 'keep')
        self.assertFalse(self.settings.path.exists())

    def test_preference_write_failure_keeps_current_root(self):
        with patch.object(self.settings, 'update', side_effect=OSError('atomic write failed')):
            with self.assertRaises(UserError):
                self.service.configure({'root': str(self.target)})
        self.assertEqual(self.store.project_root, self.root / 'original')
        self.assertFalse(list(self.target.iterdir()))

    def test_other_settings_survive_storage_change(self):
        self.settings.update({'appearance_theme': 'pine', 'confirm_delete': False})
        self.service.configure({'root': str(self.target)})
        self.assertEqual(self.settings.get()['appearance_theme'], 'pine')
        self.assertFalse(self.settings.get()['confirm_delete'])


if __name__ == '__main__':
    unittest.main()
