"""Read files or bitmap clipboard formats only on an explicit paste request."""
import io
import os
import sys
from .store import UserError

MAX_IMAGE_PIXELS = 40000000
MAX_IMAGE_BYTES = 64 * 1024 * 1024


class _BoundedPNG(io.BytesIO):
    def write(self, value):
        if self.tell() + len(value) > MAX_IMAGE_BYTES:
            raise UserError('剪贴板图片最大支持 64 MiB。', 413)
        return super().write(value)


def _mac_image_bytes():
    from AppKit import NSPasteboard
    board = NSPasteboard.generalPasteboard()
    # Do not request string/HTML/URL representations: they are not image files.
    for kind in ('public.png', 'public.tiff'):
        value = board.dataForType_(kind)
        if value is not None:
            if value.length() > MAX_IMAGE_BYTES:
                raise UserError('剪贴板图片最大支持 64 MiB。', 413)
            return bytes(value)
    return None


def read_image_png():
    """Return a bounded PNG, or None; never interpret text as a file or URL."""
    try:
        from PIL import Image, ImageGrab
    except ImportError as error:
        raise UserError('当前运行环境缺少图片支持，请使用完整安装包或先保存图片再导入。') from error
    bitmap = None
    try:
        if sys.platform == 'darwin':
            payload = _mac_image_bytes()
            if payload is None:
                return None
            bitmap = Image.open(io.BytesIO(payload))
        elif os.name == 'nt':
            bitmap = ImageGrab.grabclipboard()
            if isinstance(bitmap, list):
                raise UserError('剪贴板内容已改变，请重新粘贴。', 409)
        else:
            raise UserError('当前系统不支持图片剪贴板，请先保存图片再导入。')
        if bitmap is None:
            return None
        if not isinstance(bitmap, Image.Image):
            raise UserError('剪贴板图片无法读取，请重新复制图片。')
        width, height = bitmap.size
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise UserError('剪贴板图片最大支持 4000 万像素。', 413)
        mode = 'RGBA' if 'A' in bitmap.getbands() or 'transparency' in bitmap.info else 'RGB'
        with bitmap.convert(mode) as converted, _BoundedPNG() as output:
            converted.save(output, format='PNG')
            return output.getvalue()
    except UserError:
        raise
    except Image.DecompressionBombError as error:
        raise UserError('剪贴板图片像素过大，请缩小图片后再粘贴。', 413) from error
    except (OSError, ValueError, RuntimeError) as error:
        raise UserError('剪贴板图片暂时无法读取，请重新复制图片后再粘贴。', 409) from error
    finally:
        if isinstance(bitmap, Image.Image):
            bitmap.close()


def read_files():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL('user32', use_last_error=True)
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        user.OpenClipboard.argtypes = [wintypes.HWND]
        user.OpenClipboard.restype = wintypes.BOOL
        user.GetClipboardData.argtypes = [wintypes.UINT]
        user.GetClipboardData.restype = wintypes.HANDLE
        user.CloseClipboard.argtypes = []
        shell.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        shell.DragQueryFileW.restype = wintypes.UINT
        if not user.OpenClipboard(None):
            raise UserError('剪贴板正被占用，请稍后再粘贴。', 409)
        try:
            handle = user.GetClipboardData(15)  # CF_HDROP, never interpret text as paths.
            count = shell.DragQueryFileW(handle, 0xffffffff, None, 0) if handle else 0
            if count > 128:raise UserError('一次最多粘贴 128 个文件。')
            result = []
            for index in range(count):
                length = shell.DragQueryFileW(handle, index, None, 0)
                value = ctypes.create_unicode_buffer(length + 1)
                shell.DragQueryFileW(handle, index, value, length + 1)
                result.append(value.value)
            return result
        finally:
            user.CloseClipboard()
    if sys.platform == 'darwin':
        from AppKit import NSPasteboard, NSPasteboardURLReadingFileURLsOnlyKey
        from Foundation import NSURL
        values = NSPasteboard.generalPasteboard().readObjectsForClasses_options_(
            [NSURL], {NSPasteboardURLReadingFileURLsOnlyKey: True}) or []
        if len(values) > 128:raise UserError('一次最多粘贴 128 个文件。')
        return [str(value.path()) for value in values if value.isFileURL()]
    raise UserError('当前系统不支持文件剪贴板，请使用导入或拖放。')
