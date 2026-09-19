"""Explicit, bounded copy-to-library imports. Source files are never modified."""
import hashlib
import os
from pathlib import Path

from .project_layout import MODERN_CATEGORIES, project_parent, reserve_directory
from .store import CATEGORIES, KINDS, UserError, clean_path, now, safe_name, uid

ALIASES = {name.casefold(): key for key, name in MODERN_CATEGORIES.items()}
ALIASES.update({key: key for key in MODERN_CATEGORIES})
ALIASES.update({label.casefold(): key for key, (label, _) in CATEGORIES.items()})
ALIASES.update({'剧本': 'scripts', '文档': 'scripts', '参考资料': 'references',
                'scripts': 'scripts', 'characters': 'characters', 'scenes': 'scenes',
                'props': 'props', 'previs': 'previs', 'generated': 'generated', 'delivery': 'delivery'})


def validate(store, value, folder_id):
    from .file_import import validate_source
    if not isinstance(value, str) or not value.strip():
        raise UserError('请选择一个本地项目文件夹。')
    storage = store.project_storage.validated_root() if hasattr(store, 'project_storage') else clean_path(store.project_root)
    root = validate_source(store, value, storage)
    if not root.is_dir():
        raise UserError('项目库接收文件夹；单个文件请拖到项目的资源分类。')
    with store.connection() as db:
        if root.is_relative_to(storage) and not db.execute('SELECT 1 FROM projects WHERE root=?', (str(root),)).fetchone():
            raise UserError('请选择项目存放位置以外的来源文件夹，避免重复复制项目库分类。')
        if folder_id is not None and (not isinstance(folder_id, str) or not db.execute('SELECT 1 FROM project_library_folders WHERE id=?', (folder_id,)).fetchone()):
            raise UserError('项目分类不存在，请刷新后重试。', 404)
    return root, storage


def mapped(relative, directory):
    """Recognize explicit category names, with conservative unknown fallback."""
    path = Path(relative)
    for key, (_, legacy) in CATEGORIES.items():
        prefix = Path(legacy)
        if path == prefix or path.is_relative_to(prefix):
            return Path(MODERN_CATEGORIES[key]) / path.relative_to(prefix)
    first = path.parts[0]
    if (directory or len(path.parts) > 1) and first.casefold() in ALIASES:
        return Path(MODERN_CATEGORIES[ALIASES[first.casefold()]]) / Path(*path.parts[1:])
    category = 'scripts' if not directory and len(path.parts) == 1 and KINDS.get(path.suffix.lower()) in ('markdown', 'text', 'docx', 'pdf') else 'unclassified'
    return Path(MODERN_CATEGORIES[category]) / path


def existing(store, root):
    with store.connection() as db:
        row = db.execute('SELECT p.* FROM project_folder_imports f JOIN projects p ON p.id=f.project_id WHERE f.source_path=?',
                         (os.path.normcase(str(root)),)).fetchone()
        if row is None:
            row = db.execute('SELECT * FROM projects WHERE root=?', (str(root),)).fetchone()
        if row:
            if row['removed']:
                raise UserError('对应项目在映序回收站中，请先恢复，避免重复导入。', 409)
            clean_path(row['root'])
            return dict(row)


