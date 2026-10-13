"""Resolve private media tools without modifying the machine's PATH or Python."""
from functools import lru_cache
from pathlib import Path
import shutil
import sys

APP_ROOT = Path(__file__).resolve().parents[1]


def ffmpeg_path():
    bundled = APP_ROOT / 'runtime' / 'ffmpeg' / 'bin' / ('ffmpeg' if sys.platform == 'darwin' else 'ffmpeg.exe')
    return str(bundled) if bundled.is_file() else shutil.which('ffmpeg')


def ffprobe_path():
    # Mac's currently pinned wheel has no supported ffprobe deployment.
    if sys.platform == 'darwin':
        return None
    bundled = APP_ROOT / 'runtime' / 'ffmpeg' / 'bin' / 'ffprobe.exe'
    if sys.platform == 'win32':
        return str(bundled) if bundled.is_file() else None
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
