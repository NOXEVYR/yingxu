"""Conservative relocation of ordinary Markdown file links during project copies.

No files are written. Only existing targets explicitly present in path_map may be
rewritten. Unsupported constructs are retained and reported for review.
"""
from __future__ import annotations

import html
import os
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit

from .store import decode_text, UserError, TEXT_LIMIT

def _unescape(value):
    return re.sub(r'\\([!"#$%&\'()*+,\-./:;<=>?@\[\]\\^_`{|}~])', r'\1', value)


def _suffix(value):
    # A numeric entity contains '#', but that character is not a URL fragment.
    cursor = 0
    while cursor < len(value):
        if value[cursor] == '&':
            entity = re.match(r'&(?:\#[0-9]+|\#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);', value[cursor:])
            if entity:
                if html.unescape(entity[0]).startswith(('?', '#')):return value[cursor:]
                cursor += len(entity[0]);continue
        if value[cursor] == '\\' and cursor+1 < len(value):
            if value[cursor+1] in '?#':return value[cursor:]
            cursor += 2;continue
        if value[cursor] in '?#':return value[cursor:]
        cursor += 1
    return ''


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def _destination(text, start, reference=False):
    """Return destination span and complete syntax end, or None."""
    cursor = start
    while cursor < len(text) and text[cursor] in ' \t':cursor += 1
    begin = cursor
    angle = cursor < len(text) and text[cursor] == '<'
    if angle:
        begin = cursor = cursor + 1
        while cursor < len(text):
            if text[cursor] == '\\' and cursor + 1 < len(text):cursor += 2;continue
            if text[cursor] == '>':break
            if text[cursor] in '<\r\n':return None
            cursor += 1
        if cursor >= len(text):return None
        end = cursor;cursor += 1
    else:
        depth = 0
        while cursor < len(text):
            char = text[cursor]
            if char == '\\' and cursor + 1 < len(text):cursor += 2;continue
            if char in ' \t\r\n':break
            if char == '(':
                depth += 1
                if depth > 32:return None
            elif char == ')':
                if depth == 0:break
                depth -= 1
            cursor += 1
            if cursor - begin > 4096:return None
        if depth:return None
        end = cursor
    if end == begin or end - begin > 4096:return None
    gap = cursor
    while cursor < len(text) and text[cursor] in ' \t':cursor += 1
    if cursor < len(text) and text[cursor] in '\"\'(' and cursor > gap:
        delim = ')' if text[cursor] == '(' else text[cursor]
        cursor += 1
        while cursor < len(text):
            if text[cursor] == '\\' and cursor + 1 < len(text):cursor += 2;continue
            if text[cursor] == delim:break
            if text[cursor] in '\r\n':return None
            cursor += 1
        if cursor >= len(text):return None
        cursor += 1
        while cursor < len(text) and text[cursor] in ' \t':cursor += 1
    if reference:
        if cursor != len(text):return None
    else:
        if cursor >= len(text) or text[cursor] != ')':return None
        cursor += 1
    return begin, end, cursor


def _protected(text, warn):
    """Mask code/HTML without feeding user Markdown through a lossy renderer."""
    mask = bytearray(len(text))
    def mark(start, end):mask[start:end] = b'\1' * (end-start)
    offset = 0;fence = None
    for line in text.splitlines(keepends=True):
        body = line.rstrip('\r\n')
        # Blockquotes and a single list prefix may contain a fenced code block.
        stripped = re.sub(r'^ {0,3}(?:> ?)*', '', body)
        stripped = re.sub(r'^(?:[-+*]|\d+[.)]) +', '', stripped)
        found = re.match(r' {0,3}(`{3,}|~{3,})(.*)$', stripped)
        if fence:
            mark(offset, offset+len(line))
            if found and found[1][0] == fence[0] and len(found[1]) >= fence[1] and not found[2].strip():fence = None
        elif found and not (found[1][0] == '`' and '`' in found[2]):
            fence = (found[1][0], len(found[1]));mark(offset, offset+len(line))
        elif body.startswith(('    ', '\t')):
            mark(offset, offset+len(line))
            if '](' in body or re.search(r'\[[^\]]+\]:', body):
                warn('缩进代码或嵌套列表中的链接未自动改写，请核对。')
        offset += len(line)
    for match in re.finditer(r'<!--.*?(?:-->|\Z)|<(pre|code|script|style)\b[^>]*>.*?(?:</\1\s*>|\Z)', text, re.I|re.S):
        mark(*match.span())
    # Attributes may themselves contain Markdown-looking strings. Destinations
    # in angle brackets are still consumed by the link parser before this mask.
    for match in re.finditer(r"<\/?[A-Za-z][A-Za-z0-9:-]*(?=[\s/>])(?:[^<>\"']|\"[^\"]*\"|'[^']*')*>", text):
        mark(*match.span())
    for match in re.finditer(r'<[^<>\r\n]*>', text):mark(*match.span())
    cursor = 0
    while cursor < len(text):
        if mask[cursor] or text[cursor] != '`':cursor += 1;continue
        backslashes = 0;previous = cursor-1
        while previous >= 0 and text[previous] == '\\':backslashes += 1;previous -= 1
        if backslashes % 2:cursor += 1;continue
        end = cursor
        while end < len(text) and text[end] == '`':end += 1
        count = end-cursor
        if count > 64:
            warn('超长反引号语法未自动处理，请核对链接。');cursor=end;continue
        pattern = re.compile(r'(?<!`)' + '`'*count + r'(?!`)')
        closing = pattern.search(text, end)
        while closing and mask[closing.start()]:closing = pattern.search(text, closing.end())
        if closing:
            mark(cursor, closing.end());cursor = closing.end()
        else:cursor = end
    return mask


