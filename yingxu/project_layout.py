"""Versioned managed directories; legacy projects retain their original paths."""
from pathlib import Path

MODERN_CATEGORIES = {
    'unclassified': '未分类', 'scripts': '文本', 'shots': '素材', 'music': '音乐',
    'characters': '角色', 'scenes': '场景', 'props': '道具',
    'previs': '预演', 'generated': '生成素材', 'delivery': '成片交付',
    'references': '记录',
}


def category_paths(project):
    from .store import CATEGORIES
    if project.get('layout_version', 0) == 1:
        return dict(MODERN_CATEGORIES)
    return {key: value[1] for key, value in CATEGORIES.items()}


def extra_directories(project):
    return ['工作流'] if project.get('layout_version', 0) == 1 else ['30_Workflows']


def reserve_directory(db, parent, name, made_dirs):
    """Never reuse an unrelated directory, including case-only collisions."""
    from .store import clean_path, uid
    parent = clean_path(parent)
    occupied = {entry.name.casefold() for entry in parent.iterdir()}
    candidate = parent / name
    while True:
        registered = db.execute('SELECT 1 FROM projects WHERE root=? COLLATE NOCASE UNION ALL '
                                'SELECT 1 FROM managed_project_library_paths WHERE path=? COLLATE NOCASE LIMIT 1',
                                (str(candidate), str(candidate))).fetchone()
        if candidate.name.casefold() in occupied or registered:
            candidate = parent / (name + '_' + uid()[:6])
            continue
        try:
            candidate.mkdir()
        except FileExistsError:
            occupied.add(candidate.name.casefold())
            continue
        made_dirs.append(candidate)
        return candidate


def project_parent(db, root, folder_id, made_dirs):
    """Snapshot logical ancestors for new projects, without relocating old ones."""
    from .store import UserError, clean_path, safe_name
    root = clean_path(root)
    ancestors = []
    seen = set()
    while folder_id is not None:
        if folder_id in seen or len(seen) >= 100:
            raise UserError('项目分类层级无效，请先整理项目库。', 409)
        seen.add(folder_id)
        row = db.execute('SELECT * FROM project_library_folders WHERE id=?', (folder_id,)).fetchone()
        if row is None:
            raise UserError('项目分类不存在，请刷新后重试。', 404)
        ancestors.append(dict(row))
        folder_id = row['parent_id']
    parent = root
    for folder in reversed(ancestors):
        existing = db.execute('SELECT path FROM managed_project_library_paths WHERE folder_id=? AND parent_path=?',
                              (folder['id'], str(parent))).fetchone()
        if existing:
            target = Path(existing['path'])
            if target.parent != parent or not target.is_relative_to(root):
                raise UserError('项目分类保存位置无效。', 409)
            if not target.exists() and not target.is_symlink():
                target.mkdir()
                made_dirs.append(target)
            parent = clean_path(target)
            if not parent.is_dir():
                raise UserError('项目分类保存位置不是文件夹。', 409)
        else:
            try:
                segment = safe_name(folder['name'])
            except UserError:
                segment = '分类_' + folder['id'][:6]
            target = reserve_directory(db, parent, segment, made_dirs)
            db.execute('INSERT INTO managed_project_library_paths(folder_id,parent_path,path) VALUES(?,?,?)',
                       (folder['id'], str(parent), str(target)))
            parent = target
    return parent
