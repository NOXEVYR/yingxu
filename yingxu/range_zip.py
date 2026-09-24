"""Small, strict ZIP reader that never silently downloads the complete archive."""
from dataclasses import dataclass
import hashlib
import json
import re
import stat
import struct
import time
import urllib.parse
import urllib.request
import zlib

REPOSITORY = 'NOXEVYR/yingxu'
HOSTS = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
MAX_FILES = 20000
MAX_ARCHIVE = 2 * 1024**3
MAX_EXPANDED = 3 * 1024**3
MAX_MEMBER = 768 * 1024**2
MAX_MANIFEST = 8 * 1024**2
MAX_DIRECTORY = 8 * 1024**2
CHUNK = 1024 * 1024
ROOT_FILES = {'README.md', 'RUNNING.md', 'LICENSE', 'AGENTS.md', 'API_CONTRACT.md', '.gitignore',
              '.gitattributes', 'MIGRATION.md', 'server.py', 'macos_app.py', 'launcher.pyw', 'start.vbs',
              'Stop-YingXu.ps1', 'YingXu.exe', 'THIRD_PARTY_NOTICES.md', 'RELEASE_MANIFEST.json'}
CODE_SUFFIXES = {'.py', '.pyw', '.js', '.cjs', '.mjs', '.css', '.html', '.md', '.cs', '.ps1', '.json',
                 '.txt', '.svg', '.ico', '.icns', '.manifest', '.yaml', '.woff', '.woff2', '.ttf', '.jsx'}


class UpdateError(ValueError):
    """A safe, user-facing error, never an untrusted HTTP/server exception."""


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})', value):
        raise UpdateError('发布版本标识无效。')
    return tuple(map(int, value.split('.')))


def safe_url(value):
    try:
        url = urllib.parse.urlsplit(value)
        if (url.scheme != 'https' or url.hostname not in HOSTS or url.username or url.password or
                url.port not in (None, 443) or url.fragment or '\\' in value or any(ord(c) < 32 for c in value)):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise UpdateError('更新地址不属于受信任的 GitHub HTTPS 地址。') from None
    return value


def safe_relative(name):
    if not isinstance(name, str) or not name or len(name) > 1024 or '\\' in name:
        raise UpdateError('更新包包含无效路径。')
    parts = name.split('/')
    if any(not p or p in ('.', '..') or len(p) > 255 or p[-1] in ' .' or
           any(ord(c) < 32 or c in ':<>"|?*' for c in p) or
           re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)', p) for p in parts):
        raise UpdateError('更新包包含不安全路径。')
    return parts


def program_path(name):
    parts = safe_relative(name)
    if name in ROOT_FILES:
        return True
    suffix = '.' + parts[-1].rsplit('.', 1)[-1] if '.' in parts[-1] else ''
    if parts[0] in ('yingxu', 'frontend', 'desktop', 'tests', 'docs', 'tools') and len(parts) > 1:
        return suffix in CODE_SUFFIXES
    if parts[0] == 'runtime' and len(parts) > 1:
        return len(parts) == 2 or parts[1] in ('Lib', 'ffmpeg', 'webview2')
    return False


