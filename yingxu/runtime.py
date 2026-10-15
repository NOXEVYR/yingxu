"""Resolve private media tools without modifying the machine's PATH or Python."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import sys
import threading

APP_ROOT = Path(__file__).resolve().parents[1]
_FRAME_TOOLS_LOCK = threading.Lock()
_FRAME_TOOLS_CACHE = {}


def _stat_identity(path):
    try:
        if path.is_symlink():
            return None
        value = path.stat()
        if not stat.S_ISREG(value.st_mode):
            return None
        change_time = windows_change_time(path) if sys.platform == 'win32' else None
        return (value.st_dev, value.st_ino, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns, change_time)
    except OSError:
        return None


def windows_change_time(path):
    """Read FILE_BASIC_INFO.ChangeTime; return None when Windows cannot report it."""
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class FileBasicInfo(ctypes.Structure):
            _fields_ = [('CreationTime', ctypes.c_longlong), ('LastAccessTime', ctypes.c_longlong),
                        ('LastWriteTime', ctypes.c_longlong), ('ChangeTime', ctypes.c_longlong),
                        ('FileAttributes', wintypes.DWORD)]

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
            wintypes.LPVOID, wintypes.DWORD]
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateFileW(str(path), 0x80, 0x1 | 0x2 | 0x4, None, 3, 0, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            return None
        try:
            info = FileBasicInfo()
            if not kernel32.GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
                return None
            return info.ChangeTime
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _validated_windows_frame_tools(root, identities):
    key = (str(root), identities)
    cacheable = sys.platform != 'win32' or all(identity[-1] is not None for identity in identities)
    if cacheable:
        with _FRAME_TOOLS_LOCK:
            if key in _FRAME_TOOLS_CACHE:
                return _FRAME_TOOLS_CACHE[key]
    result = None
    try:
        lock_path = root / 'tools' / 'runtime-lock.json'
        manifest_path = root / 'runtime' / 'RUNTIME_MANIFEST.json'
        if lock_path.stat().st_size > 1024 * 1024 or manifest_path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('runtime metadata too large')
        lock = json.loads(lock_path.read_text(encoding='utf-8'))
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if not isinstance(lock, dict) or not isinstance(manifest, dict):
            raise ValueError('runtime metadata roots must be objects')
        pinned = lock.get('ffmpeg')
        if not isinstance(pinned, dict) or manifest.get('sources') != lock:
            raise ValueError('runtime source lock differs')
        expected = {
            'ffmpeg/bin/ffmpeg.exe',
            'ffmpeg/bin/ffprobe.exe',
        }
        entries = manifest.get('files')
        if not isinstance(entries, list) or len(entries) > 100_000:
            raise ValueError('invalid runtime inventory')
        indexed = {}
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError('invalid runtime entry')
            name = item.get('path')
            if not isinstance(name, str) or '\\' in name or name.startswith('/') or '..' in Path(name).parts or name in indexed:
                raise ValueError('unsafe runtime path')
            indexed[name] = item
        if not expected.issubset(indexed):
            raise ValueError('missing locked FFmpeg companion')
        paths = []
        for name in sorted(expected):
            item = indexed[name]
            if type(item.get('bytes')) is not int or item['bytes'] <= 0 or not re.fullmatch(r'[a-f0-9]{64}', str(item.get('sha256', ''))):
                raise ValueError('invalid runtime hash record')
            path = root / 'runtime' / Path(name)
            for parent in (root / 'runtime', root / 'runtime' / 'ffmpeg', root / 'runtime' / 'ffmpeg' / 'bin', path):
                if parent.is_symlink():
                    raise ValueError('linked runtime path')
            if not path.resolve(strict=True).is_relative_to((root / 'runtime').resolve(strict=True)):
                raise ValueError('runtime executable escaped bundle')
            identity = _stat_identity(path)
            if (not identity or identity != identities[2 + len(paths)] or identity[2] != item['bytes'] or
                    _sha256(path) != item['sha256']):
                raise ValueError('modified runtime executable')
            paths.append(str(path.resolve(strict=True)))
        # Both tools must be the exact same locked bundle directory.
        if Path(paths[0]).parent != Path(paths[1]).parent:
            raise ValueError('FFmpeg tools have different parents')
        observed = tuple(_stat_identity(root / relative) for relative in (
            Path('tools/runtime-lock.json'), Path('runtime/RUNTIME_MANIFEST.json'),
            Path('runtime/ffmpeg/bin/ffmpeg.exe'), Path('runtime/ffmpeg/bin/ffprobe.exe')))
        if observed != identities:
            raise ValueError('runtime changed during verification')
        result = tuple(paths)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        result = None
    if cacheable:
        with _FRAME_TOOLS_LOCK:
            _FRAME_TOOLS_CACHE[key] = result
            if len(_FRAME_TOOLS_CACHE) > 8:
                _FRAME_TOOLS_CACHE.pop(next(iter(_FRAME_TOOLS_CACHE)))
    return result


def frame_tool_paths():
    """Return the decoder pair supported by frame inspection on this platform."""
    if sys.platform == 'win32':
        root = APP_ROOT.resolve()
        locations = (
            root / 'tools' / 'runtime-lock.json',
            root / 'runtime' / 'RUNTIME_MANIFEST.json',
            root / 'runtime' / 'ffmpeg' / 'bin' / 'ffmpeg.exe',
            root / 'runtime' / 'ffmpeg' / 'bin' / 'ffprobe.exe',
        )
        identities = tuple(_stat_identity(path) for path in locations)
        if any(identity is None for identity in identities):
            return None, None
        tools = _validated_windows_frame_tools(root, identities)
        return tools if tools else (None, None)
    return ffmpeg_path(), ffprobe_path()


def frame_tool_version():
    """Read the version prefix named by the packaged FFmpeg archive lock."""
    try:
        lock = json.loads((APP_ROOT / 'tools' / 'runtime-lock.json').read_text(encoding='utf-8'))
        if not isinstance(lock, dict):
            return None
        pinned = lock.get('ffmpeg')
        if not isinstance(pinned, dict) or not isinstance(pinned.get('url'), str):
            return None
        match = re.search(r'/ffmpeg-([0-9]+(?:\.[0-9]+)+)-', pinned['url'])
        return match.group(1) if match else None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def ffmpeg_path():
    bundled = APP_ROOT / 'runtime' / 'ffmpeg' / 'bin' / ('ffmpeg' if sys.platform == 'darwin' else 'ffmpeg.exe')
    return str(bundled) if bundled.is_file() else shutil.which('ffmpeg')


def ffprobe_path():
    # Mac's currently pinned wheel has no supported ffprobe deployment.
    if sys.platform == 'darwin':
        return None
    bundled = APP_ROOT / 'runtime' / 'ffmpeg' / 'bin' / 'ffprobe.exe'
    if sys.platform == 'win32':
        _, probe = frame_tool_paths()
        return probe
    return shutil.which('ffprobe')  # explicit source-development environment


@lru_cache(maxsize=1)
def image_support():
    try:
        from PIL import Image
        # Detect broken native wheels as well as missing Pillow.
        Image.new('RGB', (1, 1)).close()
        return True
    except (ImportError, OSError):
        return False
