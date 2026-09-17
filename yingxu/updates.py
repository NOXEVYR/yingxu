"""On-demand public release lookup. Imported only by authenticated user actions."""
import json
import os
import re
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

from yingxu import __version__, __mac_preview__
from yingxu.store import UserError

RELEASES = 'https://api.github.com/repos/turnsolesama/yingxu/releases?per_page=100'
PAGE = 'https://github.com/turnsolesama/yingxu/releases/tag/'
MAX_RESPONSE = 2 * 1024 * 1024
_lock = threading.Lock()
_cache = None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def version_key(version, mac=False):
    match = re.fullmatch(r'(\d{1,4})\.(\d{1,4})\.(\d{1,4})' + (r'-mac\.(\d{1,4})' if mac else ''), version)
    return tuple(map(int, match.groups())) if match else None


def select_release(releases, platform, current):
    mac = platform == 'darwin'
    if platform not in ('win32', 'darwin'):
        raise UserError('当前平台暂未提供可检查的安装包。')
    baseline = version_key(current, mac)
    if baseline is None:
        raise UserError('当前版本标识无法识别，请查看 GitHub 发布记录。')
    candidates = []
    if not isinstance(releases, list):
        raise ValueError('Invalid release list')
    for release in releases:
        if not isinstance(release, dict) or release.get('draft') or (not mac and release.get('prerelease')):
            continue
        tag = release.get('tag_name', '')
        if not isinstance(tag, str) or not tag.startswith('yingxu-v'):
            continue
        version = tag[len('yingxu-v'):]
        key = version_key(version, mac)
        name = f'YingXu-v{version}-' + ('macOS-arm64.zip' if mac else 'Windows-x64.zip')
        assets = release.get('assets', [])
        if key and isinstance(assets, list) and any(isinstance(a, dict) and a.get('name') == name and a.get('state') == 'uploaded' and isinstance(a.get('size'), int) and a['size'] > 0 for a in assets):
            candidates.append((key, version, tag))
    if not candidates:
        raise UserError('暂未找到当前平台的完整发布包，请稍后再检查。', 502)
    key, version, tag = max(candidates)
    return {'current_version':current, 'latest_version':version,
            'update_available':key > baseline, 'tag':tag, 'url':PAGE + tag,
            'channel':'macOS Apple Silicon 试用版' if mac else 'Windows 正式版'}


def check_update():
    global _cache
    platform = sys.platform
    current = __mac_preview__ if platform == 'darwin' else __version__
    if platform not in ('win32', 'darwin'):
        raise UserError('当前平台暂未提供可检查的安装包。')
    if not _lock.acquire(blocking=False):
        raise UserError('正在检查更新，请稍候。', 409)
    try:
        if _cache and _cache[0] == (platform, current) and time.monotonic() - _cache[1] < 60:
            return dict(_cache[2])
        try:
            releases = []
            remaining = MAX_RESPONSE
            opener = build_opener(NoRedirect())
            for page in range(1, 4):
                url = RELEASES if page == 1 else RELEASES + f'&page={page}'
                request = Request(url, headers={'Accept':'application/vnd.github+json', 'User-Agent':'YingXu-Manual-Update-Check'})
                with opener.open(request, timeout=8) as response:
                    raw = response.read(remaining + 1)
                    link = response.headers.get('Link', '')
                remaining -= len(raw)
                if remaining < 0:
                    raise ValueError('Response too large')
                entries = json.loads(raw)
                if not isinstance(entries, list):
                    raise ValueError('Invalid release list')
                releases.extend(entries)
                if not re.search(r'rel=["\']?next\b', link):
                    break
            else:
                raise UserError('发布记录过多，暂时无法完整核对版本，请查看 GitHub 发布页。', 502)
            result = select_release(releases, platform, current)
        except HTTPError as error:
            message = 'GitHub 请求受限，请稍后再试。' if error.code in (403, 429) else 'GitHub 暂时无法访问，请稍后再试。'
            raise UserError(message, 502) from error
        except (OSError, URLError, ValueError) as error:
            raise UserError('未能检查更新，请确认网络可以访问 GitHub 后重试。', 502) from error
        _cache = ((platform, current), time.monotonic(), result)
        return dict(result)
    finally:
        _lock.release()


def open_release(tag):
    mac = sys.platform == 'darwin'
    if not isinstance(tag, str) or not tag.startswith('yingxu-v') or version_key(tag[8:], mac) is None:
        raise UserError('发布版本无效。')
    url = PAGE + tag
    try:
        if sys.platform == 'win32':
            os.startfile(url)
        elif mac:
            subprocess.Popen(['open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            raise UserError('当前平台暂不支持打开发布页。')
    except OSError as error:
        raise UserError('未能打开系统浏览器，请复制发布页地址后手动打开。') from error
    return {'ok':True}