def copy_project(store, value, folder_id, progress):
    from .project_migration import _tree, _digest, ProjectMigration
    from .migration_links import rewrite_markdown
    root, storage = validate(store, value, folder_id)
    previous = existing(store, root)
    if previous:
        return previous, False
    progress('正在检查文件夹内容…')
    tree = _tree(root)  # 20,000 entries / 64 GiB / 16 GiB per file; rejects links.
    if any(len(Path(entry['relative']).parts) > 20 for entry in tree):
        raise UserError('项目文件夹最多支持 20 层，请整理后重新导入。')
    if sum(entry['directory'] for entry in tree) > 4990:
        raise UserError('项目文件夹最多支持 5000 个文件夹。')
    containers = {p.as_posix() for _, legacy in CATEGORIES.values() for p in Path(legacy).parents if p != Path('.')}
    mapping = {e['relative']: mapped(e['relative'], e['directory']) for e in tree
               if e['relative'] != '.' and not (e['directory'] and e['relative'] in containers)}
    occupied = {}
    for entry in tree:
        rel = entry['relative']
        if rel not in mapping:
            continue
        key = mapping[rel].as_posix().casefold()
        if key in occupied and not (entry['directory'] and occupied[key]['directory']):
            raise UserError('分类整理后出现同名文件冲突，请先重命名：' + rel, 409)
        occupied[key] = entry
    made = []
    staging = None
    committed = False
    pid = None
    try:
        with store.lock, store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            parent = project_parent(db, storage, folder_id, made)
            if parent == root or parent.is_relative_to(root):
                raise UserError('来源文件夹包含目标项目分类，不能复制到自身内部。')
            target = reserve_directory(db, parent, safe_name(root.name), made)
        staging = (target, [], [target])
        directories = {Path(v) for v in MODERN_CATEGORIES.values()} | {Path('工作流')}
        directories.update(mapping[e['relative']] for e in tree if e['directory'] and e['relative'] in mapping)
        directories.update(p.parent for p in mapping.values())
        for directory in sorted(directories, key=lambda p: len(p.parts)):
            current = target
            for part in directory.parts:
                current = current / part
                if not current.exists():
                    clean_path(current.parent)
                    current.mkdir()
                    staging[2].append(current)
                clean_path(current)
        path_map = {str(root / rel): str(target / mapped_path) for rel, mapped_path in mapping.items()}
        warnings = []
        total = sum(not entry['directory'] for entry in tree)
        count = 0
        for entry in tree:
            if entry['directory']:
                continue
            old = clean_path(root / entry['relative'])
            new = target / mapping[entry['relative']]
            transformed = None
            if old.suffix.lower() in ('.md', '.markdown'):
                if entry['size'] > 2 * 1024**2:
                    raise UserError('Markdown 文档超过 2 MiB，无法安全调整分类后的链接。')
                rewritten = rewrite_markdown(old.read_bytes(), old, new, path_map)
                if rewritten['changed']:
                    transformed = rewritten['content']
                warnings.extend(rewritten['warnings'][:max(0, 20-len(warnings))])
            digest = hashlib.sha256()
            copied = 0
            clean_path(new.parent)
            with new.open('xb') as output:
                info = os.fstat(output.fileno())
                staging[1].append((new, (info.st_dev, info.st_ino)))
                with old.open('rb') as incoming:
                    info = os.fstat(incoming.fileno())
                    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != entry['stamp'][:4] or info.st_nlink != 1:
                        raise UserError('来源文件已经改变，请保存后重新拖入。', 409)
                    while chunk := incoming.read(1024*1024):
                        copied += len(chunk)
                        if copied > entry['size']:
                            raise UserError('复制期间文件大小改变，请重新拖入。', 409)
                        digest.update(chunk)
                        if transformed is None:
                            output.write(chunk)
                if transformed is not None:
                    output.write(transformed)
                output.flush()
                os.fsync(output.fileno())
            expected = hashlib.sha256(transformed).hexdigest() if transformed is not None else entry['sha256']
            if copied != entry['size'] or digest.hexdigest() != entry['sha256'] or _digest(new) != expected:
                raise UserError('项目副本校验失败，原文件保留，请重新拖入。', 409)
            count += 1
            progress(f'已复制并校验 {count} / {total} 个文件')
        if _tree(root) != tree:
            raise UserError('复制期间来源目录发生变化，请保存后重新拖入。', 409)
        with store.lock, store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            validate(store, value, folder_id)
            if clean_path(store.project_root) != storage:
                raise UserError('项目存放位置已改变，请重新拖入。', 409)
            pid = uid()
            db.execute('INSERT INTO projects(id,name,root,created,updated,layout_version) VALUES(?,?,?,?,?,1)',
                       (pid, root.name, str(target), now(), now()))
            db.execute('INSERT INTO sources VALUES(?,?,?,?,?)', (uid(), pid, str(target), 'unclassified', 0))
            db.execute('INSERT INTO project_library_entries(project_id,folder_id) VALUES(?,?)', (pid, folder_id))
            db.execute('INSERT INTO project_folder_imports(source_path,project_id) VALUES(?,?)', (os.path.normcase(str(root)), pid))
            folders = {}
            for directory in sorted(staging[2], key=lambda p: len(p.parts)):
                parts = directory.relative_to(target).parts
                if len(parts) < 2:
                    continue
                category = next((key for key, name in MODERN_CATEGORIES.items() if name == parts[0]), None)
                if category is None:
                    continue
                fid = uid()
                db.execute('INSERT INTO folders(id,project_id,category,parent_id,name,relative_path,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                           (fid, pid, category, folders.get(directory.parent), directory.name, directory.relative_to(target).as_posix(), now(), now()))
                folders[directory] = fid
        committed = True
        project = store.get_project(pid)
        project['import_warnings'] = list(dict.fromkeys(warnings))
        return project, True
    finally:
        if not committed and pid is not None:
            # A failed commit acknowledgement is not proof of rollback.
            # Preserve files when the durable catalogue exists or cannot be read.
            try:
                with store.connection() as db:
                    committed = db.execute('SELECT 1 FROM projects WHERE id=?', (pid,)).fetchone() is not None
            except Exception:
                committed = True
        if not committed:
            ProjectMigration._cleanup_staging(staging)
