"""Bounded, offline XMind ZIP reader/writer. Never extract archive members."""
import io
import json
from pathlib import PurePosixPath
import uuid
import zipfile
import xml.etree.ElementTree as ET

from .store import UserError, TEXT_LIMIT

ARCHIVE_LIMIT = 32 * 1024 * 1024
EXPANDED_LIMIT = 64 * 1024 * 1024
TOPIC_LIMIT = 5000
DEPTH_LIMIT = 64


def validate(content):
    if not isinstance(content, str) or len(content.encode('utf-8')) > TEXT_LIMIT:
        raise UserError('思维导图正文最多 2 MiB。')
    try:
        sheets = json.loads(content)
        if not isinstance(sheets, list) or not 1 <= len(sheets) <= 100:
            raise ValueError()
        pending = []
        ids = set()
        for sheet in sheets:
            if not isinstance(sheet, dict) or not isinstance(sheet.get('title', ''), str):
                raise ValueError()
            pending.append((sheet['rootTopic'], 0))
        count = 0
        while pending:
            topic, depth = pending.pop()
            count += 1
            if count > TOPIC_LIMIT or depth > DEPTH_LIMIT:
                raise ValueError()
            if (not isinstance(topic, dict) or not isinstance(topic.get('id'), str)
                    or not topic['id'] or topic['id'] in ids
                    or not isinstance(topic.get('title', ''), str)):
                raise ValueError()
            ids.add(topic['id'])
            children = topic.get('children', {})
            if not isinstance(children, dict):
                raise ValueError()
            for kind, group in children.items():
                if kind not in ('attached', 'detached', 'summary') or not isinstance(group, list):
                    raise ValueError()
                pending.extend((child, depth + 1) for child in group)
    except (ValueError, KeyError, TypeError, RecursionError):
        raise UserError('XMind 结构无效，或超过 100 个画布、5000 个主题、64 层深度。') from None
    return sheets


def _archive(raw):
    if len(raw) > ARCHIVE_LIMIT:
        raise UserError('XMind 文件最多 32 MiB，请使用 XMind 打开较大的文件。')
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        entries = archive.infolist()
        names = set()
        total = 0
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            total += entry.file_size
            if (name in names or '\\' in name or path.is_absolute()
                    or '..' in path.parts or ':' in name or '\x00' in name
                    or entry.flag_bits & 1 or entry.file_size > EXPANDED_LIMIT
                    or total > EXPANDED_LIMIT or len(entries) > 2048):
                raise ValueError()
            names.add(name)
        return archive
    except (ValueError, OSError, zipfile.BadZipFile):
        raise UserError('XMind 压缩包损坏、加密或超过安全读取限制。') from None


def _read(archive, name):
    try:
        if archive.getinfo(name).file_size > TEXT_LIMIT:
            raise UserError('思维导图正文最多 2 MiB。')
        return archive.read(name)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
        raise UserError('无法读取 XMind 正文，压缩包可能已损坏。') from None


def _legacy(raw):
    # Reject declarations before the standard XML parser sees them, including UTF-16.
    if b'<!doctype' in raw.lower().replace(b'\x00', b'') or b'<!entity' in raw.lower().replace(b'\x00', b''):
        raise UserError('不支持含 DTD 或实体声明的 XMind XML。')
    try:
        root = ET.fromstring(raw)
        if sum(1 for _ in root.iter()) > 30000:
            raise ValueError()
        local = lambda element: element.tag.rsplit('}', 1)[-1]
        def child(element, name):
            return next((value for value in element if local(value) == name), None)
        def topic(element, depth=0):
            if depth > DEPTH_LIMIT:
                raise ValueError()
            title = child(element, 'title')
            result = {'id': element.get('id') or uuid.uuid4().hex,
                      'title': ''.join(title.itertext()) if title is not None else ''}
            groups = child(element, 'children')
            if groups is not None:
                result['children'] = {}
                for group in groups:
                    kind = group.get('type', 'attached')
                    if local(group) == 'topics' and kind in ('attached', 'detached', 'summary'):
                        result['children'][kind] = [topic(value, depth + 1) for value in group if local(value) == 'topic']
            return result
        sheets = []
        for sheet in root:
            if local(sheet) != 'sheet':
                continue
            title = child(sheet, 'title')
            sheets.append({'id': sheet.get('id') or uuid.uuid4().hex,
                           'title': ''.join(title.itertext()) if title is not None else '画布',
                           'rootTopic': topic(child(sheet, 'topic'))})
        return json.dumps(sheets, ensure_ascii=False)
    except (ET.ParseError, ValueError, TypeError, AttributeError, RecursionError):
        raise UserError('旧版 XMind XML 正文无效或超过读取限制。') from None


def read(raw):
    with _archive(raw) as archive:
        modern = 'content.json' in archive.namelist()
        if modern:
            try:
                content = _read(archive, 'content.json').decode('utf-8-sig')
            except UnicodeError:
                raise UserError('XMind 正文必须为 UTF-8 JSON。') from None
        else:
            content = _legacy(_read(archive, 'content.xml'))
        validate(content)
        return {'format': 'xmind', 'content': content, 'editable': modern,
                'notice': ('支持主题文字、子主题和同级主题编辑；原有样式、附件及其他信息保留。保存前备份原文件。'
                           if modern else '旧版 XML XMind 仅预览主题结构；请用 XMind 另存为新版格式后编辑。')}


def create(title):
    content = json.dumps([{'id': uuid.uuid4().hex, 'class': 'sheet', 'title': '画布 1',
                          'rootTopic': {'id': uuid.uuid4().hex, 'class': 'topic', 'title': title,
                                        'structureClass': 'org.xmind.ui.map.unbalanced'}}], ensure_ascii=False)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('content.json', content)
        archive.writestr('metadata.json', json.dumps({'creator': {'name': 'YingXu'}}))
        archive.writestr('manifest.json', json.dumps({'file-entries': {'content.json': {}, 'metadata.json': {}}}))
    return output.getvalue()


def edit(before, content):
    validate(content)
    current = read(before)
    if not current['editable']:
        raise UserError('旧版 XML XMind 仅支持预览，请先另存为新版格式。')
    if validate(current['content']) == validate(content):
        return before
    output = io.BytesIO()
    try:
        with _archive(before) as source, zipfile.ZipFile(output, 'w') as target:
            target.comment = source.comment
            for entry in source.infolist():
                value = content.encode('utf-8') if entry.filename == 'content.json' else source.read(entry)
                target.writestr(entry, value)
    except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
        raise UserError('XMind 附件损坏，已停止保存，原文件保留。') from None
    result = output.getvalue()
    if len(result) > ARCHIVE_LIMIT:
        raise UserError('保存后 XMind 文件超过 32 MiB，原文件保留。')
    return result