def rewrite_markdown(raw: bytes, source: Path, target: Path, path_map: dict[Path, Path]):
    """Return {content: bytes, changed: bool, warnings: list[str]}.

    path_map is the caller's validated mapping of copied *files*. Ordinary local
    destinations are URL encoded after relocation. Query/fragment/title syntax
    is retained exactly, including stable #yx-item IDs. No original is touched.
    """
    warnings = []
    def warn(message):
        if message not in warnings and len(warnings) < 20:warnings.append(message)
    def unchanged():return {'content': raw, 'changed': False, 'warnings': warnings}
    if source.suffix.lower() not in ('.md', '.markdown'):return unchanged()
    if len(raw) > TEXT_LIMIT:
        warn('Markdown 超过 2 MiB，未自动改写链接。');return unchanged()
    try:text, encoding = decode_text(raw)
    except UserError:
        warn('Markdown 编码无法识别，未自动改写链接。');return unchanged()
    if encoding == 'utf-8-sig' and not raw.startswith(b'\xef\xbb\xbf'):encoding = 'utf-8'
    bom = b''
    if encoding == 'utf-16':
        bom = raw[:2];encoding = 'utf-16-le' if bom == b'\xff\xfe' else 'utf-16-be'
    if bom + text.encode(encoding) != raw:
        warn('Markdown 编码无法无损往返，整篇文稿保留原文。');return unchanged()
    mapping = {_key(old): Path(new) for old, new in path_map.items()}
    protected = _protected(text, warn)
    replacements = []

    def relocate(value):
        decoded_markup = html.unescape(_unescape(value))
        try:parts = urlsplit(decoded_markup)
        except ValueError:
            warn('含无法识别的链接地址，已保留原文。');return value
        if parts.scheme or parts.netloc or not parts.path or parts.path.startswith(('/', '\\')):return value
        if re.search(r'%(?![0-9a-fA-F]{2})', parts.path):
            warn('含无效百分号编码的相对链接，已保留原文。');return value
        try:relative = unquote(parts.path, errors='strict')
        except UnicodeError:
            warn('含无法解码的相对链接，已保留原文。');return value
        if (not relative or relative.startswith(('/', '\\')) or ':' in relative or '\\' in relative
                or any(ord(char) < 32 for char in relative)):
            warn('含非标准本地链接，已保留原文。');return value
        old = Path(os.path.abspath(source.parent / relative))
        new = mapping.get(_key(old))
        if new is None or not old.is_file() or old.is_symlink():
            warn('部分相对链接目标未包含在迁移文件中或已缺失，请核对。');return value
        try:
            before = Path(os.path.relpath(old, source.parent)).as_posix()
            after = Path(os.path.relpath(new, target.parent)).as_posix()
        except ValueError:
            warn('相对链接跨磁盘，未自动改写。');return value
        if before == after:return value
        # Preserve the original encoded query and fragment rather than rebuilding
        # them via urlsplit (which could normalize whitespace or entity escapes).
        return quote(after, safe='/.-_~') + _suffix(value)

    offset = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip('\r\n')
        reference = re.match(r' {0,3}\[(?:\\.|[^\]\\\r\n])+\]:[ \t]*', body)
        if reference and not any(protected[offset:offset+reference.end()]):
            if body.lstrip().startswith('[^'):
                warn('脚注语法未自动处理，请核对其中的相对链接。')
                offset += len(line);continue
            parsed = _destination(body, reference.end(), reference=True)
            if parsed:
                begin,end,_ = parsed;old = body[begin:end];new = relocate(old)
                if new != old:replacements.append((offset+begin,offset+end,new))
            else:warn('复杂或跨行的引用式链接未自动改写，请核对。')
            offset += len(line);continue
        cursor = 0;stack = []
        while cursor < len(body):
            if protected[offset+cursor]:cursor += 1;continue
            char = body[cursor]
            if char == '\\':cursor += 2;continue
            if char == '[':stack.append(cursor)
            elif char == ']' and stack:
                stack.pop()
                if cursor+1 < len(body) and body[cursor+1] == '(':
                    parsed = _destination(body, cursor+2)
                    if parsed:
                        begin,end,finish = parsed;old = body[begin:end];new = relocate(old)
                        if new != old:replacements.append((offset+begin,offset+end,new))
                        cursor = finish;continue
                    warn('复杂或跨行的行内链接未自动改写，请核对。')
            cursor += 1
        offset += len(line)
    if not replacements:return unchanged()
    chunks = [];cursor = 0
    for begin,end,value in replacements:
        chunks.extend((text[cursor:begin],value));cursor = end
    chunks.append(text[cursor:]);text = ''.join(chunks)
    try:content = bom + text.encode(encoding)
    except UnicodeError:
        warn('新链接无法使用原编码表示，整篇文稿保留原文。');return unchanged()
    return {'content': content, 'changed': content != raw, 'warnings': warnings}
