"""Copy explicitly selected local files into managed project categories."""
from __future__ import annotations

import os
from pathlib import Path

from .organize import Organize
from .store import SAFE_EXTENSIONS, UserError, clean_path, safe_name

MAX_ENTRIES = 50000
MAX_FILES = 10000
MAX_FOLDERS = 2000
MAX_FILE_BYTES = 16 * 1024**3
MAX_TOTAL_BYTES = 64 * 1024**3
COPY_CHUNK = 1024 * 1024


def validate_source(store, value, destination):
    path = clean_path(value)
    data = Path(store.data_root).resolve()
    destination = clean_path(destination)
    if path == data or path.is_relative_to(data) or (path.is_dir() and data.is_relative_to(path)):
        raise UserError('不能复制导入应用数据目录或包含应用数据的上级目录。')
    if path.is_dir() and (destination == path or destination.is_relative_to(path)):
        raise UserError('来源文件夹包含导入目标，无法递归复制到自身内部。')
    if path == Path(path.anchor) or path == Path.home().resolve():
        raise UserError('请选择具体的素材文件夹，不要导入整个磁盘或用户目录。')
    if path.is_file():
        if path.suffix.lower() not in SAFE_EXTENSIONS:
            raise UserError('此文件类型不支持复制导入：'+path.name)
        if path.stat().st_nlink != 1:
            raise UserError('不能复制导入硬链接文件，请选择独立的原文件。')
    elif not path.is_dir():
        raise UserError('请选择普通文件或文件夹。')
    return path


def _folder(organize, pid, category, name, parent):
    base = safe_name(name)
    for index in range(1000):
        candidate = base if not index else base[:88]+' ('+str(index+1)+')'
        try:
            return organize.create_folder(pid,category,candidate,parent)
        except UserError as error:
            # Retry only an actual same-name collision, never permissions/removed parent.
            parent_path = organize.folder_path(pid,category,parent)
            if error.status != 409 or not os.path.lexists(parent_path/candidate):
                raise
    raise UserError('同名文件夹过多，请修改来源文件夹名称。',409)


def _copy_file(source, parent):
    """Exclusive destination plus identity checks; rollback only this copied file."""
    source = clean_path(source)
    parent = clean_path(parent)
    before = source.stat()
    if before.st_nlink != 1 or not source.is_file():
        raise UserError('来源必须是独立的普通文件：'+source.name)
    if before.st_size > MAX_FILE_BYTES:
        raise UserError('单个复制文件不能超过 16 GiB：'+source.name)
    name = safe_name(source.stem)[:88]
    target = None
    identity = None
    try:
        with source.open('rb') as incoming:
            opened = os.fstat(incoming.fileno())
            if (opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns,opened.st_nlink) != (
                    before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,1):
                raise UserError('来源文件已改变，请重新导入。',409)
            for index in range(1000):
                candidate = parent / (name+(' ('+str(index+1)+')' if index else '')+source.suffix)
                clean_path(parent)
                try:
                    output = candidate.open('xb')
                except FileExistsError:
                    continue
                target = candidate
                break
            else:
                raise UserError('同名文件过多，请修改来源文件名称。',409)
            with output:
                identity = os.fstat(output.fileno())
                copied = 0
                while True:
                    chunk = incoming.read(COPY_CHUNK)
                    if not chunk:break
                    copied += len(chunk)
                    if copied > before.st_size or copied > MAX_FILE_BYTES:
                        raise UserError('复制期间来源文件增长，已停止导入。',409)
                    output.write(chunk)
                output.flush();os.fsync(output.fileno())
            after = os.fstat(incoming.fileno())
            if copied != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
                raise UserError('复制期间来源文件改变，已停止导入。',409)
        clean_path(target)
        return target
    except BaseException:
        if target is not None and identity is not None:
            try:
                clean_path(parent)
                current = target.lstat()
                if (current.st_dev,current.st_ino) == (identity.st_dev,identity.st_ino):target.unlink()
            except (OSError,UserError):
                pass
        raise


