"""File identity contracts for native stat variants; no real user files."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from yingxu.disk_layout import file_stamp, identity


class DiskFileIdentityTests(unittest.TestCase):
    def info(self, **changes):
        fields = dict(st_dev=17, st_ino=29, st_nlink=1, st_size=41,
                      st_mtime_ns=123_000, st_ctime_ns=456_000,
                      st_birthtime=1_700_000_000.125)
        fields.update(changes)
        return SimpleNamespace(**fields)

    def path(self, info, is_file=True):
        path = Mock()
        path.stat.return_value = info
        path.is_file.return_value = is_file
        return path

    def test_macos_birthtime_keeps_rename_identity_and_residual_stamp(self):
        before = self.info()
        renamed = self.info(st_ctime_ns=987_000)
        with patch('yingxu.disk_layout.os', SimpleNamespace(name='posix')):
            old_identity = identity(self.path(before))
            new_identity = identity(self.path(renamed))
            self.assertIsNotNone(old_identity)
            self.assertEqual(new_identity, old_identity)
            self.assertEqual(file_stamp(None, before), file_stamp(None, renamed))
            self.assertEqual(file_stamp(None, renamed)[:3], new_identity)

    def test_native_nanoseconds_take_precedence_without_precision_loss(self):
        info = self.info(st_birthtime_ns=1_700_000_000_125_000_001)
        self.assertEqual(identity(self.path(info))[2], '1700000000125000001')
        self.assertEqual(file_stamp(None, info)[2], '1700000000125000001')

    def test_legacy_windows_ctime_is_creation_time_but_posix_ctime_is_not(self):
        info = self.info()
        del info.st_birthtime
        with patch('yingxu.disk_layout.os', SimpleNamespace(name='nt')):
            self.assertEqual(identity(self.path(info))[2], str(info.st_ctime_ns))
        with patch('yingxu.disk_layout.os', SimpleNamespace(name='posix')):
            self.assertIsNone(identity(self.path(info)))
            # Platforms without birthtime retain the conservative exact stamp.
            self.assertEqual(file_stamp(None, info)[2], str(info.st_ctime_ns))

    def test_replacement_inode_device_and_birthtime_are_distinct(self):
        original = self.info()
        for changes in ({'st_ino': 30}, {'st_dev': 18},
                        {'st_birthtime': original.st_birthtime + 1}):
            with self.subTest(changes=changes):
                replaced = self.info(**changes)
                self.assertNotEqual(identity(self.path(original)), identity(self.path(replaced)))
                self.assertNotEqual(file_stamp(None, original), file_stamp(None, replaced))

    def test_changed_content_stamp_is_not_an_unchanged_residual(self):
        original = self.info()
        for changes in ({'st_size': 42}, {'st_mtime_ns': 124_000}):
            with self.subTest(changes=changes):
                changed = self.info(**changes)
                self.assertEqual(identity(self.path(original)), identity(self.path(changed)))
                self.assertNotEqual(file_stamp(None, original), file_stamp(None, changed))

    def test_hardlinks_directories_and_missing_inode_cannot_follow_a_move(self):
        for info, is_file in ((self.info(st_nlink=2), True),
                              (self.info(st_ino=0), True), (self.info(), False)):
            with self.subTest(info=info, is_file=is_file):
                self.assertIsNone(identity(self.path(info, is_file)))


if __name__ == '__main__':
    unittest.main()
