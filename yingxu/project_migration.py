"""Preview-bound, copy-first project migration. Original project trees are backups."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from contextlib import closing

from .project_layout import MODERN_CATEGORIES, category_paths, extra_directories
from .store import UserError, clean_path, decode_text, has_link, now, safe_name, uid

MAX_ENTRIES = 20000
MAX_BYTES = 64 * 1024**3
MAX_FILE_BYTES = 16 * 1024**3
MAX_DEPTH = 32
MAX_PROJECTS = 1000
MAX_MIGRATION_ENTRIES = 50000
MAX_PREVIEWS = 1
TOKEN_TTL = 600


def _key(path):
    return str(Path(path)).casefold()


def _digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle,'sha256').hexdigest()


def _tree(root):
    root=clean_path(root)
    records=[];pending=[root];total=0
    while pending:
        current=pending.pop()
        if len(current.relative_to(root).parts)>MAX_DEPTH:
            raise UserError('项目目录超过 32 层，不能迁移。',409)
        clean_path(current)
        before=current.stat()
        directory=current.is_dir()
        if not directory and (not current.is_file() or before.st_nlink!=1):
            raise UserError('项目包含非普通文件或硬链接，不能迁移：'+str(current),409)
        digest=None
        if not directory:
            total+=before.st_size
            if before.st_size>MAX_FILE_BYTES or total>MAX_BYTES:
                raise UserError('迁移支持单文件 16 GiB、单项目 64 GiB，请先分开大型素材。',409)
            digest=_digest(current)
        after=current.stat()
        stamp=(before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
        if stamp!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise UserError('项目文件正在变化，请保存并停止导入后重新预览。',409)
        records.append({'relative':current.relative_to(root).as_posix(),'directory':directory,
                        'stamp':stamp,'size':0 if directory else before.st_size,'sha256':digest})
        if len(records)+len(pending)>MAX_ENTRIES:
            raise UserError('单项目超过 20000 个文件或目录，请分批整理后迁移。',409)
        if directory:
            with os.scandir(current) as entries:
                for entry in entries:
                    if len(records)+len(pending)>=MAX_ENTRIES:
                        raise UserError('单项目超过 20000 个文件或目录，请分批整理后迁移。',409)
                    pending.append(Path(entry.path))
    return sorted(records,key=lambda value:value['relative'])


def _catalogue(db,ids):
    marks=','.join('?' for _ in ids)
    result={}
    queries={
        'projects':('SELECT * FROM projects WHERE id IN ('+marks+') ORDER BY id',ids),
        'items':('SELECT * FROM items WHERE project_id IN ('+marks+') ORDER BY id',ids),
        'sources':('SELECT * FROM sources WHERE project_id IN ('+marks+') ORDER BY id',ids),
        'folders':('SELECT * FROM folders WHERE project_id IN ('+marks+') ORDER BY id',ids),
        'versions':('SELECT v.* FROM versions v JOIN items i ON i.id=v.item_id WHERE i.project_id IN ('+marks+') ORDER BY v.id',ids),
        'library_folders':('SELECT * FROM project_library_folders ORDER BY id',[]),
        'library_entries':('SELECT * FROM project_library_entries ORDER BY project_id',[]),
        'managed_paths':('SELECT * FROM managed_project_library_paths ORDER BY folder_id,parent_path',[]),
        'active_project_ids':('SELECT id FROM projects WHERE removed=0 ORDER BY id',[]),
        'trash_batches':('SELECT * FROM trash_batches WHERE project_id IN ('+marks+') ORDER BY id',ids),
        'trash_members':('SELECT m.* FROM trash_members m JOIN trash_batches b ON b.id=m.batch_id WHERE b.project_id IN ('+marks+') ORDER BY m.batch_id,m.entity_type,m.entity_id',ids),
    }
    size=0
    for key,(query,args) in queries.items():
        rows=[]
        for row in db.execute(query,args):
            value=dict(row);size+=len(json.dumps(value,ensure_ascii=False))
            if len(rows)>=100000 or size>64*1024*1024:
                raise UserError('迁移索引过大，请减少本次选择的项目。',409)
            rows.append(value)
        result[key]=rows
    return result


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()


def _mapped_relative(project,relative):
    relative=Path(relative)
    if relative.is_absolute() or relative.drive or '..' in relative.parts:
        raise UserError('项目文件夹登记包含越界路径，不能迁移。',409)
    for category,old in sorted(category_paths(project).items(),key=lambda item:len(item[1]),reverse=True):
        prefix=Path(old)
        if relative==prefix or relative.is_relative_to(prefix):
            return Path(MODERN_CATEGORIES[category])/relative.relative_to(prefix)
    old_extra=Path(extra_directories(project)[0])
    if relative==old_extra or relative.is_relative_to(old_extra):
        return Path('工作流')/relative.relative_to(old_extra)
    return relative


def _journal(path,data):
    temporary=path.with_name(path.name+'.'+uid()+'.tmp')
    try:
        with temporary.open('x',encoding='utf-8') as output:
            json.dump(data,output,ensure_ascii=False,indent=2)
            output.flush();os.fsync(output.fileno())
        if path.exists() and (has_link(path) or path.stat().st_nlink!=1):
            raise UserError('迁移恢复记录路径不安全。',409)
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def recover_pending(store,settings):
    """Resolve interrupted DB/settings commits; never remove project copies."""
    recovery=store.data_root/'project-migrations'
    if not recovery.exists():return []
    clean_path(recovery)
    recovered=[]
    with store.lock:
        for index,path in enumerate(sorted(recovery.glob('*.json'))):
            if index>=1000:raise UserError('迁移恢复记录过多，请先检查恢复目录。',409)
            clean_path(path)
            if path.stat().st_nlink!=1 or path.stat().st_size>4*1024*1024:
                raise UserError('迁移恢复记录不是有效的独立文件。',409)
            record=json.loads(path.read_text('utf-8'))
            if record.get('state') not in ('settings_pending','committing','recovery_required'):continue
            projects=record.get('projects',[])
            if not projects or len(projects)>MAX_PROJECTS:raise UserError('迁移恢复记录格式错误。',409)
            with store.connection() as db:
                roots={row['id']:row['root'] for row in db.execute('SELECT id,root FROM projects')}
            old=all(roots.get(item['id'])==item['source_root'] for item in projects)
            new=all(roots.get(item['id'])==item['target_root'] for item in projects)
            if not old and not new:raise UserError('迁移恢复状态不一致，原目录和副本均已保留，请检查恢复记录。',409)
            configured=record['new_root'] if new else record['old_configured_root']
            effective=record['new_root'] if new else record['old_effective_root']
            settings.update({'project_storage_root':configured})
            store.project_root=Path(effective)
            record.update(state='complete' if new else 'rolled_back',recovered=now())
            _journal(path,record);recovered.append(str(path))
    return recovered


class ProjectMigration:
    def __init__(self,store,storage):
        self.store=store;self.storage=storage
        self.tokens={};self.lock=threading.Lock()

    def _allocate(self,parent,name,identity,reserved,registered):
        candidate=parent/safe_name(name)
        for number in range(1000):
            exists=os.path.lexists(candidate)
            if parent.is_dir():
                exists=exists or any(child.name.casefold()==candidate.name.casefold() for child in parent.iterdir())
            if not exists and _key(candidate) not in reserved and _key(candidate) not in registered:
                reserved.add(_key(candidate));return candidate
            candidate=parent/(safe_name(name)[:80]+'_'+identity[:6]+('' if not number else '_'+str(number+1)))
        raise UserError('目标位置同名目录过多，请选择其他位置。',409)

    def preview(self,body):
        if not isinstance(body,dict) or set(body)!={'root','project_ids'}:
            raise UserError('迁移预览参数无效。')
        ids=body['project_ids']
        if not isinstance(ids,list) or not 1<=len(ids)<=MAX_PROJECTS or any(not isinstance(value,str) for value in ids) or len(set(ids))!=len(ids):
            raise UserError('迁移支持 1 至 1000 个活动项目；更多项目需要单独整理迁移。')
        with self.store.lock:
            root=self.storage._validate(body['root'])
            # Only the latest preview is actionable. Drop its full catalogue/tree
            # before constructing another, rather than retaining large snapshots.
            with self.lock:self.tokens.clear()
            with self.store.connection() as db:
                catalogue=_catalogue(db,ids)
                registered={_key(row[0]) for row in db.execute('SELECT root FROM projects')}
                registered.update(_key(row[0]) for row in db.execute('SELECT path FROM managed_project_library_paths'))
            projects={row['id']:row for row in catalogue['projects']}
            if set(ids)!={row['id'] for row in catalogue['active_project_ids']}:
                raise UserError('更改存放位置需要同时迁移全部活动项目，请刷新后重新预览。',409)
            if any(pid not in projects or projects[pid]['removed'] for pid in ids):
                raise UserError('选择包含不存在或已移入回收站的项目，请刷新后重试。',409)
            sources=[clean_path(projects[pid]['root']) for pid in ids]
            if len({_key(source) for source in sources})!=len(sources):
                raise UserError('多个项目指向同一实际目录，请先整理重复项目。',409)
            for source in sources:
                if source==self.store.data_root or source.is_relative_to(self.store.data_root) or self.store.data_root.is_relative_to(source):
                    raise UserError('项目目录与应用数据目录重叠，不能迁移。',409)
                if root==source or root.is_relative_to(source):
                    raise UserError('迁移目标不能位于来源项目内。',409)
                if any(other!=source and (other.is_relative_to(source) or source.is_relative_to(other)) for other in sources):
                    raise UserError('选中的项目目录互相包含，请分别整理后迁移。',409)
            folders={row['id']:row for row in catalogue['library_folders']}
            entries={row['project_id']:row for row in catalogue['library_entries']}
            mapped_parents={};new_parents=[];reserved=set();plans=[];total_bytes=0;total_entries=0
            known={(row['folder_id'],row['parent_path']):Path(row['path']) for row in catalogue['managed_paths']}
            for pid in ids:
                project=projects[pid];parent=root;ancestry=[];cursor=entries.get(pid,{}).get('folder_id');seen=set()
                while cursor:
                    if cursor in seen or cursor not in folders or len(seen)>=100:raise UserError('项目分类层级无效。',409)
                    seen.add(cursor);ancestry.append(folders[cursor]);cursor=folders[cursor]['parent_id']
                for folder in reversed(ancestry):
                    key=(folder['id'],str(parent))
                    if key not in mapped_parents:
                        target=known.get(key)
                        if target is not None:
                            if target.parent!=parent or not target.is_relative_to(root):raise UserError('分类目录登记无效。',409)
                            if target.exists():clean_path(target)
                            elif os.path.lexists(target):raise UserError('分类目录是无效链接。',409)
                        else:
                            target=self._allocate(parent,folder['name'],folder['id'],reserved,registered)
                        mapped_parents[key]=target
                        new_parents.append({'folder_id':folder['id'],'parent_path':str(parent),'path':str(target),'registered':key in known,'existed':target.exists()})
                    parent=mapped_parents[key]
                target=self._allocate(parent,project['name'],pid,reserved,registered)
                source=clean_path(project['root']);tree=_tree(source)
                canonical_source=next((row['id'] for row in catalogue['sources']
                    if row['project_id']==pid and not row['is_file'] and Path(row['path'])==source),None)
                if canonical_source is None:
                    raise UserError('项目内部来源登记缺失，请先修复项目索引再迁移。',409)
                total_entries+=len(tree)
                if total_entries>MAX_MIGRATION_ENTRIES:
                    raise UserError('项目库超过 50000 个文件或目录；请先将超大素材整理为外部引用，再更改位置。',409)
                mapping={record['relative']:_mapped_relative(project,record['relative']).as_posix() for record in tree if record['relative']!='.'}
                owned_items=[];missing_items=[];external=0
                purged={row['id'] for row in catalogue['trash_batches'] if row['project_id']==pid and row['purged']}
                folder_rows={row['id']:row for row in catalogue['folders'] if row['project_id']==pid}
                for folder in folder_rows.values():
                    relative=_mapped_relative(project,folder['relative_path'])
                    if folder['category'] not in MODERN_CATEGORIES or not relative.is_relative_to(Path(MODERN_CATEGORIES[folder['category']])):
                        raise UserError('项目文件夹登记与分类不一致，不能迁移。',409)
                file_records={record['relative']:record for record in tree if not record['directory']}
                for item in catalogue['items']:
                    if item['project_id']!=pid:continue
                    path=Path(item['path'])
                    if not path.is_relative_to(source):external+=1;continue
                    relative=path.relative_to(source).as_posix()
                    if relative not in file_records:
                        if item['removed'] and item['removed_batch'] in purged:missing_items.append(item['id'])
                        else:raise UserError('项目中的已登记文件缺失，请恢复原文件后迁移：'+str(path),409)
                    if item['category'] not in MODERN_CATEGORIES:raise UserError('项目资源分类无效。',409)
                    expected=Path(MODERN_CATEGORIES[item['category']])
                    if item['folder_id']:
                        folder=folder_rows.get(item['folder_id'])
                        if not folder or folder['category']!=item['category']:raise UserError('资源文件夹登记不一致，不能迁移。',409)
                        expected=_mapped_relative(project,folder['relative_path'])
                    mapping[relative]=(expected/path.name).as_posix()
                    owned_items.append(item['id'])
                occupied={}
                for relative,mapped in mapping.items():
                    candidate=Path(mapped)
                    if candidate.is_absolute() or candidate.drive or '..' in candidate.parts:
                        raise UserError('迁移文件路径越界，已停止。',409)
                    key=mapped.casefold()
                    if key in occupied and occupied[key]!=relative:
                        raise UserError('旧目录转换后存在同名文件或文件夹冲突：'+mapped,409)
                    occupied[key]=relative
                size=sum(record['size'] for record in tree);total_bytes+=size
                if total_bytes>MAX_BYTES:raise UserError('项目库超过 64 GiB；请先将大型素材整理为外部引用，再更改位置。',409)
                plans.append({'id':pid,'name':project['name'],'source_root':str(source),'target_root':str(target),
                              'files':len(file_records),'bytes':size,'external_references':external,
                              'tree':tree,'mapping':mapping,'owned_items':owned_items,'missing_items':missing_items,
                              'canonical_source':canonical_source})
            path_map={str(Path(plan['source_root'])/old):str(Path(plan['target_root'])/new)
                      for plan in plans for old,new in plan['mapping'].items()}
            from .migration_links import rewrite_markdown
            warnings=['旧项目目录会完整保留，作为迁移前备份；外部引用仍指向原文件。',
                      '文档、HTML、画板及其他格式中的自定义路径可能需要人工检查；不会修改原文件。',
                      '历史文稿版本保留原内容；恢复旧版本后，其中的旧相对链接可能需要重新选择目标。',
                      '其他项目与 SKILL 对旧目录的引用继续使用保留的原目录，不会自动迁移。']
            transforms={};transformed_bytes=0
            version_files={row['path'] for row in catalogue['versions']}
            for plan in plans:
                source=Path(plan['source_root'])
                for record in plan['tree']:
                    if record['directory']:continue
                    old=source/record['relative']
                    if str(old) in version_files:continue
                    if old.suffix.lower() not in ('.md','.markdown'):continue
                    if record['size']>2*1024*1024:
                        raise UserError('Markdown 文档超过 2 MiB，无法安全检查内部链接：'+str(old),409)
                    rewritten=rewrite_markdown(old.read_bytes(),old,Path(path_map[str(old)]),path_map)
                    warnings.extend(rewritten['warnings'])
                    if rewritten['changed']:
                        transforms[str(old)]=rewritten['content'];transformed_bytes+=len(rewritten['content'])
                    if transformed_bytes>32*1024*1024:
                        raise UserError('需要调整链接的文稿超过 32 MiB，请先整理大型文稿后更改位置。',409)
            token=secrets.token_urlsafe(32)
            plan={'token':token,'created':time.monotonic(),'root':str(root),'ids':ids,'projects':plans,
                  'catalogue':catalogue,'fingerprint':_fingerprint(catalogue),'parents':new_parents,
                  'transforms':transforms,'total_bytes':total_bytes,'warnings':list(dict.fromkeys(warnings))[:50],
                  'old_configured_root':self.storage.settings.get()['project_storage_root'],
                  'old_effective_root':str(self.store.project_root)}
            with self.lock:
                self.tokens={key:value for key,value in self.tokens.items() if time.monotonic()-value['created']<TOKEN_TTL}
                if len(self.tokens)>=MAX_PREVIEWS:self.tokens.pop(next(iter(self.tokens)))
                self.tokens[token]=plan
            return {'token':token,'projects':[{key:value for key,value in project.items() if key not in ('tree','mapping','owned_items','missing_items','canonical_source')} for project in plans],
                    'total_bytes':total_bytes,'warnings':plan['warnings']}

    def _check(self,plan):
        if (self.storage.settings.get()['project_storage_root']!=plan['old_configured_root']
                or str(self.store.project_root)!=plan['old_effective_root']):
            raise UserError('项目默认位置已改变，请重新预览迁移。',409)
        if self.storage._validate(plan['root'])!=Path(plan['root']):raise UserError('迁移目标改变，请重新预览。',409)
        with self.store.connection() as db:
            if _fingerprint(_catalogue(db,plan['ids']))!=plan['fingerprint']:
                raise UserError('项目索引或分类已改变，请重新预览迁移。',409)
        for project in plan['projects']:
            if _tree(project['source_root'])!=project['tree']:
                raise UserError('项目文件已改变，请重新预览迁移。',409)
            if os.path.lexists(project['target_root']):raise UserError('迁移目标已被占用，请重新预览。',409)
        for parent in plan['parents']:
            path=Path(parent['path'])
            if parent['existed']:
                if not clean_path(path).is_dir():raise UserError('目标分类目录不可用。',409)
            elif os.path.lexists(path):raise UserError('迁移目标分类目录已被占用，请重新预览。',409)

    @staticmethod
    def _cleanup_staging(staging):
        # Never recursively delete unknown content. Only our recorded stage members
        # are removed; changed/link paths are left for recovery inspection.
        if staging is None:return
        root,files,directories=staging
        for path,identity in reversed(files):
            try:
                clean_path(path)
                current=path.stat()
                if path.is_relative_to(root) and (current.st_dev,current.st_ino)==identity:path.unlink()
            except (OSError,UserError):pass
        for path in reversed(directories):
            try:
                if path==root or path.is_relative_to(root):
                    clean_path(path);path.rmdir()
            except (OSError,UserError):pass

    def execute(self,body,progress=None):
        if not isinstance(body,dict) or set(body)!={'token'} or not isinstance(body['token'],str):
            raise UserError('迁移确认参数无效。')
        with self.lock:plan=self.tokens.pop(body['token'],None)
        if plan is None or time.monotonic()-plan['created']>=TOKEN_TTL:
            raise UserError('迁移预览已过期或使用过，请重新预览。',409)
        progress=progress or (lambda message:None)
        staging=None;journal_path=None;record=None;settings_changed=False;committed=False
        with self.store.lock:
            self._check(plan)
            recovery=self.store.data_root/'project-migrations'
            recovery.mkdir(exist_ok=True);clean_path(recovery)
            operation=uid();journal_path=recovery/(operation+'.json')
            backup=recovery/(operation+'.sqlite3')
            record={'id':operation,'state':'preparing','created':now(),'database_backup':str(backup),
                    'projects':[{key:project[key] for key in ('id','name','source_root','target_root')} for project in plan['projects']],
                    'warnings':plan['warnings'],'new_root':plan['root'],
                    'old_configured_root':plan['old_configured_root'],'old_effective_root':plan['old_effective_root']}
            _journal(journal_path,record)
            with backup.open('xb'):pass
            with self.store.connection() as source_db, closing(sqlite3.connect(backup)) as backup_db:source_db.backup(backup_db)
            stage_root=Path(plan['root'])/('.yingxu-migration-'+operation)
            stage_root.mkdir();staging=(stage_root,[],[stage_root])
            record.update(state='copying',staging=str(stage_root));_journal(journal_path,record)
            try:
                copied={};copied_files=0;total_files=sum(project['files'] for project in plan['projects'])
                for project in plan['projects']:
                    progress({'message':'正在复制项目：'+project['name'],'completed':copied_files,'total':total_files})
                    source=Path(project['source_root']);stage=stage_root/project['id'];stage.mkdir();staging[2].append(stage)
                    tree_by_path={item['relative']:item for item in project['tree']}
                    legacy=next(row for row in plan['catalogue']['projects'] if row['id']==project['id'])
                    containers={parent.as_posix() for value in category_paths(legacy).values() for parent in Path(value).parents if parent!=Path('.')}
                    directories={Path(value) for key,value in project['mapping'].items()
                                 if tree_by_path.get(key,{}).get('directory') and key not in containers}
                    directories.update(Path(value).parent for value in project['mapping'].values())
                    directories.update(Path(value) for value in MODERN_CATEGORIES.values());directories.add(Path('工作流'))
                    for directory in sorted(directories,key=lambda value:len(value.parts)):
                        current=stage
                        for part in directory.parts:
                            current=current/part
                            if not current.exists():current.mkdir();staging[2].append(current)
                    for item in project['tree']:
                        if item['directory']:continue
                        old=source/item['relative'];new=stage/project['mapping'][item['relative']]
                        transformed=plan['transforms'].get(str(old))
                        digest=hashlib.sha256();written=0
                        with new.open('xb') as output:
                            info=os.fstat(output.fileno());staging[1].append((new,(info.st_dev,info.st_ino)))
                            if transformed is not None:output.write(transformed);digest.update(transformed);written=len(transformed)
                            else:
                                clean_path(old)
                                with old.open('rb') as incoming:
                                    observed=os.fstat(incoming.fileno())
                                    if (observed.st_dev,observed.st_ino,observed.st_size,observed.st_mtime_ns)!=item['stamp'][:4] or observed.st_nlink!=1:
                                        raise UserError('迁移来源文件身份已改变，已停止。',409)
                                    while True:
                                        chunk=incoming.read(1024*1024)
                                        if not chunk:break
                                        written+=len(chunk)
                                        if written>item['size']:raise UserError('迁移中文件改变，已停止。',409)
                                        output.write(chunk);digest.update(chunk)
                                        if written%(32*1024*1024)<len(chunk):
                                            progress({'message':'正在复制 '+old.name+'：'+str(written//(1024*1024))+' MiB',
                                                      'completed':copied_files,'total':total_files})
                            output.flush();os.fsync(output.fileno())
                        expected=hashlib.sha256(transformed).hexdigest() if transformed is not None else item['sha256']
                        if digest.hexdigest()!=expected or _digest(new)!=expected or (transformed is None and written!=item['size']):
                            raise UserError('迁移副本校验失败，原项目保持不变。',409)
                        copied[str(Path(project['target_root'])/project['mapping'][item['relative']])]={'size':written,'sha256':expected,'mtime':new.stat().st_mtime_ns}
                        copied_files+=1
                        progress({'message':'已校验文件 '+str(copied_files)+' / '+str(total_files),
                                  'completed':copied_files,'total':total_files})
                progress('正在复核原文件与项目索引')
                self._check(plan)
                record['state']='verified';_journal(journal_path,record)
                with self.store.connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if _fingerprint(_catalogue(db,plan['ids']))!=plan['fingerprint']:raise UserError('项目索引已改变，请重新预览。',409)
                    for parent in plan['parents']:
                        target=Path(parent['path'])
                        if not parent['existed']:target.mkdir()
                        clean_path(target)
                        if not parent['registered']:
                            db.execute('INSERT INTO managed_project_library_paths(folder_id,parent_path,path) VALUES(?,?,?)',
                                       (parent['folder_id'],parent['parent_path'],parent['path']))
                    for project in plan['projects']:
                        final=Path(project['target_root']);stage=stage_root/project['id']
                        clean_path(final.parent)
                        if os.path.lexists(final):raise UserError('目标目录被占用，已停止迁移。',409)
                        # Windows rename is exclusive; macOS uses renamex_np.
                        if os.name=='nt':os.rename(stage,final)
                        else:
                            from .organize import _rename
                            _rename(stage,final)
                        source=Path(project['source_root'])
                        def mapped(path):
                            value=Path(path)
                            if not value.is_relative_to(source):return str(value)
                            relative=value.relative_to(source).as_posix()
                            return str(final if relative=='.' else final/project['mapping'].get(relative,_mapped_relative(next(p for p in plan['catalogue']['projects'] if p['id']==project['id']),relative)))
                        db.execute('UPDATE projects SET root=?,layout_version=1,updated=? WHERE id=?',(str(final),now(),project['id']))
                        for row in plan['catalogue']['sources']:
                            if row['project_id']==project['id']:db.execute('UPDATE sources SET path=? WHERE id=?',(mapped(row['path']),row['id']))
                        for row in plan['catalogue']['folders']:
                            if row['project_id']==project['id']:
                                relative=Path(mapped(source/row['relative_path'])).relative_to(final).as_posix()
                                db.execute('UPDATE folders SET relative_path=? WHERE id=?',(relative,row['id']))
                        for row in plan['catalogue']['items']:
                            if row['project_id']!=project['id'] or row['id'] not in project['owned_items']:continue
                            target=mapped(row['path'])
                            if row['id'] in project['missing_items']:
                                db.execute('UPDATE items SET path=?,source_id=? WHERE id=?',(target,project['canonical_source'],row['id']));continue
                            info=copied[target]
                            db.execute('UPDATE items SET path=?,source_id=?,size=?,mtime=? WHERE id=?',(target,project['canonical_source'],info['size'],info['mtime'],row['id']))
                            if row['path'] in plan['transforms']:
                                content,_=decode_text(plan['transforms'][row['path']])
                                db.execute('UPDATE items SET search_content=? WHERE id=?',(content,row['id']));self.store._search_row(db,row['id'])
                        for row in plan['catalogue']['versions']:
                            if Path(row['path']).is_relative_to(source):db.execute('UPDATE versions SET path=? WHERE id=?',(mapped(row['path']),row['id']))
                        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_exports'").fetchone():
                            current=time.time()
                            db.execute('''INSERT INTO context_exports(project_id,requested_at,first_requested_at)
                                VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET revision=revision+1,
                                requested_at=excluded.requested_at,first_requested_at=excluded.first_requested_at,
                                failed_revision=0,error='' ''',(project['id'],current,current))
                    record['state']='committing';_journal(journal_path,record)
                    record['state']='settings_pending';_journal(journal_path,record)
                    settings_changed=True;self.storage.settings.update({'project_storage_root':plan['root']})
                    record['state']='committing';_journal(journal_path,record)
                committed=True;self.store.project_root=Path(plan['root'])
                record['state']='complete';record['completed']=now()
                try:_journal(journal_path,record)
                except (OSError,UserError):plan['warnings'].append('迁移已经完成，恢复记录最终写入失败；下次启动会核对数据库完成记录。')
                self._cleanup_staging(staging)
                return {'migrated':len(plan['projects']),'projects':record['projects'],'recovery_manifest':str(journal_path),
                        'database_backup':str(backup),'warnings':plan['warnings'],'originals_retained':True}
            except BaseException as error:
                rollback_error=None
                if settings_changed and not committed:
                    try:self.storage.settings.update({'project_storage_root':plan['old_configured_root']})
                    except (OSError,UserError) as failure:rollback_error=failure
                if record is not None:
                    record.update(state='recovery_required' if rollback_error else 'failed',error=str(error),failed=now())
                    try:_journal(journal_path,record)
                    except (OSError,UserError):pass
                self._cleanup_staging(staging)
                raise