def import_files(store, source_path, pid, category, folder_id=None, progress=None, *, move_owned=False):
    from .jobs import Jobs
    organize = Organize(store)
    folder_id = None if folder_id in (None,'','root') else folder_id
    destination = organize.folder_path(pid,category,folder_id)
    source = validate_source(store,source_path,destination)
    project_root = clean_path(store.get_project(pid)['root'])
    if move_owned and source.is_file() and source.is_relative_to(project_root):
        owned_source = next((row for row in store.sources(pid) if Path(row['path'])==project_root and not row['is_file']),None)
        if owned_source is None:
            raise UserError('项目内部来源记录缺失，请刷新项目后重试。',409)
        store.index_files(owned_source,[source])
        with store.connection() as db:
            row=db.execute('SELECT id,removed FROM items WHERE project_id=? AND path=?',(pid,str(source))).fetchone()
        if row is None or row['removed']:
            raise UserError('文件或所属文件夹在回收站中，请先恢复后再移动。',409)
        moved=organize.move_items([row['id']],category,folder_id)
        return {'done':moved['stats']['moved']+moved['stats']['referenced'],
                'skipped':moved['stats']['unchanged'],'errors':[]}
    errors = []
    def report(message):
        if len(errors)<20:errors.append(message)
    directories = []
    def remember_directory(path):
        if len(directories)<MAX_FOLDERS:directories.append(path)
        elif len(errors)<20:report('一次复制最多 2000 个文件夹，额外文件夹内容已跳过。')
    files = []
    total = 0
    for path in Jobs.walk(source,store.data_root,on_error=report,max_entries=MAX_ENTRIES,
                          max_depth=18,on_directory=remember_directory):
        if len(files)>=MAX_FILES:
            report('一次复制最多 10000 个文件，剩余文件已跳过。');break
        try:
            validate_source(store,path,destination)
            size=path.stat().st_size
            if size>MAX_FILE_BYTES:raise UserError('单个复制文件不能超过 16 GiB：'+path.name)
            if total+size>MAX_TOTAL_BYTES:
                report('一次复制总大小不能超过 64 GiB，剩余文件已跳过。');break
            total+=size;files.append(path)
        except (OSError,UserError) as error:report(str(error))
    parents = {():folder_id}
    skipped = 0
    if source.is_dir():
        outer = _folder(organize,pid,category,source.name,folder_id)
        parents[()] = outer['id']
        for directory in sorted(directories,key=lambda value:len(value.parts)):
            parts = directory.relative_to(source).parts
            if parts[:-1] not in parents:continue
            try:
                parents[parts] = _folder(organize,pid,category,directory.name,parents[parts[:-1]])['id']
            except (OSError,UserError) as error:report(str(error))
    project_root = clean_path(store.get_project(pid)['root'])
    owned_source = next((row for row in store.sources(pid) if row['path']==str(project_root) and not row['is_file']),None)
    if owned_source is None:raise UserError('项目内部来源记录缺失，请刷新项目后重试。',409)
    done = 0
    for index,path in enumerate(files):
        copied = None
        parts = path.parent.relative_to(source).parts if source.is_dir() else ()
        if parts not in parents:
            skipped+=1;continue
        try:
            with store.lock:
                destination = organize.folder_path(pid,category,parents[parts])
                validate_source(store,path,destination)
            copied = _copy_file(path,destination)
            with store.lock:
                current_destination = organize.folder_path(pid,category,parents[parts])
                if current_destination != destination or clean_path(copied).parent != current_destination:
                    raise UserError('导入期间目标文件夹位置已改变，已停止登记；请刷新后检查已复制文件。',409)
                added,unchanged = store.index_files(owned_source,[copied],True,
                    target_folder_id=parents[parts],target_category=category)
            done+=added;skipped+=unchanged
            if not added:report('文件已复制但未能登记，请刷新项目：'+str(copied))
            if progress:progress('正在复制到项目：'+str(index+1)+' / '+str(len(files)))
        except (OSError,UserError,ValueError) as error:
            skipped+=1
            report(str(error)+(('；文件已复制，尚未登记：'+str(copied)) if copied is not None else ''))
    return {'done':done,'skipped':skipped,'errors':errors}
