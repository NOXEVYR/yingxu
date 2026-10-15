"""Runtime portability and update checks; all fixtures are temporary."""
import importlib.machinery
import importlib.util
import hashlib
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from yingxu import __version__, runtime
from tools.prepare_runtime import prepare_ffmpeg


class RuntimeTests(unittest.TestCase):
    @staticmethod
    def write_frame_runtime(root):
        lock = {'ffmpeg': {'url': 'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.1-essentials_build.zip',
                           'file': 'ffmpeg.zip', 'bytes': 123, 'sha256': 'a' * 64}}
        (root / 'tools').mkdir(parents=True)
        (root / 'tools' / 'runtime-lock.json').write_text(json.dumps(lock), encoding='utf-8')
        binary_dir = root / 'runtime' / 'ffmpeg' / 'bin'
        binary_dir.mkdir(parents=True)
        binaries = {'ffmpeg.exe': b'fixture ffmpeg executable', 'ffprobe.exe': b'fixture ffprobe executable'}
        inventory = []
        for name, data in binaries.items():
            path = binary_dir / name
            path.write_bytes(data)
            inventory.append({'path': f'ffmpeg/bin/{name}', 'bytes': len(data),
                              'sha256': hashlib.sha256(data).hexdigest()})
        (root / 'runtime' / 'RUNTIME_MANIFEST.json').write_text(
            json.dumps({'sources': lock, 'files': inventory}), encoding='utf-8')
        return binary_dir

    def test_windows_frame_tools_require_the_locked_runtime_pair(self):
        with tempfile.TemporaryDirectory(prefix='yingxu-frame-runtime-') as temporary:
            root = Path(temporary).resolve()
            binary_dir = self.write_frame_runtime(root)
            with patch.object(runtime, 'APP_ROOT', root), patch.object(runtime.sys, 'platform', 'win32'), \
                    patch.object(runtime.shutil, 'which', side_effect=AssertionError('system PATH must not be used')):
                self.assertEqual(runtime.frame_tool_paths(), (str(binary_dir / 'ffmpeg.exe'), str(binary_dir / 'ffprobe.exe')))
                self.assertEqual(runtime.frame_tool_version(), '9.0.1')
                # A content change with a restored timestamp still fails its inventory hash.
                probe = binary_dir / 'ffprobe.exe'
                before = probe.stat()
                probe.write_bytes(b'fixture ffprobe executablf')
                os.utime(probe, ns=(before.st_atime_ns, before.st_mtime_ns))
                self.assertEqual(runtime.frame_tool_paths(), (None, None))

    def test_windows_frame_tools_fail_closed_for_non_object_metadata(self):
        for lock_value in (None, [], {'ffmpeg': None}, {'ffmpeg': []}):
            with self.subTest(lock=lock_value), tempfile.TemporaryDirectory(prefix='yingxu-frame-runtime-bad-lock-') as temporary:
                root = Path(temporary).resolve()
                self.write_frame_runtime(root)
                lock_path = root / 'tools' / 'runtime-lock.json'
                lock_path.write_text(json.dumps(lock_value), encoding='utf-8')
                with patch.object(runtime, 'APP_ROOT', root), patch.object(runtime.sys, 'platform', 'win32'):
                    self.assertEqual(runtime.frame_tool_paths(), (None, None))
                    self.assertIsNone(runtime.frame_tool_version())

        for manifest_value in (None, []):
            with self.subTest(manifest=manifest_value), tempfile.TemporaryDirectory(prefix='yingxu-frame-runtime-bad-manifest-') as temporary:
                root = Path(temporary).resolve()
                self.write_frame_runtime(root)
                manifest_path = root / 'runtime' / 'RUNTIME_MANIFEST.json'
                manifest_path.write_text(json.dumps(manifest_value), encoding='utf-8')
                with patch.object(runtime, 'APP_ROOT', root), patch.object(runtime.sys, 'platform', 'win32'):
                    self.assertEqual(runtime.frame_tool_paths(), (None, None))

    def test_windows_frame_tools_reject_a_manifest_from_another_source(self):
        with tempfile.TemporaryDirectory(prefix='yingxu-frame-runtime-source-') as temporary:
            root = Path(temporary).resolve()
            binary_dir = self.write_frame_runtime(root)
            manifest_path = root / 'runtime' / 'RUNTIME_MANIFEST.json'
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest['sources']['ffmpeg']['sha256'] = 'f' * 64
            manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
            with patch.object(runtime, 'APP_ROOT', root), patch.object(runtime.sys, 'platform', 'win32'), \
                    patch.object(runtime.shutil, 'which', return_value=str(binary_dir / 'ffprobe.exe')):
                self.assertEqual(runtime.frame_tool_paths(), (None, None))

    def test_prepare_runtime_keeps_only_a_matching_locked_ffmpeg_probe_pair(self):
        with tempfile.TemporaryDirectory(prefix='yingxu-prepare-ffmpeg-') as temporary:
            root = Path(temporary).resolve()
            archive = root / 'ffmpeg.zip'
            with __import__('zipfile').ZipFile(archive, 'w') as source:
                for name, data in (('ffmpeg.exe', b'ffmpeg'), ('ffprobe.exe', b'ffprobe'), ('ffplay.exe', b'ffplay')):
                    source.writestr('ffmpeg-9.0.1-essentials_build/bin/' + name, data)
            extraction, target = root / 'expanded', root / 'runtime' / 'ffmpeg'
            entry = {'url': 'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.1-essentials_build.zip'}
            def version_result(command, **kwargs):
                tool = Path(command[0]).name.removesuffix('.exe')
                return Mock(stdout=f'{tool} version 9.0.1-full_build source\n')
            with patch('tools.prepare_runtime.subprocess.run', side_effect=version_result) as run:
                prepare_ffmpeg(archive, extraction, target, entry)
            self.assertTrue((target / 'bin' / 'ffmpeg.exe').is_file())
            self.assertTrue((target / 'bin' / 'ffprobe.exe').is_file())
            self.assertFalse((target / 'bin' / 'ffplay.exe').exists())
            self.assertEqual(len(run.call_args_list), 2)
            self.assertEqual(Path(run.call_args_list[0].args[0][0]).parent,
                             Path(run.call_args_list[1].args[0][0]).parent)

    def test_prepare_runtime_rejects_missing_or_mismatched_ffprobe(self):
        for include_probe, probe_version in ((False, '9.0.1-full_build'), (True, '8.0.0-full_build')):
            with self.subTest(include_probe=include_probe, probe_version=probe_version), \
                    tempfile.TemporaryDirectory(prefix='yingxu-prepare-ffmpeg-invalid-') as temporary:
                root = Path(temporary).resolve(); archive = root / 'ffmpeg.zip'
                with __import__('zipfile').ZipFile(archive, 'w') as source:
                    source.writestr('ffmpeg-9.0.1-essentials_build/bin/ffmpeg.exe', b'ffmpeg')
                    if include_probe:
                        source.writestr('ffmpeg-9.0.1-essentials_build/bin/ffprobe.exe', b'ffprobe')
                def version_result(command, **kwargs):
                    tool = Path(command[0]).name.removesuffix('.exe')
                    version = '9.0.1-full_build' if tool == 'ffmpeg' else probe_version
                    return Mock(stdout=f'{tool} version {version} source\n')
                with patch('tools.prepare_runtime.subprocess.run', side_effect=version_result):
                    with self.assertRaises(ValueError):
                        prepare_ffmpeg(archive, root / 'expanded', root / 'runtime' / 'ffmpeg',
                            {'url': 'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.1-essentials_build.zip'})

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
