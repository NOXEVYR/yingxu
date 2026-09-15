"""Bounded cleanup of synthetic native-browser profiles after host exit."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('desktop_build_cleanup', Path(__file__).resolve().parents[1] / 'desktop/build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class DesktopBuildCleanupTests(unittest.TestCase):
    def fixture(self):
        return Path(tempfile.gettempdir()).resolve() / 'yingxu-lifecycle-synthetic-cleanup'

    def locked(self):
        error = OSError('synthetic sharing violation')
        error.winerror = 32
        return error

    def test_retries_only_transient_sharing_violations(self):
        with patch.object(build.shutil, 'rmtree', side_effect=[self.locked(), None]) as remove, patch.object(build.time, 'sleep') as sleep:
            build.cleanup_integration_fixture(self.fixture())
            self.assertEqual(remove.call_count, 2)
            sleep.assert_called_once_with(0.2)

    def test_persistent_lock_fails_after_bounded_wait(self):
        with patch.object(build.shutil, 'rmtree', side_effect=self.locked()) as remove, patch.object(build.time, 'sleep') as sleep:
            with self.assertRaises(OSError):
                build.cleanup_integration_fixture(self.fixture())
            self.assertEqual(remove.call_count, 26)
            self.assertEqual(sleep.call_count, 25)

    def test_other_io_errors_are_not_hidden(self):
        with patch.object(build.shutil, 'rmtree', side_effect=PermissionError('synthetic access denied')) as remove, patch.object(build.time, 'sleep') as sleep:
            with self.assertRaises(PermissionError):
                build.cleanup_integration_fixture(self.fixture())
            remove.assert_called_once()
            sleep.assert_not_called()

    def test_rejects_paths_outside_owned_fixture_before_removal(self):
        with patch.object(build.shutil, 'rmtree') as remove:
            for value in (Path(tempfile.gettempdir()).resolve(), self.fixture() / 'nested', self.fixture().parent / 'unrelated'):
                with self.assertRaises(ValueError):
                    build.cleanup_integration_fixture(value)
            remove.assert_not_called()
