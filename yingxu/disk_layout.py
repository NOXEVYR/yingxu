"""Discover managed category directories without creating or moving disk files."""
from pathlib import Path
import os

from .project_layout import category_paths


def location(project, path):
    root = Path(project['root'])
    path = Path(path)
    for category, relative in category_paths(project).items():
        base = root / relative
        if path == base or path.is_relative_to(base):
            return category, base
    return None


def register_directory(store, project, path):
    from .store import UserError, clean_path, now, uid
    path = clean_path(path)
    found = location(project, path)
    if not found or not path.is_dir():
        return
    category, base = found
    parts = path.relative_to(base).parts
    if len(parts) > 20:
        raise UserError('项目子文件夹超过 20 层，未登记。', 409)
    with store.lock, store.connection() as db:
        store._project(db, project['id'])
        parent = None
        current = base
        count = db.execute('SELECT count(*) FROM folders WHERE project_id=?', (project['id'],)).fetchone()[0]
        for part in parts:
            current = current / part
            relative = current.relative_to(Path(project['root'])).as_posix()
            row = db.execute('SELECT * FROM folders WHERE project_id=? AND relative_path=? COLLATE NOCASE',
                             (project['id'], relative)).fetchone()
            if row:
                if row['removed']:
                    return  # Scanning must never revive recycled folders or their descendants.
                parent = row['id']
                continue
            if count >= 5000:
                raise UserError('项目文件夹超过 5000 个，未登记剩余目录。', 409)
            folder_id = uid()
            db.execute('INSERT INTO folders(id,project_id,category,parent_id,name,relative_path,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                       (folder_id, project['id'], category, parent, part, relative, now(), now()))
            parent = folder_id
            count += 1


def identity(path):
    info = path.stat()
    birth = getattr(info, 'st_birthtime_ns', info.st_ctime_ns if os.name == 'nt' else None)
    if birth is None or not info.st_ino or info.st_nlink != 1 or not path.is_file():
        return None
    return str(info.st_dev), str(info.st_ino), str(birth)


def follow_move(store, db, project, path):
    """Follow a previously observed file ID only when its old path is gone."""
    from .store import clean_path
    from .organize import Organize
    if db.execute('SELECT 1 FROM items WHERE project_id=? AND path=?', (project['id'], str(path))).fetchone():
        return
    path = clean_path(path)
    key = identity(path)
    if not key or not path.is_relative_to(Path(project['root'])):
        return
    rows = db.execute('''SELECT i.* FROM disk_file_identities d JOIN items i ON i.id=d.item_id
        WHERE i.project_id=? AND i.removed=0 AND i.path=d.path AND d.device=? AND d.file_id=? AND d.birth=?''',
        (project['id'], *key)).fetchall()
    if len(rows) != 1:
        return
    old = Path(rows[0]['path'])
    if not old.is_relative_to(Path(project['root'])) or os.path.lexists(old):
        return
    # An absent path through a changed link is not evidence of a move.
    for parent in old.parents:
        if os.path.lexists(parent):
            clean_path(parent)
            break
    users = db.execute('SELECT project_id,removed FROM items WHERE path=?', (str(old),)).fetchall()
    if any(row['removed'] or db.execute('SELECT 1 FROM items WHERE project_id=? AND path=?',
                                        (row['project_id'], str(path))).fetchone() for row in users):
        return
    if identity(path) != key:
        return
    Organize(store)._repoint_file(db, old, path)
    db.execute('UPDATE items SET name=? WHERE id=? AND name=?', (path.stem, rows[0]['id'], old.stem))
    store._search_row(db, rows[0]['id'])


def remember_file(db, row):
    from .store import clean_path, UserError
    try:
        path = clean_path(row['path'])
        key = identity(path)
        if key:
            db.execute('''INSERT INTO disk_file_identities VALUES(?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
                path=excluded.path,device=excluded.device,file_id=excluded.file_id,birth=excluded.birth
                WHERE path<>excluded.path OR device<>excluded.device OR file_id<>excluded.file_id OR birth<>excluded.birth''',
                (row['id'], str(path), *key))
    except (OSError, UserError):
        pass
