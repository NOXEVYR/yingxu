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

from yingxu import __version__, __mac_preview__, __build__
from .release_identity import compare_builds, public_build, fetch_metadata, fetch_manifest
from .range_zip import UpdateError
from yingxu.store import UserError

RELEASES = 'https://api.github.com/repos/NOXEVYR/yingxu/releases?per_page=100'
PAGE = 'https://github.com/NOXEVYR/yingxu/releases/tag/'
MAX_RESPONSE = 2 * 1024 * 1024
_lock = threading.Lock()
_cache = None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def version_key(version, mac=False):
    match = re.fullmatch(r'(\d{1,4})\.(\d{1,4})\.(\d{1,4})' + (r'-mac\.(\d{1,4})' if mac else ''), version)
    return tuple(map(int, match.groups())) if match else None


def select_release(releases, platform, current, current_build=None, latest_build=None):
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
    kind = 'version' if key > baseline else 'current'
    verified = False
    message = '发现新正式版本。' if kind == 'version' else '当前版本号已是最新；未核对构建身份。'
    if not mac and key == baseline and (current_build is not None or latest_build is not None):
        kind = compare_builds(current_build, latest_build)
        verified = kind != 'manual'
        message = {'build': '发现同版本的新构建修订。', 'current': '当前已是最新正式构建。',
                   'manual': '版本号相同，但构建修订缺失、无效或属于不同系列，请到发布页人工核对。'}[kind]
    return {'current_version':current, 'latest_version':version,
            'current_build':public_build(current_build), 'latest_build':public_build(latest_build),
            'update_kind':kind, 'identity_verified':verified, 'message':message,
            'update_available':kind in ('version', 'build'), 'tag':tag, 'url':PAGE + tag,
            'channel':'macOS Apple Silicon 试用版' if mac else 'Windows 正式版'}


def check_update():
    global _cache
    platform = sys.platform
    current = __mac_preview__ if platform == 'darwin' else __version__
    current_build = __build__ if platform == 'win32' else ''
    if platform not in ('win32', 'darwin'):
        raise UserError('当前平台暂未提供可检查的安装包。')
    if not _lock.acquire(blocking=False):
        raise UserError('正在检查更新，请稍候。', 409)
    try:
        if _cache and _cache[0] == (platform, current, current_build) and time.monotonic() - _cache[1] < 60:
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
            result = select_release(releases, platform, current, current_build)
            if platform == 'win32' and version_key(result['latest_version']) >= version_key(current):
                # A bound external identity prevents reuse of another build's
                # cached plan even when the semantic version itself is newer.
                same_version = result['latest_version'] == current
                from .incremental_update import select_release as bound_release
                from .range_zip import Network
                matching = [row for row in releases if isinstance(row, dict) and row.get('tag_name') == result['tag']]
                candidate = bound_release(matching)
                if candidate is not None:
                    network = Network(seconds=120, max_bytes=(32 if same_version else 1) * 1024**2)
                    metadata = fetch_metadata(candidate, network)
                    latest_build = metadata.get('build_revision', '')
                    if same_version and compare_builds(current_build, latest_build) != 'manual':
                        fetch_manifest(candidate, metadata, network)
                    result = select_release(releases, platform, current, current_build, latest_build)
                    if not same_version and not result['latest_build']:
                        result['message'] = '发现新正式版本；发布缺少可比较的构建修订，增量更新需重新核对清单。'
                elif same_version:
                    result.update(update_available=False, update_kind='manual', identity_verified=False,
                                  message='版本号相同，但发布缺少可验证的构建清单，请到发布页人工核对。')
        except HTTPError as error:
            message = 'GitHub 请求受限，请稍后再试。' if error.code in (403, 429) else 'GitHub 暂时无法访问，请稍后再试。'
            raise UserError(message, 502) from error
        except UpdateError as error:
            raise UserError(str(error), 502) from error
        except (OSError, URLError, ValueError) as error:
            raise UserError('未能检查更新，请确认网络可以访问 GitHub 后重试。', 502) from error
        _cache = ((platform, current, current_build), time.monotonic(), result)
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
