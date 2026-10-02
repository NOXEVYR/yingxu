"""Bounded reads from registered immutable collections; no scan or execution."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .skill_collections import SCHEMA, MAX_PACKAGE_BYTES, MAX_PACKAGE_FILES, _canonical
from .handoffs import PRIVATE_SEGMENTS, PRIVATE_SUFFIXES
from .store import UserError, clean_path, decode_text

MAX_FILE = 1024 * 1024
TEXT_EXTENSIONS = frozenset({'.md', '.txt', '.srt', '.vtt', '.csv', '.json', '.yaml', '.yml', '.html', '.htm', '.svg'})
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10)), 'COM¹', 'COM²', 'COM³', 'LPT¹', 'LPT²', 'LPT³'}
_PRIVATE = re.compile(r'(?i)^(?:\.(?:env|git|ssh|aws|azure|codex|config)|credentials?|secrets?|tokens?|api[-_]?keys?|accounts?|auth|configs?|settings|logs?)(?:[._-]|$)')
_PRIVATE_NAMES = {'.npmrc', '.pypirc', '.netrc', 'id_rsa', 'id_ed25519', 'id_dsa', 'mcp-access.json', 'mcp-listener.json'}
_CLIENT_CONFIG = re.compile(r'(?i)(?:client|harness)[ _-]*(?:config|settings)|(?:config|settings)[ _-]*(?:client|harness)')


def file_name(value):
    # Validate the original spelling before any Path normalization.
    if (not isinstance(value, str) or not 1 <= len(value) <= 512 or value.startswith('/')
            or '\\' in value or ':' in value or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
        raise UserError('请提供包内规范 POSIX 相对文件名。')
    parts = value.split('/')
    if any(p in ('', '.', '..') or p.endswith((' ', '.')) or p.split('.')[0].upper() in _RESERVED
           or any(c in p for c in '<>"|?*') for p in parts):
        raise UserError('包内文件名无效。')
    return value


def private_file(value):
    parts = {p.casefold() for p in value.split('/')}
    return (bool(parts & PRIVATE_SEGMENTS) or any(_PRIVATE.match(p) or _CLIENT_CONFIG.search(p) or p.casefold() in _PRIVATE_NAMES for p in value.split('/'))
            or bool(parts & {'client', 'clients', 'harness'}) and bool(parts & {'config', 'configs', 'settings'})
            or Path(value).suffix.casefold() in PRIVATE_SUFFIXES)


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def bounded_read(path, maximum=MAX_FILE):
    path = clean_path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise UserError('固定技能文件不是独立普通文件。', 403)
    if before.st_size > maximum:
        raise UserError('固定技能文件超过读取限制。', 413)
    with path.open('rb') as handle:
        opened = os.fstat(handle.fileno())
        # Windows stat/fstat ctime can differ, compare each API with itself.
        if _stamp(opened)[:4] != _stamp(before)[:4] or opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode):
            raise UserError('固定技能文件在读取时变化。', 409)
        raw = handle.read(maximum + 1)
        current = clean_path(path).stat()
        if _stamp(current) != _stamp(before) or _stamp(os.fstat(handle.fileno())) != _stamp(opened):
            raise UserError('固定技能文件在读取时变化。', 409)
    if len(raw) > maximum:
        raise UserError('固定技能文件超过读取限制。', 413)
    return raw


class FixedSkillReader:
    def __init__(self, collections, collection_id, version, *, skill_id=None, pin=None):
        self.root = collections._version_path(collection_id, version)
        with collections.store.connection() as db:
            row = db.execute('''SELECT v.path,v.package_sha256,v.total_bytes,v.file_count,
                substr(v.manifest,1,1048577) manifest,c.source_skill_id
                FROM yx_skill_collection_versions v JOIN yx_skill_collections c ON c.id=v.collection_id
                WHERE v.collection_id=? AND v.version=?''', (collection_id, version)).fetchone()
        if row is None:
            raise UserError('固定收藏版本不存在。', 404)
        if not row['path'] or Path(row['path']) != self.root:
            raise UserError('固定收藏目录登记异常。', 409)
        try:
            if not isinstance(row['manifest'], str) or len(row['manifest'].encode('utf-8')) > MAX_FILE:
                raise ValueError()
            manifest = json.loads(row['manifest'])
            if (not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA
                    or manifest.get('id') != collection_id or manifest.get('version') != version
                    or manifest.get('package_sha256') != version or row['package_sha256'] != version
                    or manifest.get('skill_id') != row['source_skill_id']
                    or not re.fullmatch(r'[0-9a-f]{32}', str(manifest.get('skill_id', '')))
                    or (skill_id is not None and manifest['skill_id'] != skill_id)):
                raise ValueError()
            files = manifest['files']
            if not isinstance(files, list) or not 1 <= len(files) <= MAX_PACKAGE_FILES:
                raise ValueError()
            total, seen, entries = 0, set(), []
            for item in files:
                if not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256', 'mtime_ns'}:
                    raise ValueError()
                name = file_name(item['path'])
                if name.casefold() in seen or type(item['size']) is not int or not 0 <= item['size'] <= MAX_PACKAGE_BYTES:
                    raise ValueError()
                if type(item['mtime_ns']) is not int or item['mtime_ns'] < 0 or not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
                    raise ValueError()
                seen.add(name.casefold()); total += item['size']
                entries.append({k: item[k] for k in ('path', 'size', 'sha256')})
            if (type(manifest['file_count']) is not int or manifest['file_count'] != len(files)
                    or type(manifest['total_bytes']) is not int or manifest['total_bytes'] != total
                    or total > MAX_PACKAGE_BYTES or row['file_count'] != len(files) or row['total_bytes'] != total
                    or 'skill.md' not in seen):
                raise ValueError()
            digest = hashlib.sha256(_canonical({'files': entries, 'file_count': len(files), 'total_bytes': total}).encode('utf-8')).hexdigest()
            if digest != version:
                raise ValueError()
            manifest_digest = hashlib.sha256(_canonical(manifest).encode('utf-8')).hexdigest()
            if pin is not None and (pin.get('collection_id') != collection_id or pin.get('version') != version
                    or pin.get('source_skill_id') != manifest['skill_id'] or pin.get('manifest_digest') != manifest_digest):
                raise ValueError()
            disk = json.loads(bounded_read(self.root / 'manifest.json').decode('utf-8'))
            if _canonical(disk) != _canonical(manifest):
                raise ValueError()
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
            raise UserError('固定技能版本清单异常。', 409) from None
        self.entries, self.pin = entries, pin
        # Validate the controlled files root, but never walk it.
        self.files_root = clean_path(self.root / 'files')
        if not self.files_root.is_dir() or not self.files_root.is_relative_to(self.root):
            raise UserError('固定技能文件目录无效。', 403)

    def files(self):
        return [{'file': e['path'], 'size': e['size'], 'sha256': e['sha256'],
                 'content_available': Path(e['path']).suffix.casefold() in TEXT_EXTENSIONS and e['size'] <= MAX_FILE,
                 'content_verified': False}
                for e in self.entries if not private_file(e['path'])]

    def read(self, name='SKILL.md'):
        name = file_name(name)
        entry = next((e for e in self.entries if e['path'] == name), None)
        # The old entry lookup accepted a lowercase skill.md.
        if entry is None and name.casefold() == 'skill.md':
            entry = next(e for e in self.entries if e['path'].casefold() == 'skill.md')
        if entry is None or private_file(entry['path']):
            raise UserError('文件未登记或不在参考读取范围。', 403)
        data = {'file': entry['path'], 'size': entry['size'], 'sha256': entry['sha256']}
        if Path(entry['path']).suffix.casefold() not in TEXT_EXTENSIONS:
            return {**data, 'content_available': False, 'content_verified': False, 'reason': '脚本和二进制仅提供清单信息，不执行。'}, None
        raw = bounded_read(self.files_root / entry['path'])
        if len(raw) != entry['size'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise UserError('固定技能文件内容已变化。', 409)
        if self.pin is not None and entry['path'].casefold() == 'skill.md' and entry['sha256'] != self.pin.get('entry_sha256'):
            raise UserError('固定技能入口内容已变化。', 409)
        if '\x00' in decode_text(raw)[0]:
            raise UserError('文本包含 NUL，不能作为参考正文读取。', 409)
        return {**data, 'content_verified': True}, raw
