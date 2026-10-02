"""Runtime portability and update checks; all fixtures are temporary."""
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yingxu import __version__, runtime


class RuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows short paths')
    def test_short_path_is_normalized_at_import_boundary(self):
        import ctypes
        from yingxu.store import clean_path
        from yingxu.paths import default_data_root
        with tempfile.TemporaryDirectory(prefix='yingxu-long-path-fixture-') as temporary:
            root = Path(temporary).resolve()
            buffer = ctypes.create_unicode_buffer(32768)
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.GetShortPathNameW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            kernel32.GetShortPathNameW.restype = ctypes.c_uint32
            length = kernel32.GetShortPathNameW(str(root), buffer, len(buffer))
            if length == 0 and ctypes.get_last_error() == 5:
                self.skipTest('GetShortPathNameW denied access to this synthetic path (WinError 5)')
            self.assertGreater(length, 0)
            self.assertEqual(clean_path(buffer.value), root)
            with patch.dict(os.environ, {'YINGXU_DATA_DIR': buffer.value}):
                self.assertEqual(default_data_root(), root)

    def test_bundled_ffmpeg_without_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            exe = root / 'runtime/ffmpeg/bin' / ('ffmpeg' if sys.platform == 'darwin' else 'ffmpeg.exe')
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b'fixture only')
            with patch.object(runtime, 'APP_ROOT', root), patch.dict(os.environ, {'PATH': ''}):
                self.assertEqual(runtime.ffmpeg_path(), str(exe))

    def test_source_checkout_can_use_system_tool(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(runtime, 'APP_ROOT', Path(temporary).resolve()), patch.object(runtime.shutil, 'which', return_value='system-tool'):
                self.assertEqual(runtime.ffmpeg_path(), 'system-tool')

    def test_missing_pillow_is_reported(self):
        runtime.image_support.cache_clear()
        try:
            with patch.dict('sys.modules', {'PIL': None}):
                self.assertFalse(runtime.image_support())
        finally:
            runtime.image_support.cache_clear()

    def test_old_background_is_rejected_without_termination(self):
        loader = importlib.machinery.SourceFileLoader('runtime_launcher_fixture', str(Path(__file__).resolve().parents[1] / 'launcher.pyw'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        launcher = importlib.util.module_from_spec(spec)
        loader.exec_module(launcher)
        with tempfile.TemporaryDirectory(prefix='yingxu-runtime-identity-') as temporary:
            root = Path(temporary).resolve()
            with patch.object(launcher, 'ROOT', root / 'program'), patch.object(launcher, 'DATA', root / 'data'):
                current = dict(app='yingxu', ok=True, version=__version__, build_revision=launcher.__build__,
                               program_id=launcher.instance_id(launcher.ROOT),
                               instance_id=launcher.instance_id(launcher.DATA))
                self.assertIsNone(launcher.require_current_service(None))
                self.assertIs(launcher.require_current_service(current), current)
                with self.assertRaisesRegex(RuntimeError, '旧版'):
                    launcher.require_current_service(dict(current, version='0.2.1'))

    def test_incomplete_background_identity_is_rejected_without_process_actions(self):
        loader = importlib.machinery.SourceFileLoader('runtime_incomplete_launcher_fixture', str(Path(__file__).resolve().parents[1] / 'launcher.pyw'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        launcher = importlib.util.module_from_spec(spec)
        loader.exec_module(launcher)
        with tempfile.TemporaryDirectory(prefix='yingxu-runtime-incomplete-') as temporary:
            root = Path(temporary).resolve()
            with patch.object(launcher, 'ROOT', root / 'program'), patch.object(launcher, 'DATA', root / 'data'):
                current = dict(app='yingxu', ok=True, version=__version__, build_revision=launcher.__build__,
                               program_id=launcher.instance_id(launcher.ROOT),
                               instance_id=launcher.instance_id(launcher.DATA))
                for field in ('build_revision', 'program_id'):
                    incomplete = {name: value for name, value in current.items() if name != field}
                    with self.subTest(missing=field), patch.object(launcher, 'health', return_value=incomplete), \
                            patch.object(launcher, 'acquire_mutex') as mutex, \
                            patch.object(launcher.subprocess, 'Popen') as process, \
                            patch.object(launcher.subprocess, 'run') as command, patch.object(launcher.os, 'kill') as kill:
                        with self.assertRaisesRegex(RuntimeError, '旧版'):
                            launcher.ensure_running(12345)
                        mutex.assert_not_called()
                        process.assert_not_called()
                        command.assert_not_called()
                        kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
