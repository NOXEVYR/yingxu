"""Synthetic clipboard fixtures only; never access the user's clipboard."""
import io
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

from PIL import Image
from server import Application, Server
from yingxu import clipboard_files
from yingxu.store import UserError


class ClipboardImageReaderTests(unittest.TestCase):
    def windows_image(self, bitmap):
        with patch.object(clipboard_files.sys, 'platform', 'win32'), \
             patch.object(clipboard_files.os, 'name', 'nt'), \
             patch('PIL.ImageGrab.grabclipboard', return_value=bitmap):
            return clipboard_files.read_image_png()

    def test_windows_bitmap_is_png_with_alpha_and_pixels_preserved(self):
        bitmap = Image.new('RGBA', (3, 2), (17, 33, 99, 45))
        data = self.windows_image(bitmap)
        self.assertTrue(data.startswith(b'\x89PNG\r\n\x1a\n'))
        with Image.open(io.BytesIO(data)) as result:
            self.assertEqual(result.mode, 'RGBA')
            self.assertEqual(result.size, (3, 2))
            self.assertEqual(result.getpixel((1, 1)), (17, 33, 99, 45))

    def test_rgb_remains_rgb_and_no_bitmap_returns_none(self):
        data = self.windows_image(Image.new('RGB', (2, 2), (12, 34, 56)))
        with Image.open(io.BytesIO(data)) as result:
            self.assertEqual(result.mode, 'RGB')
            self.assertEqual(result.getpixel((0, 0)), (12, 34, 56))
        self.assertIsNone(self.windows_image(None))

    def test_pixel_and_encoded_size_limits_reject_without_large_allocation(self):
        with patch.object(clipboard_files, 'MAX_IMAGE_PIXELS', 3):
            with self.assertRaises(UserError) as caught:
                self.windows_image(Image.new('RGB', (2, 2)))
            self.assertEqual(caught.exception.status, 413)
        with patch.object(clipboard_files, 'MAX_IMAGE_BYTES', 12):
            with self.assertRaises(UserError) as caught:
                self.windows_image(Image.new('RGB', (2, 2)))
            self.assertEqual(caught.exception.status, 413)

    def test_clipboard_changed_to_files_requests_retry(self):
        with self.assertRaisesRegex(UserError, '已改变'):
            self.windows_image(['synthetic-file.png'])

    def test_busy_or_invalid_clipboard_has_actionable_error(self):
        for error in (OSError('busy'), ValueError('invalid image')):
            with patch.object(clipboard_files.sys, 'platform', 'win32'), \
                 patch.object(clipboard_files.os, 'name', 'nt'), \
                 patch('PIL.ImageGrab.grabclipboard', side_effect=error):
                with self.assertRaisesRegex(UserError, '重新复制图片'):
                    clipboard_files.read_image_png()

    def test_mac_tiff_and_png_normalize_without_reading_text(self):
        for format in ('TIFF', 'PNG'):
            image = Image.new('RGBA', (2, 3), (1, 2, 3, 40))
            stream = io.BytesIO(); image.save(stream, format=format); image.close()
            data = stream.getvalue()
            class Data(bytes):
                def length(self): return len(self)
            board = Mock()
            board.dataForType_.side_effect = lambda kind: Data(data) if kind == ('public.png' if format == 'PNG' else 'public.tiff') else None
            appkit = Mock(); appkit.NSPasteboard.generalPasteboard.return_value = board
            with patch.dict('sys.modules', {'AppKit': appkit}), patch.object(clipboard_files.sys, 'platform', 'darwin'):
                result = clipboard_files.read_image_png()
            with Image.open(io.BytesIO(result)) as decoded:
                self.assertEqual(decoded.format, 'PNG')
                self.assertEqual(decoded.getpixel((1, 1)), (1, 2, 3, 40))
            self.assertTrue(all(call.args[0] in ('public.png', 'public.tiff') for call in board.dataForType_.call_args_list))

    def test_mac_checks_payload_size_before_copying_and_rejects_invalid_bytes(self):
        value = Mock(); value.length.return_value = clipboard_files.MAX_IMAGE_BYTES + 1
        appkit = Mock(); appkit.NSPasteboard.generalPasteboard.return_value.dataForType_.return_value = value
        with patch.dict('sys.modules', {'AppKit': appkit}), patch.object(clipboard_files.sys, 'platform', 'darwin'):
            with self.assertRaises(UserError) as caught:
                clipboard_files.read_image_png()
            self.assertEqual(caught.exception.status, 413)
        with patch.object(clipboard_files.sys, 'platform', 'darwin'), patch.object(clipboard_files, '_mac_image_bytes', return_value=b'not an image'):
            with self.assertRaisesRegex(UserError, '重新复制图片'):
                clipboard_files.read_image_png()


class ClipboardImageImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='yingxu-clipboard-images-')
        self.root = Path(self.tmp.name).resolve()
        with patch('yingxu.skills.Path.home', return_value=self.root/'home'):
            self.app = Application(self.root/'data', self.root/'projects')
        self.project = self.app.store.create_project('合成剪贴板项目')
        self.pid = self.project['id']
        self.folder = self.app.organize.create_folder(self.pid, 'characters', '合成子目录')
        self.target = {'project_id': self.pid, 'category': 'characters', 'folder_id': self.folder['id']}
        stream = io.BytesIO()
        with Image.new('RGBA', (2, 2), (1, 2, 3, 44)) as image:
            image.save(stream, format='PNG')
        self.png = stream.getvalue()

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_image_paste_targets_exact_project_category_folder_and_never_overwrites(self):
        with patch.object(clipboard_files, 'read_files', return_value=[]), patch.object(clipboard_files, 'read_image_png', return_value=self.png):
            first = self.app.paste_clipboard(self.target)['items'][0]
            second = self.app.paste_clipboard(self.target)['items'][0]
        self.assertNotEqual(first['path'], second['path'])
        for item in (first, second):
            self.assertEqual(item['project_id'], self.pid)
            self.assertEqual(item['category'], 'characters')
            self.assertEqual(item['folder_id'], self.folder['id'])
            self.assertEqual(Path(item['path']).read_bytes(), self.png)
            self.assertEqual(Path(item['path']).suffix, '.png')

    def test_local_file_has_priority_over_bitmap_and_keeps_original(self):
        source = self.root/'source.md'; source.write_bytes(b'unchanged original')
        with patch.object(clipboard_files, 'read_files', return_value=[str(source)]), patch.object(clipboard_files, 'read_image_png') as image_reader:
            item = self.app.paste_clipboard(self.target)['items'][0]
        image_reader.assert_not_called()
        self.assertEqual(Path(item['path']).read_bytes(), b'unchanged original')
        self.assertEqual(source.read_bytes(), b'unchanged original')

    def test_empty_or_image_failure_writes_nothing(self):
        directory = self.app.organize.folder_path(self.pid, 'characters', self.folder['id'])
        before = list(directory.iterdir())
        with patch.object(clipboard_files, 'read_files', return_value=[]):
            with patch.object(clipboard_files, 'read_image_png', return_value=None):
                with self.assertRaisesRegex(UserError, '不是复制图片地址'):
                    self.app.paste_clipboard(self.target)
            with patch.object(clipboard_files, 'read_image_png', side_effect=UserError('图片过大', 413)):
                with self.assertRaisesRegex(UserError, '图片过大'):
                    self.app.paste_clipboard(self.target)
        self.assertEqual(list(directory.iterdir()), before)

    def test_invalid_target_is_rejected_before_any_clipboard_read(self):
        with patch.object(clipboard_files, 'read_files') as files, patch.object(clipboard_files, 'read_image_png') as images:
            with self.assertRaises(UserError):
                self.app.paste_clipboard({**self.target, 'folder_id': 'not-a-folder'})
            files.assert_not_called(); images.assert_not_called()

    def test_only_authorized_explicit_post_reads_clipboard(self):
        server = Server(('127.0.0.1', 0), self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def request(method, headers):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            connection.request(method, '/api/clipboard/paste', body=json.dumps(self.target) if method == 'POST' else None, headers={'Content-Type': 'application/json', **headers})
            response = connection.getresponse(); status = response.status; response.read(); connection.close()
            return status
        try:
            with patch.object(clipboard_files, 'read_files', return_value=[]) as files, patch.object(clipboard_files, 'read_image_png', return_value=self.png) as images:
                self.assertEqual(request('GET', {'X-YingXu-Token': self.app.token}), 404)
                self.assertEqual(request('POST', {}), 403)
                self.assertEqual(request('POST', {'X-YingXu-Token': self.app.token, 'Origin': 'https://example.invalid'}), 403)
                files.assert_not_called(); images.assert_not_called()
                self.assertEqual(request('POST', {'X-YingXu-Token': self.app.token}), 201)
                files.assert_called_once(); images.assert_called_once()
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
