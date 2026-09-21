"""Logical project folders. Organisation never relocates project files."""
from __future__ import annotations

import hashlib
import json
import secrets
import time

from .store import UserError, now, uid


class ProjectLibrary:
    def __init__(self, store):
        self.store = store
        self.delete_plans = {}
        with store.lock, store.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS project_library_folders(
              id TEXT PRIMARY KEY, name TEXT NOT NULL,
              parent_id TEXT REFERENCES project_library_folders(id));
            CREATE TABLE IF NOT EXISTS project_library_entries(
              project_id TEXT PRIMARY KEY REFERENCES projects(id),
              folder_id TEXT REFERENCES project_library_folders(id), last_opened TEXT);
            CREATE INDEX IF NOT EXISTS project_library_parent ON project_library_folders(parent_id);
            CREATE INDEX IF NOT EXISTS project_library_folder ON project_library_entries(folder_id);
            CREATE TABLE IF NOT EXISTS project_folder_imports(
              source_path TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id));
            ''')

    @staticmethod
    def _body(body, allowed):
        if not isinstance(body, dict) or not body or set(body) - set(allowed):
            raise UserError('项目库参数无效。')

    @staticmethod
    def _name(value):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80 or any(ord(c) < 32 for c in value):
            raise UserError('分类名称请填写 1 至 80 个字符。')
        return value.strip()

    @staticmethod
    def _folder(db, folder_id):
        if not isinstance(folder_id, str):
            raise UserError('请选择有效的项目分类。')
        row = db.execute('SELECT * FROM project_library_folders WHERE id=?', (folder_id,)).fetchone()
        if row is None:
            raise UserError('项目分类不存在，请刷新后重试。', 404)
        return dict(row)

    def _parent(self, db, parent_id):
        if parent_id is not None:
            self._folder(db, parent_id)

    @staticmethod
    def _unique(db, name, parent_id, except_id=''):
        siblings = db.execute('SELECT id,name FROM project_library_folders WHERE parent_id IS ?', (parent_id,))
        if any(r['id'] != except_id and r['name'].casefold() == name.casefold() for r in siblings):
            raise UserError('这一层已有同名分类。', 409)

    def snapshot(self):
        with self.store.lock:
            projects = self.store.list_projects()
            with self.store.connection() as db:
                folders = [dict(r) for r in db.execute('SELECT * FROM project_library_folders ORDER BY name COLLATE NOCASE,id')]
                entries = {r['project_id']: dict(r) for r in db.execute('SELECT * FROM project_library_entries')}
            for project in projects:
                entry = entries.get(project['id'], {})
                project.update(folder_id=entry.get('folder_id'), last_opened=entry.get('last_opened'))
            recent = sorted((p for p in projects if p['last_opened']), key=lambda p: (p['last_opened'], p['id']), reverse=True)
            return {'folders': folders, 'projects': projects, 'recent_ids': [p['id'] for p in recent], 'total': len(projects)}

    def create_folder(self, body):
        self._body(body, {'name', 'parent_id'})
        name, parent = self._name(body.get('name')), body.get('parent_id')
        with self.store.lock, self.store.connection() as db:
            self._parent(db, parent)
            self._unique(db, name, parent)
            folder_id = uid()
            db.execute('INSERT INTO project_library_folders VALUES(?,?,?)', (folder_id, name, parent))
            return self._folder(db, folder_id)

    def update_folder(self, folder_id, body):
        self._body(body, {'name', 'parent_id'})
        with self.store.lock, self.store.connection() as db:
            folder = self._folder(db, folder_id)
            name = self._name(body.get('name', folder['name']))
            parent = body.get('parent_id', folder['parent_id'])
            self._parent(db, parent)
            cursor, seen = parent, set()
            while cursor is not None:
                if cursor == folder_id or cursor in seen:
                    raise UserError('不能把分类移入自身或自己的子分类。', 409)
                seen.add(cursor)
                cursor = self._folder(db, cursor)['parent_id']
            self._unique(db, name, parent, folder_id)
            db.execute('UPDATE project_library_folders SET name=?,parent_id=? WHERE id=?', (name, parent, folder_id))
            return self._folder(db, folder_id)

    def delete_folder(self, folder_id):
        with self.store.lock, self.store.connection() as db:
            self._folder(db, folder_id)
            if db.execute('SELECT 1 FROM project_library_folders WHERE parent_id=? LIMIT 1', (folder_id,)).fetchone():
                raise UserError('请先移走子分类，再删除这个空分类。', 409)
            if db.execute('SELECT 1 FROM project_library_entries e JOIN projects p ON p.id=e.project_id '
                          'WHERE e.folder_id=? AND p.removed=0 LIMIT 1', (folder_id,)).fetchone():
                raise UserError('请先移走分类中的项目，再删除空分类。', 409)
            db.execute('UPDATE project_library_entries SET folder_id=NULL WHERE folder_id=?', (folder_id,))
            db.execute('DELETE FROM project_library_folders WHERE id=?', (folder_id,))
            return {'deleted': True, 'id': folder_id}

    def _deletion_scope(self, db, folder_id):
        self._folder(db, folder_id)
        folders = [dict(row) for row in db.execute('SELECT * FROM project_library_folders ORDER BY id')]
        selected = {folder_id}
        while True:
            children = {row['id'] for row in folders if row['parent_id'] in selected}
            if children <= selected:
                break
            selected.update(children)
        folders = [row for row in folders if row['id'] in selected]
        projects = [dict(row) for row in db.execute('''SELECT p.*, e.folder_id FROM projects p
            JOIN project_library_entries e ON e.project_id=p.id WHERE p.removed=0 ORDER BY p.id''')
            if row['folder_id'] in selected]
        if len(projects) > 500:
            raise UserError('这个分类超过 500 个项目，请按子分类分批删除。', 409)
        members = []
        for project in projects:
            for table in ('items', 'folders'):
                members.extend((table, dict(row)) for row in db.execute(
                    'SELECT * FROM '+table+' WHERE project_id=? AND removed=0 ORDER BY id', (project['id'],)))
        signature = hashlib.sha256(json.dumps([folders, projects, members], sort_keys=True,
                                               ensure_ascii=False).encode()).hexdigest()
        return folders, projects, members, signature

    def preview_delete_contents(self, folder_id):
        with self.store.lock, self.store.connection() as db:
            folders, projects, members, signature = self._deletion_scope(db, folder_id)
            current = time.monotonic()
            self.delete_plans = {key: plan for key, plan in self.delete_plans.items() if plan['expires'] > current}
            if len(self.delete_plans) >= 32:
                self.delete_plans.pop(next(iter(self.delete_plans)))
            token = secrets.token_urlsafe(32)
            self.delete_plans[token] = dict(folder_id=folder_id, signature=signature, expires=current+600)
            return {'token': token, 'name': next(row['name'] for row in folders if row['id'] == folder_id),
                    'folder_count': len(folders), 'project_count': len(projects),
                    'item_count': sum(kind == 'items' for kind, row in members),
                    'projects': [{'id': row['id'], 'name': row['name'], 'root': row['root']} for row in projects]}

    def delete_contents(self, folder_id, body, organize):
        if not isinstance(body, dict) or set(body) != {'token'} or not isinstance(body['token'], str):
            raise UserError('请先预览分类及内容的删除范围。', 409)
        with self.store.lock, self.store.connection() as db:
            plan = self.delete_plans.pop(body['token'], None)
            if not plan or plan['expires'] <= time.monotonic() or plan['folder_id'] != folder_id:
                raise UserError('删除确认已失效，请重新预览。', 409)
            folders, projects, members, signature = self._deletion_scope(db, folder_id)
            if signature != plan['signature']:
                raise UserError('分类或项目内容已变化，尚未删除，请重新预览。', 409)
            # All project tombstones and hierarchy changes share one transaction.
            entries = []
            for project in projects:
                entities = [('project', project['id'])] + [
                    ('item' if kind == 'items' else 'folder', row['id'])
                    for kind, row in members if row['project_id'] == project['id']]
                result = organize._mark_batch(db, 'project', project['id'], project['id'], project['name'], entities)
                entries.append({'id': result['batch_id'], 'kind': 'project'})
            for folder in folders:
                db.execute('UPDATE project_library_entries SET folder_id=NULL WHERE folder_id=?', (folder['id'],))
            # Detach the selected tree first so foreign keys never depend on ordering.
            for folder in folders:
                db.execute('UPDATE project_library_folders SET parent_id=NULL WHERE id=?', (folder['id'],))
            for folder in folders:
                db.execute('DELETE FROM project_library_folders WHERE id=?', (folder['id'],))
            return {'ok': True, 'entries': entries, 'project_ids': [row['id'] for row in projects],
                    'folder_count': len(folders), 'project_count': len(projects)}

    def assign_project(self, project_id, body):
        self._body(body, {'folder_id'})
        folder = body['folder_id']
        with self.store.lock, self.store.connection() as db:
            self.store._project(db, project_id)
            self._parent(db, folder)
            db.execute('INSERT INTO project_library_entries(project_id,folder_id) VALUES(?,?) '
                       'ON CONFLICT(project_id) DO UPDATE SET folder_id=excluded.folder_id', (project_id, folder))
            return dict(db.execute('SELECT * FROM project_library_entries WHERE project_id=?', (project_id,)).fetchone())

    def visit(self, project_id):
        with self.store.lock, self.store.connection() as db:
            self.store._project(db, project_id)
            db.execute('INSERT INTO project_library_entries(project_id,last_opened) VALUES(?,?) '
                       'ON CONFLICT(project_id) DO UPDATE SET last_opened=excluded.last_opened', (project_id, now()))
            return dict(db.execute('SELECT * FROM project_library_entries WHERE project_id=?', (project_id,)).fetchone())