def validate_manifest(raw, version=None):
    if len(raw) > MAX_MANIFEST:
        raise UpdateError('发布清单超过大小限制。')
    try:
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get('application') != 'YingXu' or data.get('root') != 'YingXu/' or
                data.get('architecture') != 'Windows x64' or (version and data.get('version') != version)):
            raise ValueError()
        version_key(data.get('version'))
        files = data.get('files')
        if not isinstance(files, list) or not 1 <= len(files) < MAX_FILES:
            raise ValueError()
        result, folded, total = {}, set(), 0
        for row in files:
            name = row['path']
            if (not program_path(name) or name == 'RELEASE_MANIFEST.json' or name.casefold() in folded or
                    type(row['bytes']) is not int or not 0 <= row['bytes'] <= MAX_MEMBER or
                    not isinstance(row['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
                raise ValueError()
            result[name] = {'path': name, 'bytes': row['bytes'], 'sha256': row['sha256']}
            folded.add(name.casefold())
            total += row['bytes']
        if total > MAX_EXPANDED:
            raise ValueError()
        _check_tree(result)
        if not {'YingXu.exe', 'server.py', 'launcher.pyw', 'frontend/index.html'}.issubset(result):
            raise ValueError()
        return data, result
    except (ValueError, KeyError, TypeError, RecursionError):
        raise UpdateError('发布清单的应用、平台、版本或文件列表无效。') from None


def _check_tree(names):
    folded = {name.casefold() for name in names}
    prefixes = {}
    for name in names:
        parts = name.casefold().split('/')
        if any('/'.join(parts[:index]) in folded for index in range(1, len(parts))):
            raise UpdateError('更新包文件与目录存在冲突。')
        original = name.split('/')
        for index in range(1, len(parts) + 1):
            key, prefix = '/'.join(parts[:index]), '/'.join(original[:index])
            if key in prefixes and prefixes[key] != prefix:
                raise UpdateError('更新包目录存在大小写冲突。')
            prefixes[key] = prefix


class _Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Network:
    def __init__(self, cancelled=None, seconds=1800, max_bytes=MAX_ARCHIVE + 32 * CHUNK):
        self.cancelled = cancelled or (lambda: False)
        self.deadline = time.monotonic() + seconds
        self.max_bytes = max_bytes
        self.received = 0
        self.requests = 0
        self.etags = {}

    def active(self):
        if self.cancelled():
            raise UpdateError('更新操作已取消。')
        if time.monotonic() > self.deadline:
            raise UpdateError('更新操作超时，请重新尝试。')

    def _open(self, request):
        return urllib.request.build_opener(_Redirect()).open(request, timeout=15)

    def get(self, url, limit, start=None, end=None, total=None):
        self.active()
        safe_url(url)
        self.requests += 1
        if self.requests > 50000 or limit < 0 or limit > MAX_MANIFEST:
            raise UpdateError('更新请求超过限制。')
        headers = {'User-Agent': 'YingXu-Incremental-Updater', 'Accept-Encoding': 'identity'}
        ranged = start is not None
        if ranged:
            if not 0 <= start <= end < total or end - start + 1 != limit:
                raise UpdateError('更新分块范围无效。')
            headers['Range'] = f'bytes={start}-{end}'
            if url in self.etags:
                headers['If-Match'] = self.etags[url]
        with self._open(urllib.request.Request(url, headers=headers)) as response:
            safe_url(response.geturl())
            if response.status != (206 if ranged else 200):
                raise UpdateError('服务器未支持可靠的分块下载，请使用发布页的完整包。')
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise UpdateError('更新响应编码无效。')
            if ranged and response.headers.get('Content-Range') != f'bytes {start}-{end}/{total}':
                raise UpdateError('更新分块范围与请求不一致，请使用发布页的完整包。')
            length = response.headers.get('Content-Length')
            if length is not None:
                try:
                    length = int(length)
                except ValueError:
                    raise UpdateError('更新响应长度无效。') from None
                if length < 0 or length > limit or ranged and length != limit:
                    raise UpdateError('更新响应长度与请求不一致。')
            etag = response.headers.get('ETag')
            if etag and not etag.startswith('W/'):
                if url in self.etags and self.etags[url] != etag:
                    raise UpdateError('发布附件在更新过程中发生变化，请重新检查。')
                self.etags[url] = etag
            output = bytearray()
            while True:
                self.active()
                block = getattr(response, 'read1', response.read)(min(65536, limit + 1 - len(output)))
                if not block:
                    break
                output.extend(block)
                self.received += len(block)
                if len(output) > limit or self.received > self.max_bytes:
                    raise UpdateError('更新传输超过大小限制。')
            if ranged and len(output) != limit:
                raise UpdateError('更新分块传输不完整，请重试。')
            return bytes(output)


@dataclass
class Member:
    name: str
    raw_name: bytes
    offset: int
    compressed: int
    size: int
    crc: int
    flags: int
    method: int
    boundary: int = 0
    data_offset: int = -1


class RangeZip:
    def __init__(self, url, size, network):
        if type(size) is not int or not 22 <= size <= MAX_ARCHIVE:
            raise UpdateError('发布包大小无效。')
        self.url, self.size, self.network = safe_url(url), size, network
        self.members = self._directory()

    def _range(self, start, length):
        if length == 0:
            return b''
        return self.network.get(self.url, length, start, start + length - 1, self.size)

    def _directory(self):
        # Published packages have no ZIP comment/ZIP64. Refuse unsupported forms
        # rather than reading an arbitrary tail that may contain unchanged payload.
        tail = self._range(self.size - 22, 22)
        sig, disk, cd_disk, local_count, count, length, offset, comment = struct.unpack('<4s4H2IH', tail)
        if (sig != b'PK\x05\x06' or disk or cd_disk or comment or local_count != count or
                not 1 <= count <= MAX_FILES or count == 65535 or not 0 < length <= MAX_DIRECTORY or
                offset + length != self.size - 22):
            raise UpdateError('发布包目录格式不支持增量更新，请使用发布页的完整包。')
        directory = self._range(offset, length)
        cursor, total, members, folded = 0, 0, {}, set()
        for _ in range(count):
            if cursor + 46 > len(directory):
                raise UpdateError('发布包目录不完整。')
            values = struct.unpack_from('<4s6H3I5H2I', directory, cursor)
            sig, made, need, flags, method, _, _, crc, compressed, size, nlen, xlen, clen, disk, _, attr, local = values
            end = cursor + 46 + nlen + xlen + clen
            if sig != b'PK\x01\x02' or end > len(directory) or not nlen or disk:
                raise UpdateError('发布包目录项无效。')
            raw_name = directory[cursor + 46:cursor + 46 + nlen]
            try:
                name = raw_name.decode('utf-8' if flags & 0x800 else 'cp437')
            except UnicodeError:
                raise UpdateError('发布包文件名编码无效。') from None
            safe_relative(name)
            mode = attr >> 16
            if (flags & ~0x80e or flags & 1 or method not in (0, 8) or need >= 45 or
                    stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or attr & 0x10 or
                    size > MAX_MEMBER or compressed > MAX_MEMBER or
                    size > max(CHUNK, compressed * 1000) or local + 30 + nlen + compressed > offset or
                    not name.startswith('YingXu/') or not program_path(name[7:]) or name.casefold() in folded):
                raise UpdateError('发布包包含不安全、重复或异常文件。')
            if method == 0 and size != compressed:
                raise UpdateError('发布包储存成员大小无效。')
            # ZIP64 and Unicode-path overrides are not part of our package format.
            extra = directory[cursor + 46 + nlen:cursor + 46 + nlen + xlen]
            self._extra(extra)
            members[name] = Member(name, raw_name, local, compressed, size, crc, flags, method)
            folded.add(name.casefold())
            total += size
            cursor = end
        if cursor != len(directory) or total > MAX_EXPANDED:
            raise UpdateError('发布包目录或总解压大小无效。')
        _check_tree(members)
        ordered = sorted(members.values(), key=lambda item: item.offset)
        for index, member in enumerate(ordered):
            member.boundary = ordered[index + 1].offset if index + 1 < len(ordered) else offset
            if member.offset + 30 + len(member.raw_name) + member.compressed > member.boundary:
                raise UpdateError('发布包成员范围重叠。')
        return members

    @staticmethod
    def _extra(extra):
        position = 0
        while position < len(extra):
            if len(extra) - position < 4:
                raise UpdateError('发布包扩展字段无效。')
            kind, length = struct.unpack_from('<HH', extra, position)
            position += 4
            if position + length > len(extra) or kind in (1, 0x7075):
                raise UpdateError('发布包扩展格式不支持增量更新。')
            position += length

    def prepare(self, member):
        if member.data_offset >= 0:
            return
        raw = self._range(member.offset, 30)
        sig, need, flags, method, _, _, crc, compressed, size, nlen, xlen = struct.unpack('<4s5H3I2H', raw)
        if (sig != b'PK\x03\x04' or need >= 45 or flags != member.flags or method != member.method or
                nlen != len(member.raw_name) or member.offset + 30 + nlen + xlen + member.compressed > member.boundary or
                (not flags & 8 and (crc, compressed, size) != (member.crc, member.compressed, member.size)) or
                (flags & 8 and (crc not in (0, member.crc) or compressed not in (0, member.compressed) or size not in (0, member.size)))):
            raise UpdateError('发布包本地文件头与目录不一致。')
        names = self._range(member.offset + 30, nlen + xlen)
        if names[:nlen] != member.raw_name:
            raise UpdateError('发布包本地文件名不一致。')
        self._extra(names[nlen:])
        member.data_offset = member.offset + 30 + nlen + xlen

    def extract(self, member, output, expected_sha256=None, progress=None):
        self.prepare(member)
        decompressor = zlib.decompressobj(-15) if member.method == 8 else None
        sha, crc, expanded = hashlib.sha256(), 0, 0

        def emit(block):
            nonlocal crc, expanded
            expanded += len(block)
            if expanded > member.size:
                raise UpdateError('发布包成员超过声明的解压大小。')
            output.write(block)
            sha.update(block)
            crc = zlib.crc32(block, crc)

        for offset in range(0, member.compressed, CHUNK):
            block = self._range(member.data_offset + offset, min(CHUNK, member.compressed - offset))
            if progress:
                progress(len(block))
            if decompressor:
                pending = block
                while pending:
                    self.network.active()
                    emit(decompressor.decompress(pending, min(CHUNK, member.size - expanded + 1)))
                    pending = decompressor.unconsumed_tail
                if decompressor.unused_data:
                    raise UpdateError('发布包压缩流包含额外数据。')
            else:
                emit(block)
        if decompressor and (not decompressor.eof or decompressor.unused_data):
            raise UpdateError('发布包压缩流不完整。')
        if expanded != member.size or crc & 0xffffffff != member.crc or expected_sha256 and sha.hexdigest() != expected_sha256:
            raise UpdateError('更新文件长度、CRC 或 SHA-256 校验失败。')
        return sha.hexdigest()
