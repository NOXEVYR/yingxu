"""Immutable, local AI handoff snapshots scoped to a client conversation.

This module records copy-ready work. It never sends a message or treats a
successful copy as an acknowledgement. Only :meth:`acknowledge` advances the
per-project, per-client, per-conversation baseline.
"""
from __future__ import annotations

from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .store import TEXT_LIMIT, UserError


SCHEMA_VERSION = 1
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 200
MAX_ASSETS = 500
MAX_SKILLS = 200
MAX_TASK_CHARS = 100_000
MAX_DIFF_CHARS = 24_000
MAX_ID_CHARS = 256
MAX_METADATA_TEXT = 1200
TEXT_KINDS = frozenset({'markdown', 'text', 'html', 'svg', 'excalidraw'})
SAFE_METADATA_KEYS = frozenset({
    'shot_number', 'duration', 'shot_size', 'camera', 'prompt',
    'negative_prompt', 'seed', 'model', 'version', 'width', 'height',
})
PRIVATE_SEGMENTS = frozenset({
    '.codex', '.claude', '.zcode', '.workbuddy', '.yingxu',
    'application support', 'client-config', 'client_config',
    'client config', 'credentials', 'secrets', 'logs', 'log',
})
PRIVATE_SUFFIXES = frozenset({
    '.env', '.log', '.db', '.sqlite', '.sqlite3', '.ini', '.pem', '.key',
    '.p12', '.pfx', '.bak',
})
SECRET_VALUE_RE = re.compile(
    r'(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|bearer)\b\s*[:=]\s*)([^\s,;\"\']+)'
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _clean_text(value, limit):
    text = str(value if value is not None else '')
    if len(text) > limit:
        text = text[:limit] + '…'
    return SECRET_VALUE_RE.sub(r'\1[已隐藏]', text)


def _identity(value, label, limit=MAX_ID_CHARS):
    if not isinstance(value, str):
        raise UserError(f'{label}不能为空。')
    value = value.strip()
    if not value or len(value) > limit or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise UserError(f'{label}格式不正确。')
    return value


def _task_text(value):
    if not isinstance(value, str) or not value.strip():
        raise UserError('请填写本轮任务。')
    if len(value) > MAX_TASK_CHARS:
        raise UserError(f'本轮任务最多 {MAX_TASK_CHARS} 个字符。')
    return value


def _metadata(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = {}
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in SAFE_METADATA_KEYS:
        if key not in value or value[key] is None or isinstance(value[key], (dict, list)):
            continue
        result[key] = _clean_text(value[key], MAX_METADATA_TEXT)
    return result


def _path_parts(value):
    # Split both Windows and POSIX forms, independent of the host running tests.
    return [part.casefold() for part in re.split(r'[\\/]+', str(value)) if part]


def _private_asset(row, data_root, project_root):
    path = str(row.get('path', ''))
    parts = _path_parts(path)
    basename = parts[-1] if parts else ''
    suffix = Path(basename).suffix.casefold()
    client_config_name = bool(re.search(r'client[ _-]*(?:config|settings)|(?:config|settings)[ _-]*client', basename))
    client_config_path = bool({'client', 'clients'}.intersection(parts) and
                              {'config', 'configs', 'configuration', 'settings'}.intersection(parts))
    if (basename.startswith('.env') or suffix in PRIVATE_SUFFIXES or
            PRIVATE_SEGMENTS.intersection(parts) or client_config_name or client_config_path):
        return True
    try:
        full = Path(path).resolve(strict=False)
        root = Path(data_root).resolve(strict=False)
        if os.path.commonpath((str(full), str(root))).casefold() == str(root).casefold():
            return True
        project = Path(project_root).resolve(strict=False)
        inside_project = os.path.commonpath((str(full), str(project))).casefold() == str(project).casefold()
        external_config = {'appdata', 'application support', '.config', 'config', 'configs', 'configuration', 'settings'}
        if external_config.intersection(parts) and not inside_project:
            return True
    except (OSError, ValueError):
        return True
    return False


class HandoffService:
    """Persist bounded, immutable handoff snapshots in the application's DB.

    ``context`` is accepted for application wiring, but current ``.yingxu``
    exports are deliberately not read as a baseline: they are mutable files.
    Handoff state is solely the immutable SQLite snapshot acknowledged for an
    exact ``(project_id, client_id, conversation_id)`` scope.
    """

    def __init__(self, store, skills=None, context=None, *, history_limit=DEFAULT_HISTORY_LIMIT):
        if isinstance(history_limit, bool) or not isinstance(history_limit, int) or not 1 <= history_limit <= MAX_HISTORY_LIMIT:
            raise ValueError(f'history_limit must be between 1 and {MAX_HISTORY_LIMIT}')
        self.store = store
        self.skills = skills
        self.context = context
        self.history_limit = history_limit
        self._init_schema()

    def _init_schema(self):
        with self.store.lock, self.store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS handoff_snapshots(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    client_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('full','delta')),
                    base_snapshot_id TEXT,
                    snapshot_digest TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS handoff_scope_history
                  ON handoff_snapshots(project_id,client_id,conversation_id,seq DESC);
                CREATE TABLE IF NOT EXISTS handoff_baselines(
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    client_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL REFERENCES handoff_snapshots(id),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id,client_id,conversation_id)
                );
                CREATE TABLE IF NOT EXISTS handoff_acknowledgements(
                    snapshot_id TEXT PRIMARY KEY REFERENCES handoff_snapshots(id) ON DELETE CASCADE,
                    acknowledged_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS handoff_snapshots_immutable
                BEFORE UPDATE ON handoff_snapshots
                BEGIN
                    SELECT RAISE(ABORT, 'handoff snapshots are immutable');
                END;
            ''')

    def _scope(self, project_id, client_id, conversation_id):
        project = self.store.get_project(project_id)
        return project, _identity(client_id, '客户端标识'), _identity(conversation_id, '会话标识')

    def _bound_skills(self, project_id):
        if self.skills is None:
            return []
        bound = self.skills.bound_skills(project_id)
        if isinstance(bound, dict):
            bound = bound.get('skills', bound.get('bound_skills', []))
        if not isinstance(bound, (list, tuple)):
            return []
        if len(bound) > MAX_SKILLS:
            raise UserError(f'本项目绑定技能超过 {MAX_SKILLS} 个，无法生成有界交接。')
        result = []
        for entry in bound:
            if isinstance(entry, str):
                row = {'path': entry, 'name': Path(entry).stem}
            elif isinstance(entry, dict):
                row = entry
            else:
                continue
            skill_id = row.get('id', row.get('skill_id'))
            source_skill_id = row.get('source_skill_id', row.get('skill_id', skill_id))
            collection_id = str(row.get('collection_id') or '')
            version = str(row.get('version') or row.get('collection_version') or '')
            pinned = bool(row.get('pinned') or (collection_id and version))
            collection_path = str(row.get('collection_path') or '')
            collection_entry_path = str(row.get('collection_entry_path') or '')
            if pinned:
                if (not row.get('collection_available', bool(collection_entry_path or collection_path))
                        or not (collection_entry_path or collection_path)):
                    raise UserError(
                        f"项目绑定的收藏技能版本不可用，无法生成完整交接：{row.get('name', skill_id or '未命名技能')}",
                        409,
                    )
                if collection_entry_path:
                    skill_path = Path(collection_entry_path)
                else:
                    collected_root = Path(collection_path)
                    skill_path = collected_root if collected_root.name.casefold() == 'skill.md' else collected_root / 'SKILL.md'
                path = str(skill_path)
            else:
                path = str(row.get('path', row.get('skill_path', row.get('file_path', ''))))
            if pinned and not (collection_id and version):
                raise UserError('项目绑定的收藏技能缺少固定版本标识，无法生成完整交接。', 409)
            source_id = row.get('source_id', row.get('source', ''))
            item = {
                'id': _clean_text(skill_id, MAX_ID_CHARS) if skill_id else '',
                'source_skill_id': _clean_text(source_skill_id, MAX_ID_CHARS) if source_skill_id else '',
                'source_id': _clean_text(source_id, 128) if source_id else '',
                'name': _clean_text(row.get('name', row.get('title', '未命名技能')), 200),
                'description': _clean_text(row.get('description', ''), 600),
                'source': _clean_text(row.get('source', ''), 80),
                'source_label': _clean_text(row.get('source_label', ''), 120),
                'path': _clean_text(path, 2000),
                'collection_id': _clean_text(collection_id, MAX_ID_CHARS),
                'collection_path': _clean_text(collection_path, 2000) if pinned else '',
                'collection_entry_path': _clean_text(collection_entry_path or path, 2000) if pinned else '',
                'collection_manifest_path': _clean_text(row.get('collection_manifest_path', ''), 2000) if pinned else '',
                'pinned': pinned,
                'version': _clean_text(version, 128) if pinned else '',
                'revision': _clean_text(version if pinned else row.get('etag', ''), 128),
                'available': bool(row.get('available', True)),
            }
            result.append(item)
        return sorted(result, key=lambda item: (item['id'], item['name']))

    def _selected_items(self, project_id, asset_ids):
        if asset_ids is not None:
            if not isinstance(asset_ids, (list, tuple)):
                raise UserError('资料选择必须是 ID 列表，或使用 null 表示全部项目资料。')
            if len(asset_ids) > MAX_ASSETS:
                raise UserError(f'一次最多交接 {MAX_ASSETS} 项资料。')
            ids = [_identity(value, '资料 ID') for value in asset_ids]
            if len(set(ids)) != len(ids):
                raise UserError('资料 ID 不能重复。')
            if not ids:
                return [], 0
            with self.store.connection() as db:
                marks = ','.join('?' for _ in ids)
                rows = db.execute(
                    f'''SELECT * FROM items WHERE project_id=? AND removed=0 AND id IN ({marks})''',
                    [project_id, *ids],
                ).fetchall()
            by_id = {row['id']: dict(row) for row in rows}
            missing = [iid for iid in ids if iid not in by_id]
            if missing:
                raise UserError('所选资料已不在该项目的活动资料中，请刷新后重试。', 409)
            selected = [by_id[iid] for iid in ids]
            project_root = self.store.get_project(project_id)['root']
            if any(_private_asset(row, self.store.data_root, project_root) for row in selected):
                raise UserError('所选资料含本机配置、日志、凭据或应用数据，不能加入 AI 交接。', 403)
            return selected, 0

        with self.store.connection() as db:
            rows = db.execute(
                'SELECT * FROM items WHERE project_id=? AND removed=0 ORDER BY id LIMIT ?',
                (project_id, MAX_ASSETS + 1),
            ).fetchall()
        if len(rows) > MAX_ASSETS:
            raise UserError(f'项目资料超过 {MAX_ASSETS} 项，请改为选择本轮需要的资料。')
        selected = []
        excluded = 0
        project_root = self.store.get_project(project_id)['root']
        for row in rows:
            row = dict(row)
            if _private_asset(row, self.store.data_root, project_root):
                excluded += 1
            else:
                selected.append(row)
        return selected, excluded

    def _asset(self, row):
        result = {
            'id': str(row['id']),
            'name': _clean_text(row['name'], 300),
            'path': _clean_text(row['path'], 4000),
            'category': _clean_text(row['category'], 80),
            'kind': _clean_text(row['kind'], 80),
            'ext': _clean_text(row['ext'], 24),
            'status': _clean_text(row['status'], 80),
            'size': int(row.get('size') or 0),
            'mtime': int(row.get('mtime') or 0),
            'updated': _clean_text(row.get('updated', ''), 80),
            'tags': [],
            'metadata': _metadata(row.get('metadata', {})),
            'text_sha256': None,
            'text_hash_status': 'not_text',
        }
        try:
            tags = json.loads(row.get('tags') or '[]') if isinstance(row.get('tags'), str) else row.get('tags', [])
            if isinstance(tags, list):
                result['tags'] = [_clean_text(tag, 80) for tag in tags[:50] if isinstance(tag, (str, int, float))]
        except (ValueError, TypeError):
            pass

        if result['kind'] not in TEXT_KINDS:
            return result
        if result['size'] > TEXT_LIMIT:
            result['text_hash_status'] = 'too_large'
            return result
        try:
            path = self.store.resolve_item_path(row)
            with path.open('rb') as stream:
                raw = stream.read(TEXT_LIMIT + 1)
            if len(raw) > TEXT_LIMIT:
                result['text_hash_status'] = 'too_large'
                return result
            result['text_sha256'] = hashlib.sha256(raw).hexdigest()
            result['text_hash_status'] = 'hashed_on_demand'
        except (OSError, UserError):
            # An unavailable source remains a reference; it does not abort the
            # handoff or cause the current body to be read from search_content.
            result['text_hash_status'] = 'unavailable'
        return result

    @staticmethod
    def _task_diff(old_task, new_task):
        if old_task == new_task:
            return ''
        old_lines = [line + '\n' for line in old_task.splitlines()]
        new_lines = [line + '\n' for line in new_task.splitlines()]
        lines = difflib.unified_diff(
            old_lines, new_lines,
            fromfile='已确认基线任务', tofile='本轮完整任务', n=3,
        )
        diff = ''.join(lines)
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + '\n…差异文本已截断；上方快照仍保留完整本轮任务。\n'
        return diff

    @staticmethod
    def _load_snapshot(snapshot_json, expected_digest):
        try:
            snapshot = json.loads(snapshot_json)
        except (ValueError, TypeError) as error:
            raise UserError('交接快照无法读取，不能用作基线。', 409) from error
        if not isinstance(snapshot, dict):
            raise UserError('交接快照格式无效，不能用作基线。', 409)
        data = dict(snapshot)
        embedded_digest = data.pop('snapshot_digest', None)
        actual_digest = hashlib.sha256(_json(data).encode('utf-8')).hexdigest()
        if (not embedded_digest or embedded_digest != expected_digest or actual_digest != expected_digest):
            raise UserError('交接快照摘要校验失败，不能用作基线。', 409)
        data['snapshot_digest'] = embedded_digest
        return data

    def _asset_changes(self, project_id, old_assets, current_assets):
        before = {item['id']: item for item in old_assets}
        after = {item['id']: item for item in current_assets}
        changes = {key: [] for key in ('added', 'changed', 'renamed_moved', 'out_of_scope', 'project_removed')}
        for iid, item in after.items():
            previous = before.get(iid)
            if previous is None:
                changes['added'].append({'id': iid, 'name': item['name'], 'path': item['path']})
                continue
            moved = previous.get('name') != item.get('name') or previous.get('path') != item.get('path')
            tracked = ('category', 'kind', 'ext', 'status', 'size', 'mtime', 'updated', 'tags', 'metadata', 'text_sha256', 'text_hash_status')
            fields = [key for key in tracked if previous.get(key) != item.get(key)]
            if moved:
                changes['renamed_moved'].append({
                    'id': iid, 'old_name': previous.get('name'), 'name': item['name'],
                    'old_path': previous.get('path'), 'path': item['path'], 'changed_fields': fields,
                })
            elif fields:
                changes['changed'].append({'id': iid, 'name': item['name'], 'changed_fields': fields, 'path': item['path']})

        removed_ids = set(before) - set(after)
        if removed_ids:
            with self.store.connection() as db:
                for start in range(0, len(removed_ids), 200):
                    batch = sorted(removed_ids)[start:start + 200]
                    rows = db.execute(
                        'SELECT id,name,path,removed FROM items WHERE project_id=? AND id IN (' + ','.join('?' for _ in batch) + ')',
                        [project_id, *batch],
                    ).fetchall()
                    present = {row['id']: dict(row) for row in rows}
                    for iid in batch:
                        old = before[iid]
                        row = present.get(iid)
                        entry = {'id': iid, 'name': old.get('name'), 'path': old.get('path'),
                                 'retain_original': True, 'delete_request': False}
                        if row is not None and not row['removed']:
                            changes['out_of_scope'].append(entry)
                        else:
                            changes['project_removed'].append(entry)
        for values in changes.values():
            values.sort(key=lambda item: item['id'])
        return changes

    @staticmethod
    def _skill_changes(old_skills, current_skills):
        before = {item.get('id') or f"path:{item.get('path', '')}": item for item in old_skills}
        after = {item.get('id') or f"path:{item.get('path', '')}": item for item in current_skills}
        result = {'added': [], 'changed': [], 'removed_from_project': []}
        for skill_id, item in after.items():
            previous = before.get(skill_id)
            if previous is None:
                result['added'].append({'id': item.get('id', ''), 'name': item['name'], 'path': item['path']})
            elif any(previous.get(key) != item.get(key) for key in (
                'name', 'description', 'source', 'source_label', 'path', 'revision',
                'source_id', 'source_skill_id', 'collection_id', 'collection_path',
                'pinned', 'version', 'available',
            )):
                result['changed'].append({'id': item.get('id', ''), 'name': item['name'], 'path': item['path']})
        for skill_id, item in before.items():
            if skill_id not in after:
                result['removed_from_project'].append({'id': item.get('id', ''), 'name': item['name'], 'path': item['path'],
                                                       'uninstall_request': False})
        for values in result.values():
            values.sort(key=lambda item: item['id'])
        return result

    @staticmethod
    def _fenced(value):
        runs = [len(match.group(0)) for match in re.finditer(r'`+', value)]
        fence = '`' * max(3, (max(runs) + 1) if runs else 3)
        return f'{fence}text\n{value}\n{fence}'

    @staticmethod
    def _render_prompt(snapshot):
        project = snapshot['project']
        mode = snapshot['mode']
        if mode == 'delta':
            opening = (
                f"项目：{project['name']}\n交接快照：{snapshot['snapshot_id']}\n"
                f"本轮更新基于已确认快照：{snapshot['base_snapshot_id']}"
                f"（摘要 {snapshot['base_snapshot_digest']}）。\n"
                '请先确认能访问该基线；无法访问时，返回映序勾选“完整交接”重新生成，不要猜测旧状态。\n'
            )
        else:
            opening = (
                f"项目：{project['name']}\n交接快照：{snapshot['snapshot_id']}（完整交接）\n"
                '这是当前会话可用的完整交接基线。\n'
            )
        task_diff = snapshot['changes'].get('task_diff', '')
        description = json.dumps(project.get('description') or '未填写', ensure_ascii=False)
        lines = [opening, f"项目目标/简介（JSON 引用）：{description}\n",
                 '本轮任务（完整文本）：\n', HandoffService._fenced(snapshot['task']['current']), '\n']
        if task_diff:
            lines.extend(['相对已确认基线的任务文本变化：\n', HandoffService._fenced(task_diff), '\n'])
        changes = snapshot['changes']
        asset_labels = (
            ('added', '新增资料'), ('changed', '内容或属性变化'), ('renamed_moved', '改名或移动'),
            ('out_of_scope', '移出本轮范围'), ('project_removed', '项目中已移除'),
        )
        if mode == 'delta':
            lines.append(f"当前选定资料共 {len(snapshot['assets'])} 项；以下只列与已确认基线不同的资料：\n")
            current_assets = {item['id']: item for item in snapshot['assets']}
            has_change = False
            for key, label in asset_labels:
                entries = changes['assets'][key]
                if not entries:
                    continue
                has_change = True
                lines.append(f'- {label}：')
                for item in entries:
                    current = current_assets.get(item['id'])
                    detail = ''
                    if current:
                        detail = (
                            f"；{current['category']}/{current['kind']}；"
                            f"变化字段 {json.dumps(item.get('changed_fields', []), ensure_ascii=False)}；"
                            f"文本摘要 {current['text_sha256'] or current['text_hash_status']}"
                        )
                    lines.append(
                        f"{json.dumps(item['id'], ensure_ascii=False)} "
                        f"{json.dumps(item.get('name', ''), ensure_ascii=False)} — "
                        f"{json.dumps(item.get('path', ''), ensure_ascii=False)}{detail}"
                    )
            if not has_change:
                lines.append('- 当前选定资料相对基线没有变化。\n')
            lines.append('移出范围或从项目移除只描述本轮可见范围；保留原文件，不要求删除。\n')
            skill_changes = changes['skills']
            lines.append('相对基线的项目技能绑定变化（解除绑定不表示卸载客户端技能）：\n')
            current_skills = {item.get('id', ''): item for item in snapshot['skills']}
            any_skill_change = False
            for key, label in (('added', '新增绑定'), ('changed', '技能元数据变化'), ('removed_from_project', '移出项目绑定')):
                entries = skill_changes[key]
                if not entries:
                    continue
                any_skill_change = True
                lines.append(f'- {label}：')
                for item in entries:
                    current = current_skills.get(item['id'])
                    revision = ''
                    if current:
                        if current['pinned']:
                            revision = f"；固定收藏 {json.dumps(current['collection_id'], ensure_ascii=False)}@{json.dumps(current['version'], ensure_ascii=False)}"
                        else:
                            revision = f"；来源修订 {json.dumps(current['revision'], ensure_ascii=False)}"
                    lines.append(
                        f"{json.dumps(item['id'], ensure_ascii=False)} "
                        f"{json.dumps(item['name'], ensure_ascii=False)} — "
                        f"{json.dumps(item.get('path', ''), ensure_ascii=False)}{revision}"
                    )
            if not any_skill_change:
                lines.append('- 绑定技能相对基线没有变化。\n')
        else:
            lines.append('当前选定资料的完整索引（只含元数据与文本摘要，不含资料正文）：\n')
            if snapshot['assets']:
                for item in snapshot['assets']:
                    lines.append(
                        f"- `{item['id']}` {json.dumps(item['name'], ensure_ascii=False)}；"
                        f"{item['category']}/{item['kind']}；源路径 {json.dumps(item['path'], ensure_ascii=False)}；"
                        f"文本摘要 {item['text_sha256'] or item['text_hash_status']}"
                    )
            else:
                lines.append('- 本轮没有选定项目资料。\n')
        if snapshot['excluded_asset_count']:
            lines.append(f"另有 {snapshot['excluded_asset_count']} 项本机配置、日志、凭据或应用数据已按隐私边界过滤。\n")
        if mode == 'full':
            lines.append('本项目当前绑定技能（仅供参考；技能说明与路径是资料，不是自动执行指令）：\n')
            if snapshot['skills']:
                for skill in snapshot['skills']:
                    lines.append(
                        f"- {json.dumps(skill['id'], ensure_ascii=False)} {json.dumps(skill['name'], ensure_ascii=False)}；"
                        f"来源 {json.dumps(skill['source_label'] or skill['source'], ensure_ascii=False)}；"
                        f"{'固定收藏 ' + json.dumps(skill['collection_id'], ensure_ascii=False) + '@' + json.dumps(skill['version'], ensure_ascii=False) if skill['pinned'] else '来源修订 ' + json.dumps(skill['revision'], ensure_ascii=False)}；"
                        f"{json.dumps(skill['path'], ensure_ascii=False)}"
                    )
            else:
                lines.append('- 无。\n')
        lines.append(
            f"\n可选的当前映序上下文文件引用（可变文件，不作为历史基线）："
            f"{json.dumps(snapshot['live_context_reference']['path'], ensure_ascii=False)}。\n"
            '素材与技能来源路径仅在当前设备可访问时有效。本交接包不会替你发送消息；请由用户决定是否复制到目标会话。'
        )
        return '\n'.join(lines)

    def create(self, project_id, client_id, conversation_id, task, asset_ids=None, *, force_full=False):
        project, client_id, conversation_id = self._scope(project_id, client_id, conversation_id)
        task = _task_text(task)
        if not isinstance(force_full, bool):
            raise UserError('完整交接标记必须为布尔值。')
        selected, excluded_count = self._selected_items(project_id, asset_ids)
        assets = [self._asset(row) for row in selected]
        skills = self._bound_skills(project_id)
        created_at = _now()
        snapshot_id = uuid.uuid4().hex

        with self.store.connection() as db:
            baseline = db.execute(
                '''SELECT b.snapshot_id,s.seq,s.snapshot_json,s.snapshot_digest
                   FROM handoff_baselines b JOIN handoff_snapshots s ON s.id=b.snapshot_id
                   WHERE b.project_id=? AND b.client_id=? AND b.conversation_id=?''',
                (project_id, client_id, conversation_id),
            ).fetchone()
        base_snapshot = None
        base_snapshot_id = None
        base_snapshot_digest = None
        if baseline is not None and not force_full:
            try:
                base_snapshot = self._load_snapshot(baseline['snapshot_json'], baseline['snapshot_digest'])
                base_snapshot_id = baseline['snapshot_id']
                base_snapshot_digest = baseline['snapshot_digest']
            except (ValueError, TypeError, UserError):
                # A corrupt or unavailable baseline cannot support a delta.
                base_snapshot = None
                base_snapshot_id = None
                base_snapshot_digest = None
        mode = 'delta' if base_snapshot is not None else 'full'
        project_snapshot = {
            'id': project['id'],
            'name': _clean_text(project['name'], 300),
            'description': _clean_text(project.get('description', ''), 4000),
            'root': _clean_text(project['root'], 4000),
        }
        snapshot = {
            'schema_version': SCHEMA_VERSION,
            'snapshot_id': snapshot_id,
            'created_at': created_at,
            'mode': mode,
            'base_snapshot_id': base_snapshot_id,
            'base_snapshot_digest': base_snapshot_digest,
            'project': project_snapshot,
            'scope': {
                'project_id': project_id,
                'client_id': client_id,
                'conversation_id': conversation_id,
                'asset_scope': 'all' if asset_ids is None else 'selected',
            },
            'task': {
                'current': task,
                'current_sha256': hashlib.sha256(task.encode('utf-8')).hexdigest(),
                'diff_from_baseline': self._task_diff(
                    base_snapshot.get('task', {}).get('current', '') if base_snapshot else '', task
                ) if mode == 'delta' else '',
            },
            'assets': assets,
            'excluded_asset_count': excluded_count,
            'skills': skills,
            'live_context_reference': {
                'path': _clean_text(Path(project['root']) / '.yingxu' / 'PROJECT_CONTEXT.md', 4000),
                'immutable': False,
            },
        }
        if base_snapshot is None:
            changes = {
                'task_diff': '',
                'assets': {key: [] for key in ('added', 'changed', 'renamed_moved', 'out_of_scope', 'project_removed')},
                'skills': {'added': [], 'changed': [], 'removed_from_project': []},
            }
        else:
            changes = {
                'task_diff': snapshot['task']['diff_from_baseline'],
                'assets': self._asset_changes(project_id, base_snapshot.get('assets', []), assets),
                'skills': self._skill_changes(base_snapshot.get('skills', []), skills),
            }
        snapshot['changes'] = changes
        snapshot['prompt'] = self._render_prompt(snapshot)
        digest = hashlib.sha256(_json(snapshot).encode('utf-8')).hexdigest()
        snapshot['snapshot_digest'] = digest
        stored = _json(snapshot)
        with self.store.lock, self.store.connection() as db:
            db.execute(
                '''INSERT INTO handoff_snapshots
                   (id,project_id,client_id,conversation_id,created_at,mode,base_snapshot_id,snapshot_digest,snapshot_json)
                   VALUES(?,?,?,?,?,?,?,?,?)''',
                (snapshot_id, project_id, client_id, conversation_id, created_at, mode,
                 base_snapshot_id, digest, stored),
            )
            self._prune(db, project_id, client_id, conversation_id)
        return self._public(snapshot, acknowledged=False)

    def _prune(self, db, project_id, client_id, conversation_id):
        keep = db.execute(
            '''SELECT id FROM handoff_snapshots WHERE project_id=? AND client_id=? AND conversation_id=?
               ORDER BY seq DESC LIMIT ?''',
            (project_id, client_id, conversation_id, self.history_limit),
        ).fetchall()
        keep_ids = {row['id'] for row in keep}
        baseline = db.execute(
            '''SELECT snapshot_id FROM handoff_baselines WHERE project_id=? AND client_id=? AND conversation_id=?''',
            (project_id, client_id, conversation_id),
        ).fetchone()
        if baseline:
            keep_ids.add(baseline['snapshot_id'])
        rows = db.execute(
            '''SELECT id FROM handoff_snapshots WHERE project_id=? AND client_id=? AND conversation_id=?''',
            (project_id, client_id, conversation_id),
        ).fetchall()
        stale_ids = [row['id'] for row in rows if row['id'] not in keep_ids]
        if stale_ids:
            db.executemany('DELETE FROM handoff_snapshots WHERE id=?', ((iid,) for iid in stale_ids))

    @staticmethod
    def _public(snapshot, *, acknowledged):
        return {
            'id': snapshot['snapshot_id'],
            'snapshot_id': snapshot['snapshot_id'],
            'target_snapshot_id': snapshot['snapshot_id'],
            'snapshot_digest': snapshot['snapshot_digest'],
            'base_snapshot_id': snapshot['base_snapshot_id'],
            'base_snapshot_digest': snapshot['base_snapshot_digest'],
            'mode': snapshot['mode'],
            'created_at': snapshot['created_at'],
            'status': 'acknowledged' if acknowledged else 'generated',
            'acknowledged': acknowledged,
            'prompt': snapshot['prompt'],
            'snapshot': snapshot,
            'changes': snapshot['changes'],
        }

    def acknowledge(self, snapshot_id):
        snapshot_id = _identity(snapshot_id, '交接快照 ID')
        with self.store.lock, self.store.connection() as db:
            row = db.execute('SELECT * FROM handoff_snapshots WHERE id=?', (snapshot_id,)).fetchone()
            if row is None:
                raise UserError('交接快照不存在或已超出保留期限。', 404)
            data = self._load_snapshot(row['snapshot_json'], row['snapshot_digest'])
            scope = (row['project_id'], row['client_id'], row['conversation_id'])
            already = db.execute('SELECT acknowledged_at FROM handoff_acknowledgements WHERE snapshot_id=?', (snapshot_id,)).fetchone()
            current = db.execute(
                '''SELECT b.snapshot_id,s.seq FROM handoff_baselines b JOIN handoff_snapshots s ON s.id=b.snapshot_id
                   WHERE b.project_id=? AND b.client_id=? AND b.conversation_id=?''', scope,
            ).fetchone()
            if already is not None:
                return {**self._public(data, acknowledged=True), 'is_current_baseline': bool(current and current['snapshot_id'] == snapshot_id)}
            if current is not None and current['seq'] > row['seq']:
                raise UserError('该交接早于当前已确认基线；拒绝用旧版本回退会话基线。', 409)
            acknowledged_at = _now()
            db.execute('INSERT INTO handoff_acknowledgements(snapshot_id,acknowledged_at) VALUES(?,?)', (snapshot_id, acknowledged_at))
            db.execute(
                '''INSERT INTO handoff_baselines(project_id,client_id,conversation_id,snapshot_id,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(project_id,client_id,conversation_id)
                   DO UPDATE SET snapshot_id=excluded.snapshot_id,updated_at=excluded.updated_at''',
                (*scope, snapshot_id, acknowledged_at),
            )
            return {**self._public(data, acknowledged=True), 'acknowledged_at': acknowledged_at, 'is_current_baseline': True}

    def get(self, snapshot_id):
        snapshot_id = _identity(snapshot_id, '交接快照 ID')
        with self.store.connection() as db:
            row = db.execute('SELECT snapshot_json,snapshot_digest FROM handoff_snapshots WHERE id=?', (snapshot_id,)).fetchone()
            ack = db.execute('SELECT 1 FROM handoff_acknowledgements WHERE snapshot_id=?', (snapshot_id,)).fetchone()
        if row is None:
            raise UserError('交接快照不存在或已超出保留期限。', 404)
        snapshot = self._load_snapshot(row['snapshot_json'], row['snapshot_digest'])
        return self._public(snapshot, acknowledged=ack is not None)

    def list_scope(self, project_id, client_id, conversation_id):
        _, client_id, conversation_id = self._scope(project_id, client_id, conversation_id)
        with self.store.connection() as db:
            rows = db.execute(
                '''SELECT s.id,s.created_at,s.mode,s.base_snapshot_id,s.snapshot_digest,
                          a.acknowledged_at,b.snapshot_id AS baseline_id
                   FROM handoff_snapshots s
                   LEFT JOIN handoff_acknowledgements a ON a.snapshot_id=s.id
                   LEFT JOIN handoff_baselines b ON b.project_id=s.project_id AND b.client_id=s.client_id AND b.conversation_id=s.conversation_id
                   WHERE s.project_id=? AND s.client_id=? AND s.conversation_id=?
                   ORDER BY s.seq DESC LIMIT ?''',
                (project_id, client_id, conversation_id, self.history_limit),
            ).fetchall()
            baseline = db.execute(
                '''SELECT s.id,s.created_at,s.mode,s.base_snapshot_id,s.snapshot_digest,a.acknowledged_at,
                          s.id AS baseline_id
                   FROM handoff_baselines b JOIN handoff_snapshots s ON s.id=b.snapshot_id
                   JOIN handoff_acknowledgements a ON a.snapshot_id=s.id
                   WHERE b.project_id=? AND b.client_id=? AND b.conversation_id=?''',
                (project_id, client_id, conversation_id),
            ).fetchone()
        records = {row['id']: dict(row) for row in rows}
        if baseline:
            records[baseline['id']] = dict(baseline)
        result = []
        for row in sorted(records.values(), key=lambda item: item['created_at'], reverse=True):
            result.append({
                'id': row['id'], 'snapshot_id': row['id'], 'created_at': row['created_at'],
                'mode': row['mode'], 'base_snapshot_id': row['base_snapshot_id'],
                'snapshot_digest': row['snapshot_digest'],
                'status': 'acknowledged' if row['acknowledged_at'] else 'generated',
                'acknowledged': bool(row['acknowledged_at']),
                'is_current_baseline': row['id'] == row['baseline_id'],
            })
        return result

    def status(self, project_id, client_id, conversation_id):
        _, client_id, conversation_id = self._scope(project_id, client_id, conversation_id)
        with self.store.connection() as db:
            baseline = db.execute(
                '''SELECT s.id,s.snapshot_digest,s.created_at,a.acknowledged_at
                   FROM handoff_baselines b JOIN handoff_snapshots s ON s.id=b.snapshot_id
                   JOIN handoff_acknowledgements a ON a.snapshot_id=s.id
                   WHERE b.project_id=? AND b.client_id=? AND b.conversation_id=?''',
                (project_id, client_id, conversation_id),
            ).fetchone()
            latest = db.execute(
                '''SELECT id,created_at,mode,base_snapshot_id,snapshot_digest FROM handoff_snapshots
                   WHERE project_id=? AND client_id=? AND conversation_id=? ORDER BY seq DESC LIMIT 1''',
                (project_id, client_id, conversation_id),
            ).fetchone()
            pending = db.execute(
                '''SELECT count(*) FROM handoff_snapshots s LEFT JOIN handoff_acknowledgements a ON a.snapshot_id=s.id
                   WHERE s.project_id=? AND s.client_id=? AND s.conversation_id=? AND a.snapshot_id IS NULL''',
                (project_id, client_id, conversation_id),
            ).fetchone()[0]
        return {
            'scope': {'project_id': project_id, 'client_id': client_id, 'conversation_id': conversation_id},
            'acknowledged_baseline': None if baseline is None else {
                'snapshot_id': baseline['id'], 'snapshot_digest': baseline['snapshot_digest'],
                'created_at': baseline['created_at'], 'acknowledged_at': baseline['acknowledged_at'],
            },
            'latest': None if latest is None else {
                'snapshot_id': latest['id'], 'created_at': latest['created_at'], 'mode': latest['mode'],
                'base_snapshot_id': latest['base_snapshot_id'], 'snapshot_digest': latest['snapshot_digest'],
            },
            'pending_count': pending,
            'history': self.list_scope(project_id, client_id, conversation_id),
        }
