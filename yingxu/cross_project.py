"""Move catalogue identities across projects without overwriting files or losing history.

Copies are verified before the database commit; original files survive every
pre-commit failure. A durable receipt preserves evidence if cleanup is interrupted.
"""
from __future__ import annotations
import hashlib
import json
import os
import time
from pathlib import Path

from .organize import _ids, _rename
from .migration_links import rewrite_markdown
from .store import UserError, clean_path, has_link, now, uid, TEXT_LIMIT

MAX_BYTES = 2 * 1024**3
MAX_LINK_BYTES = 16 * 1024**2


def _digest(path):
    clean_path(path)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _receipt(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def _copy(source, target):
    with source.open('rb') as incoming, target.open('xb') as outgoing:
        for chunk in iter(lambda: incoming.read(1024 * 1024), b''):
            outgoing.write(chunk)
        outgoing.flush(); os.fsync(outgoing.fileno())


def _commit(db):
    db.commit()


def move_items(organize, ids, target_project_id, category, folder_id=None):
    ids = _ids(ids)
    if not isinstance(target_project_id, str) or not target_project_id:
        raise UserError('请选择目标项目。')
    folder_id = None if folder_id in ('', None, 'root') else folder_id
    store = organize.store
    with store.lock, store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        marks = ','.join('?' for _ in ids)
        rows = {r['id']: dict(r) for r in db.execute(
            'SELECT * FROM items WHERE removed=0 AND id IN (' + marks + ')', ids)}
        if len(rows) != len(ids):
            raise UserError('部分条目已不存在或已移入回收站。', 404)
        items = [rows[i] for i in ids]
        pids = {i['project_id'] for i in items}
        if len(pids) != 1:
            raise UserError('一次只能移动同一个项目的条目。')
        source_id = items[0]['project_id']
        if source_id == target_project_id:
            raise UserError('跨项目移动需要选择另一个项目。')
        source_project = store._project(db, source_id)
        target_project = store._project(db, target_project_id)
        root = clean_path(source_project['root'])
        target_root = clean_path(target_project['root'])
        if root == target_root or root.is_relative_to(target_root) or target_root.is_relative_to(root):
            raise UserError('两个项目目录存在包含关系，请先整理项目位置。', 409)
        destination = organize.folder_path(target_project_id, category, folder_id)
        selected = set(ids)
        for relation in db.execute('SELECT source_id,target_id FROM relations WHERE source_id IN (' + marks + ') OR target_id IN (' + marks + ')', ids + ids):
            if relation['source_id'] not in selected or relation['target_id'] not in selected:
                raise UserError('所选文件与未选文件存在关联，请一起选择关联文件，或先解除关联后移动。', 409)
        groups = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='resource_group_members'").fetchone():
            groups = [r[0] for r in db.execute('SELECT DISTINCT group_id FROM resource_group_members WHERE item_id IN (' + marks + ')', ids)]
            for gid in groups:
                members = {r[0] for r in db.execute('SELECT item_id FROM resource_group_members WHERE group_id=?', (gid,))}
                if not members <= selected:
                    raise UserError('所选文件属于素材组，请选择整个素材组，或先从组中移出后再移动。', 409)
            count = db.execute('SELECT count(*) FROM resource_groups WHERE project_id=?', (target_project_id,)).fetchone()[0]
            if count + len(groups) > 500:
                raise UserError('目标项目的素材组数量将超过 500 个。', 409)
        plans = []; reserved = set(); total = 0
        for item in items:
            source = store.resolve_item_path(item, db)
            owned = source.is_relative_to(root)
            target = destination / source.name if owned else source
            info = source.stat()
            if owned and info.st_nlink != 1:
                raise UserError('所选文件包含硬链接，请先复制为独立文件再移动。', 409)
            if owned and db.execute('SELECT 1 FROM items WHERE path=? AND id<>?', (str(source), item['id'])).fetchone():
                raise UserError('所选文件仍被其他条目引用，不能直接搬走原文件；请使用复制导入。', 409)
            key = os.path.normcase(str(target))
            if key in reserved or (owned and (target.exists() or target.is_symlink())):
                raise UserError('目标已有同名文件：' + target.name + '；未移动任何文件。', 409)
            if db.execute('SELECT 1 FROM items WHERE project_id=? AND path=?', (target_project_id, str(target))).fetchone():
                raise UserError('目标项目已有该路径的记录，请先整理重复条目。', 409)
            reserved.add(key)
            total += info.st_size if owned else 0
            if total > MAX_BYTES:
                raise UserError('一次跨项目移动最多 2 GiB，请分批移动。')
            plans.append({'item': item, 'source': source, 'target': target, 'owned': owned,
                          'before': _digest(source) if owned else '', 'content': None})
        mapping = {p['source']: p['target'] for p in plans}
        # A partial transfer must not strand links in the remaining documents.
        markdown = db.execute("SELECT * FROM items WHERE project_id=? AND kind='markdown' AND removed=0 LIMIT 1001", (source_id,)).fetchall()
        if len(markdown) > 1000:
            raise UserError('项目文稿较多，无法完整核对跨项目链接；请先缩小项目或使用复制导入。', 409)
        budget = 0
        by_id = {p['item']['id']: p for p in plans}
        warnings = []
        for row in markdown:
            item = dict(row); path = store.resolve_item_path(item, db)
            size = path.stat().st_size; budget += size
            if size > TEXT_LIMIT or budget > MAX_LINK_BYTES:
                raise UserError('项目文稿超过链接核对容量，请先使用复制导入，保留原文稿链接。', 409)
            raw = path.read_bytes(); plan = by_id.get(item['id'])
            if plan and plan['owned']:
                rewritten = rewrite_markdown(raw, path, plan['target'], mapping)
                if rewritten['warnings']:
                    raise UserError('文稿含未随批次移动或无法核对的本地链接，请一起选择引用文件，或先使用复制导入：' + item['name'], 409)
                if rewritten['changed']:
                    plan['content'] = rewritten['content']
            else:
                rewritten = rewrite_markdown(raw, path, path, mapping)
                if rewritten['changed']:
                    raise UserError('仍有文稿引用所选文件，请将该文稿一起移动，或先解除链接：' + item['name'], 409)
        operation = uid()
        receipt_root = store.data_root / 'cross-project-moves'
        receipt_root.mkdir(exist_ok=True)
        record_path = receipt_root / (operation + '.json')
        record = {'id': operation, 'state': 'preparing', 'source_project_id': source_id,
                  'project_id': target_project_id, 'files': []}
        created = []; committed = False
        try:
            for index, plan in enumerate(plans):
                if not plan['owned']:continue
                source, target = plan['source'], plan['target']
                temporary = destination / ('.yingxu-move-' + operation + '-' + str(index) + '.tmp')
                plan['temporary'] = temporary
                # Register before copying so partial writes are removed on error.
                created.append(temporary)
                _copy(source, temporary)
                if _digest(temporary) != plan['before'] or _digest(source) != plan['before']:
                    raise UserError('复制过程中原文件发生变化，未移动任何记录。', 409)
                if plan['content'] is not None:
                    # Preserve exact original bytes in normal per-item history.
                    vid = uid(); backup = store.data_root / 'versions' / plan['item']['id'] / (vid + source.suffix)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    created.append(backup); _copy(source, backup)
                    db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?)', (vid, plan['item']['id'], now(), source.stat().st_size, str(backup), plan['before']))
                    with temporary.open('wb') as stream:
                        stream.write(plan['content']); stream.flush(); os.fsync(stream.fileno())
                plan['after'] = _digest(temporary)
                record['files'].append({'id': plan['item']['id'], 'source': str(source), 'target': str(target),
                                        'source_sha256': plan['before'], 'target_sha256': plan['after']})
            _receipt(record_path, record)
            for plan in plans:
                if plan['owned']:
                    if _digest(plan['source']) != plan['before']:
                        raise UserError('原文件发生变化，请刷新后重试。', 409)
                    _rename(plan['temporary'], plan['target'])
                    created.append(plan['target'])
                # File-specific source avoids access leaks across project roots.
                target = plan['target']; iid = plan['item']['id']
                db.execute('INSERT OR IGNORE INTO sources(id,project_id,path,category,is_file) VALUES(?,?,?,?,1)', (uid(), target_project_id, str(target), category))
                sid = db.execute('SELECT id FROM sources WHERE project_id=? AND path=?', (target_project_id, str(target))).fetchone()[0]
                info = target.stat()
                db.execute('UPDATE items SET project_id=?,source_id=?,path=?,category=?,folder_id=?,size=?,mtime=?,updated=?,removed_batch=NULL WHERE id=?',
                           (target_project_id, sid, str(target), category, folder_id, info.st_size, info.st_mtime_ns, now(), iid))
                if plan['content'] is not None:
                    from .store import decode_text
                    db.execute('UPDATE items SET search_content=? WHERE id=?', (decode_text(plan['content'])[0][:500000], iid))
                store._search_row(db, iid)
            for gid in groups:
                db.execute('UPDATE resource_groups SET project_id=?,updated=?,revision=revision+1 WHERE id=?', (target_project_id, now(), gid))
            # Retire obsolete file sources so a future rescan cannot resurrect a move.
            for plan in plans:
                db.execute('DELETE FROM sources WHERE id=? AND is_file=1 AND NOT EXISTS(SELECT 1 FROM items WHERE source_id=sources.id)', (plan['item']['source_id'],))
            for pid in (source_id, target_project_id):
                db.execute('UPDATE projects SET updated=? WHERE id=?', (now(), pid))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='context_exports'").fetchone():
                    db.execute('INSERT INTO context_exports(project_id,requested_at,first_requested_at) VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET requested_at=excluded.requested_at,revision=context_exports.revision+1', (pid, time.time(), time.time()))
            _commit(db); committed = True
        except Exception:
            # A storage error may be reported after SQLite already committed.
            # Establish the durable catalogue state before deleting any copies.
            try:
                db.rollback()
                current = [db.execute('SELECT project_id,path FROM items WHERE id=?', (p['item']['id'],)).fetchone() for p in plans]
                committed = all(r and r['project_id']==target_project_id and r['path']==str(p['target']) for r,p in zip(current,plans))
                original = all(r and r['project_id']==source_id and r['path']==str(p['source']) for r,p in zip(current,plans))
            except Exception:
                raise UserError('无法确认移动是否提交，已保留两边文件和操作记录；请重启后核对项目。', 409)
            if committed:
                warnings.append('提交响应异常，已重新核对文件和项目记录，移动已完成。')
            elif original:
                retained = False
                published = {p['target']: p.get('after') for p in plans if p['owned']}
                for path in reversed(created):
                    try:
                        if path.exists():
                            if has_link(path) or (path in published and _digest(path)!=published[path]):
                                retained = True; continue
                            path.unlink()
                    except (OSError, UserError):retained = True
                if not retained and record_path.exists():record_path.unlink()
                if retained:raise UserError('移动未提交，原文件保留；部分临时副本无法清理，请核对操作记录。', 409)
                raise
            else:
                raise UserError('移动记录状态不一致，已保留两边文件和操作记录；请重启后核对。', 409)
        # Cleanup is deliberately after commit: crashes always leave a usable copy.
        for plan in plans:
            if not plan['owned']:continue
            try:
                if _digest(plan['source']) != plan['before'] or _digest(plan['target']) != plan['after']:
                    raise OSError('file changed after commit')
                plan['source'].unlink()
            except (OSError, UserError):
                warnings.append('已移动项目记录，但原位置的副本未清理，请核对：' + plan['source'].name)
        record['state'] = 'cleanup_pending' if warnings else 'done'
        try:_receipt(record_path, record)
        except OSError:warnings.append('文件已移动，操作记录未能更新。')
        returned = []
        for plan in plans:
            row = db.execute('SELECT id,project_id,source_id,name,category,folder_id,path,kind,ext FROM items WHERE id=?', (plan['item']['id'],)).fetchone()
            returned.append(dict(row))
        owned = sum(p['owned'] for p in plans)
        return {'ok': True, 'source_project_id': source_id, 'project_id': target_project_id,
                'items': returned, 'content_changed': [p['item']['id'] for p in plans if p['content'] is not None], 'stats': {'moved': owned, 'referenced': len(plans)-owned, 'unchanged': 0, 'copied': 0}, 'warnings': warnings}
