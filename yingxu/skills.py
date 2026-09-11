"""A bounded local SKILL.md catalogue. Content is data and is never executed."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from functools import wraps

from .store import UserError, clean_path, has_link, now, safe_name, uid
from .skill_sources import LABELS, catalogue, scan_roots, custom_location, MAX_CUSTOM_SOURCES, DiscoveryBudget

MAX_SKILL_BYTES = 1024 * 1024
MAX_SKILLS = 2000
MAX_DEPTH = 3
MAX_SCAN_ENTRIES = 20_000
MAX_SCAN_SECONDS = 3.0
SKIP_DIRS = {"cache", "caches", "__pycache__", "node_modules", "venv", "env"}
SOURCE_LABELS = {"yingxu": "映序本地", "codex": "Codex 技能", "claude": "Claude 技能"}


def _synchronized(method):
    @wraps(method)
    def locked(self,*args,**kwargs):
        with self.lock:
            return method(self,*args,**kwargs)
    return locked


def _key(path):
    return os.path.normcase(os.path.abspath(path)).casefold()


def _check_no_links(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            if has_link(part):
                raise UserError("技能路径包含联接或符号链接，请选择实际文件夹。")
    return path


def _scalar(value):
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        try:
            return str(json.loads(value))
        except (ValueError, TypeError):
            return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value.split(" #", 1)[0].strip()


def _metadata(content, fallback="未命名技能"):
    """Read only a small subset of YAML scalars; no YAML objects or directives."""
    lines = content.lstrip("\ufeff").splitlines()
    result = {}
    if lines and lines[0].strip() == "---":
        stop = next((i for i in range(1, min(len(lines), 300)) if lines[i].strip() in {"---", "..."}), None)
        if stop is not None:
            i = 1
            while i < stop:
                match = re.match(r"^(name|description):[ \t]*(.*)$", lines[i])
                if not match:
                    i += 1
                    continue
                field, value = match.groups()
                subsequent = []
                cursor = i + 1
                while cursor < stop and (not lines[cursor].strip() or lines[cursor][:1].isspace()):
                    subsequent.append(lines[cursor])
                    cursor += 1
                if value.strip() in {"|", "|-", "|+", ">", ">-", ">+"}:
                    nonempty = [line for line in subsequent if line.strip()]
                    indent = min((len(line) - len(line.lstrip()) for line in nonempty), default=0)
                    text = [line[indent:] for line in subsequent]
                    parsed = " ".join(line.strip() for line in text) if value.strip().startswith(">") else "\n".join(text)
                    result[field] = parsed.strip()
                else:
                    parsed = _scalar(value)
                    if subsequent and not value.startswith(('"', "'")):
                        parsed += " " + " ".join(line.strip() for line in subsequent)
                    result[field] = parsed.strip()
                i = cursor
    return {
        "name": (result.get("name") or fallback).strip()[:160],
        "description": result.get("description", "").strip()[:4000],
    }


def _read(path):
    _check_no_links(path)
    if not path.is_file() or path.name.lower() != "skill.md":
        raise UserError("技能文件已经不存在。", 404)
    if path.stat().st_size > MAX_SKILL_BYTES:
        raise UserError("SKILL.md 超过 1 MiB，已跳过以保持工作台流畅。")
    with path.open("rb") as handle:
        raw = handle.read(MAX_SKILL_BYTES + 1)
    if len(raw) > MAX_SKILL_BYTES:
        raise UserError("SKILL.md 超过 1 MiB。")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise UserError("技能文件需使用 UTF-8 编码。") from exc
    if "\x00" in content:
        raise UserError("技能文件包含无效文本字符。")
    return raw, content, hashlib.sha256(raw).hexdigest()


class SkillLibrary:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.root = _check_no_links(Path(store.data_root) / "skills")
        self.root.mkdir(parents=True, exist_ok=True)
        self.versions_root = Path(store.data_root) / "skill_versions"
        self.home = Path.home()
        self.locations = {}
        self.sources = {}
        with store.lock, store.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS yx_skills(
              id TEXT PRIMARY KEY,path TEXT NOT NULL,path_key TEXT UNIQUE NOT NULL,
              name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',source TEXT NOT NULL,
              editable INTEGER NOT NULL DEFAULT 0,mtime INTEGER NOT NULL DEFAULT 0,
              size INTEGER NOT NULL DEFAULT 0,etag TEXT NOT NULL DEFAULT '',
              available INTEGER NOT NULL DEFAULT 1,created TEXT NOT NULL,updated TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS yx_skills_available ON yx_skills(available,source,name);
            CREATE TABLE IF NOT EXISTS yx_project_skills(
              project_id TEXT NOT NULL REFERENCES projects(id),
              skill_id TEXT NOT NULL REFERENCES yx_skills(id),created TEXT NOT NULL,
              PRIMARY KEY(project_id,skill_id));
            CREATE TABLE IF NOT EXISTS yx_skill_source_settings(
              id TEXT PRIMARY KEY,label TEXT NOT NULL DEFAULT '',path TEXT NOT NULL DEFAULT '',
              enabled INTEGER NOT NULL DEFAULT 1,custom INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS yx_skill_locations(
              skill_id TEXT NOT NULL REFERENCES yx_skills(id),source_id TEXT NOT NULL,
              PRIMARY KEY(skill_id,source_id));
            """)
            columns = {row[1] for row in db.execute('PRAGMA table_info(yx_skills)')}
            if 'file_identity' not in columns:
                db.execute("ALTER TABLE yx_skills ADD COLUMN file_identity TEXT NOT NULL DEFAULT ''")
            if 'removed' not in columns:
                db.execute('ALTER TABLE yx_skills ADD COLUMN removed INTEGER NOT NULL DEFAULT 0')
            if 'removed_at' not in columns:
                db.execute("ALTER TABLE yx_skills ADD COLUMN removed_at TEXT NOT NULL DEFAULT ''")
            if 'purged' not in columns:
                db.execute('ALTER TABLE yx_skills ADD COLUMN purged INTEGER NOT NULL DEFAULT 0')
            if 'recycle_started' not in columns:
                db.execute('ALTER TABLE yx_skills ADD COLUMN recycle_started INTEGER NOT NULL DEFAULT 0')
        self.refresh()

    def _item(self, row, bound=False):
        result = dict(row)
        result.pop("path_key", None)
        result.pop('file_identity',None)
        result["editable"] = bool(result["editable"])
        result["available"] = bool(result["available"])
        result["bound"] = bool(bound)
        location = self.locations.get(result['source'], {})
        result['source_id'] = result['source']
        result['source_group'] = location.get('source', result['source'])
        result["source_label"] = location.get('label') or SOURCE_LABELS.get(result['source'], result['source'])
        return result

    def _row(self, skill_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM yx_skills WHERE id=? AND removed=0", (str(skill_id),)).fetchone()
        if row is None:
            raise UserError("技能不存在，请刷新技能库。", 404)
        return dict(row)

    def _trusted_path(self, row, write=False):
        source = self.sources.get(row["source"])
        if source is None or not self.locations.get(row['source'],{}).get('enabled'):
            raise UserError("技能来源无效。")
        path = _check_no_links(row["path"])
        root = _check_no_links(source)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise UserError("技能路径超出已登记的技能库。") from exc
        if not path.is_file() or path.name.lower() != "skill.md":
            raise UserError("技能文件已经不存在，请刷新技能库。", 404)
        if write and (row["source"] != "yingxu" or not row["editable"] or path.stat().st_nlink > 1):
            raise UserError("外部技能和共享链接文件只读；请在映序中新建自己的技能。", 403)
        return path

    def directory(self, skill_id):
        """Resolve a registered active skill's location without reading its content."""
        with self.lock:
            return clean_path(self._trusted_path(self._row(skill_id)).parent)

    def _record(self, path, source, content, etag, previous=None):
        info = path.stat()
        metadata = _metadata(content, path.parent.name)
        return {
            "id": previous["id"] if previous else uid(),
            "path": str(path), "path_key": _key(path), **metadata,
            "source": source, "editable": int(source == "yingxu" and info.st_nlink == 1),
            "mtime": info.st_mtime_ns, "size": info.st_size, "etag": etag,
            "file_identity": f'{info.st_dev}:{info.st_ino}' if info.st_ino else '',
            "available": 1, "created": previous["created"] if previous else now(), "updated": now(),
        }

    def _upsert(self, db, record):
        # A surviving hard-link alias keeps the original catalogue/binding ID.
        # This path switch is performed only for a verified matching identity.
        db.execute('UPDATE yx_skills SET path=?,path_key=? WHERE id=? AND path_key!=?',
                   (record['path'],record['path_key'],record['id'],record['path_key']))
        db.execute("""
          INSERT INTO yx_skills(id,path,path_key,name,description,source,editable,mtime,size,etag,available,created,updated,file_identity)
          VALUES(:id,:path,:path_key,:name,:description,:source,:editable,:mtime,:size,:etag,:available,:created,:updated,:file_identity)
          ON CONFLICT(path_key) DO UPDATE SET path=excluded.path,name=excluded.name,
            description=excluded.description,source=excluded.source,editable=excluded.editable,
            mtime=excluded.mtime,size=excluded.size,etag=excluded.etag,available=1,updated=excluded.updated,file_identity=excluded.file_identity
        """, record)

    def _catalogue(self):
        with self.store.connection() as db:
            settings = {row['id']: dict(row) for row in db.execute('SELECT * FROM yx_skill_source_settings')}
        self.locations = catalogue(self.home, self.root, settings)
        self.sources = {key: Path(value['path']) for key,value in self.locations.items()}

    @_synchronized
    def source_list(self):
        with self.lock, self.store.connection() as db:
            rows = db.execute('SELECT id,source FROM yx_skills WHERE available=1 AND removed=0').fetchall()
            links = db.execute('SELECT skill_id,source_id FROM yx_skill_locations').fetchall()
        visible = {row['id'] for row in rows}
        counts = {key:set() for key in self.locations}
        for row in links:
            if row['skill_id'] in visible and row['source_id'] in counts and self.locations[row['source_id']]['enabled']:
                counts[row['source_id']].add(row['skill_id'])
        # Newly created local skills are visible before the next refresh.
        for row in rows:
            if row['source'] in counts and self.locations[row['source']]['enabled']:
                counts[row['source']].add(row['id'])
        groups = {key:set() for key in LABELS}
        sources = []
        for key,value in self.locations.items():
            item = {name:field for name,field in value.items() if name!='mode'}
            item['count'] = len(counts[key])
            groups[item['source']].update(counts[key])
            sources.append(item)
        return {'sources':sources,'groups':[{'id':key,'label':label,'count':len(groups[key])} for key,label in LABELS.items()],
                'all_total':len(visible),'errors':[],'truncated':any(s['status']=='truncated' for s in sources)}

    def add_source(self, data):
        with self.lock:
            value = custom_location(data,self.home,_check_no_links)
            if any(_key(value['path'])==_key(source['path']) for source in self.locations.values()):
                raise UserError('这个扫描位置已经登记。',409)
            with self.store.lock,self.store.connection() as db:
                if db.execute('SELECT count(*) FROM yx_skill_source_settings WHERE custom=1').fetchone()[0]>=MAX_CUSTOM_SOURCES:
                    raise UserError('最多登记 16 个自定义扫描位置。')
                db.execute('INSERT INTO yx_skill_source_settings(id,path,label,enabled,custom) VALUES(:id,:path,:label,:enabled,:custom)',value)
            return self.refresh()

    def update_source(self, source_id, data, remove=False):
        with self.lock:
            source = self.locations.get(source_id)
            if source is None:
                raise UserError('扫描位置不存在。',404)
            if source_id=='yingxu' or (remove and not source['custom']):
                raise UserError('映序本地保持启用；其他内置位置可关闭但不能移除。',403)
            if not isinstance(data,dict) or set(data)-{'enabled','label'} or (not remove and not data):
                raise UserError('扫描位置参数无效。')
            enabled = data.get('enabled',source['enabled'])
            label = data.get('label',source['label'])
            if not isinstance(enabled,bool) or not isinstance(label,str) or not label.strip() or len(label.strip())>80 or any(ord(c)<32 for c in label):
                raise UserError('请输入有效的位置名称与启用状态。')
            with self.store.lock,self.store.connection() as db:
                if remove:
                    db.execute('DELETE FROM yx_skill_source_settings WHERE id=?',(source_id,))
                else:
                    db.execute('INSERT INTO yx_skill_source_settings(id,label,path,enabled,custom) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,enabled=excluded.enabled',
                               (source_id,label.strip(),source['path'],int(enabled),int(source['custom'])))
            return self.refresh()

    def refresh(self):
        """Bounded per-location updates; interrupted locations retain their index."""
        with self.lock:
            self._catalogue()
            with self.store.connection() as db:
                previous = {row['path_key']:dict(row) for row in db.execute('SELECT * FROM yx_skills')}
            previous_identities = {row['file_identity']:row for row in previous.values() if row['file_identity']}
            records, identities, memberships = {}, {}, set()
            errors, completed = [], set()
            skipped = visits = 0
            deadline = time.monotonic()+MAX_SCAN_SECONDS
            truncated = False
            old_active = sum(bool(row['available']) and not row['removed'] for row in previous.values())
            newly_added = 0
            for source, location in self.locations.items():
                if not location['enabled']:
                    location['status']='disabled';completed.add(source);continue
                if time.monotonic()>deadline or visits>=MAX_SCAN_ENTRIES:
                    location['status']='truncated';truncated=True;continue
                try:
                    roots,status = scan_roots(location,_check_no_links,deadline=deadline)
                    location['status']=status
                except DiscoveryBudget as exc:
                    location['status']='truncated';truncated=True
                    if len(errors)<20:errors.append(f"{location['label']}：{exc}")
                    continue
                except (OSError,UserError) as exc:
                    location['status']='error'
                    if len(errors)<20: errors.append(f"{location['label']}：{exc}")
                    continue
                stack = [(root,0) for root in roots]
                complete = True
                while stack:
                    if visits>=MAX_SCAN_ENTRIES or time.monotonic()>deadline or len(records)>=MAX_SKILLS:
                        complete=False;break
                    folder,depth = stack.pop()
                    try:
                        _check_no_links(folder)
                        with os.scandir(folder) as entries:
                            for entry in entries:
                                visits+=1
                                if visits>MAX_SCAN_ENTRIES or time.monotonic()>deadline or len(records)>=MAX_SKILLS:
                                    complete=False;break
                                path=Path(entry.path)
                                if has_link(path): skipped+=1;continue
                                if entry.is_dir(follow_symlinks=False):
                                    if depth<MAX_DEPTH and not entry.name.startswith('.') and entry.name.casefold() not in SKIP_DIRS:
                                        stack.append((path,depth+1))
                                    continue
                                if entry.name.lower()!='skill.md' or not entry.is_file(follow_symlinks=False):continue
                                try:
                                    info=path.lstat();key=_key(path)
                                    identity=(info.st_dev,info.st_ino) if info.st_ino else (key,)
                                    existing=records.get(key) or identities.get(identity)
                                    if existing:
                                        memberships.add((existing['id'],source));skipped+=1;continue
                                    if info.st_size>MAX_SKILL_BYTES:skipped+=1;continue
                                    identity_key=f'{info.st_dev}:{info.st_ino}' if info.st_ino else ''
                                    old=previous.get(key)
                                    if old is None and identity_key in previous_identities:
                                        candidate=previous_identities[identity_key]
                                        try:
                                            prior=_check_no_links(candidate['path']).lstat()
                                            if (prior.st_dev,prior.st_ino)==(info.st_dev,info.st_ino):old=candidate
                                        except (OSError,UserError):pass
                                    if old is None and old_active+newly_added>=MAX_SKILLS:
                                        complete=False;continue
                                    if old and old['mtime']==info.st_mtime_ns and old['size']==info.st_size:
                                        record=dict(old)
                                        record.update(available=1,source=source,editable=int(source=='yingxu' and info.st_nlink==1),
                                                      path=str(path),path_key=key,file_identity=identity_key)
                                    else:
                                        _raw,content,etag=_read(path)
                                        record=self._record(path,source,content,etag,old)
                                    if old is None:newly_added+=1
                                    records[key]=record;identities[identity]=record
                                    memberships.add((record['id'],source))
                                except (OSError,UserError) as exc:
                                    skipped+=1;complete=False
                                    if len(errors)<20:errors.append(f"{location['label']}：{path.name}：{exc}")
                    except (OSError,UserError) as exc:
                        complete=False
                        if len(errors)<20:errors.append(f"{location['label']}：{exc}")
                if complete:completed.add(source)
                else:location['status']='truncated';truncated=True
            enabled={key for key,value in self.locations.items() if value['enabled']}
            with self.store.lock,self.store.connection() as db:
                # Seed legacy IDs and local creations without changing bindings.
                db.execute('INSERT OR IGNORE INTO yx_skill_locations SELECT id,source FROM yx_skills WHERE id NOT IN (SELECT skill_id FROM yx_skill_locations)')
                for source in completed:
                    db.execute('DELETE FROM yx_skill_locations WHERE source_id=?',(source,))
                for record in records.values():self._upsert(db,record)
                db.executemany('INSERT OR IGNORE INTO yx_skill_locations(skill_id,source_id) VALUES(?,?)',memberships)
                links=db.execute('SELECT skill_id,source_id FROM yx_skill_locations').fetchall()
                available={row['skill_id'] for row in links if row['source_id'] in enabled}
                db.execute('UPDATE yx_skills SET available=0')
                db.executemany('UPDATE yx_skills SET available=1 WHERE id=?',((key,) for key in available))
                # A file present through another enabled location remains usable.
                alternatives={}
                for row in links:
                    if row['source_id'] in enabled:alternatives.setdefault(row['skill_id'],row['source_id'])
                for row in db.execute('SELECT id,source FROM yx_skills WHERE available=1').fetchall():
                    if row['source'] not in enabled:
                        db.execute('UPDATE yx_skills SET source=?,editable=0 WHERE id=?',(alternatives[row['id']],row['id']))
            result=self.list()
            result.update(scanned=len(records),skipped=skipped,errors=errors,truncated=truncated)
            return result

    @_synchronized
    def list(self, q='', project_id='', source='', source_id=''):
        if project_id:self.store.get_project(project_id)
        if source and source not in LABELS:raise UserError('未知技能来源。')
        if source_id and source_id not in self.locations:raise UserError('扫描位置不存在。',404)
        query=str(q or '').strip().casefold()[:300]
        with self.lock,self.store.connection() as db:
            bound={row[0] for row in db.execute('SELECT skill_id FROM yx_project_skills WHERE project_id=?',(project_id,))} if project_id else set()
            rows=db.execute("SELECT * FROM yx_skills WHERE available=1 AND removed=0 ORDER BY source!='yingxu',name,id").fetchall()
            links=db.execute('SELECT skill_id,source_id FROM yx_skill_locations').fetchall()
        locations={}
        for link in links:
            if self.locations.get(link['source_id'],{}).get('enabled'):
                locations.setdefault(link['skill_id'],set()).add(link['source_id'])
        skills=[]
        for row in rows:
            ids=locations.get(row['id'],set())
            if self.locations.get(row['source'],{}).get('enabled'):ids.add(row['source'])
            if source_id and source_id not in ids:continue
            if source and not any(self.locations[key]['source']==source for key in ids):continue
            if query and not all(part in (row['name']+' '+row['description']+' '+row['path']).casefold() for part in query.split()):continue
            item=self._item(row,row['id'] in bound);item['source_ids']=sorted(ids);skills.append(item)
        return dict(self.source_list(),skills=skills,total=len(skills))

    def get(self, skill_id):
        with self.lock:
            row = self._row(skill_id)
            path = self._trusted_path(row)
            _raw, content, etag = _read(path)
            record = self._record(path, row["source"], content, etag, row)
            with self.store.lock, self.store.connection() as db:
                self._upsert(db, record)
            result = self._item(record)
            result.update(content=content, etag=etag)
            return result

    @staticmethod
    def _content(value):
        if not isinstance(value, str) or "\x00" in value:
            raise UserError("请输入有效的技能文本。")
        try:
            raw = value.encode("utf-8")
        except UnicodeError as exc:
            raise UserError("技能文本包含无效字符。") from exc
        if len(raw) > MAX_SKILL_BYTES:
            raise UserError("技能文本最多 1 MiB。")
        return raw

    def create(self, data):
        if not isinstance(data, dict):
            raise UserError("技能资料格式无效。")
        name = str(data.get("name", "")).strip()
        if not name or len(name) > 160:
            raise UserError("技能名称需为 1 至 160 字。")
        description = str(data.get("description", "")).strip()
        if len(description) > 4000:
            raise UserError("技能简介最多 4000 字。")
        content = data.get("content", f"# {name}\n\n## 使用范围\n\n## 执行步骤\n")
        self._content(content)
        if not content.lstrip("\ufeff").startswith("---\n") and not content.lstrip("\ufeff").startswith("---\r\n"):
            content = f"---\nname: {json.dumps(name, ensure_ascii=False)}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n{content}"
        raw = self._content(content)
        with self.lock:
            _check_no_links(self.root)
            with self.store.connection() as db:
                count = db.execute("SELECT count(*) FROM yx_skills WHERE available=1 AND removed=0").fetchone()[0]
            if count >= MAX_SKILLS:
                raise UserError("技能库已达到 2000 项的本地索引上限。")
            folder = self.root / (safe_name(name) + "_" + uid()[:8])
            folder.mkdir()
            path = folder / "SKILL.md"
            with path.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            record = self._record(path, "yingxu", content, hashlib.sha256(raw).hexdigest())
            with self.store.lock, self.store.connection() as db:
                self._upsert(db, record)
            result = self._item(record)
            result["content"] = content
            return result

    def save(self, skill_id, data):
        if not isinstance(data, dict) or not isinstance(data.get("etag"), str):
            raise UserError("保存技能需要原版本 etag。")
        raw = self._content(data.get("content"))
        with self.lock:
            row = self._row(skill_id)
            path = self._trusted_path(row, write=True)
            previous, _content, etag = _read(path)
            if etag != data["etag"]:
                raise UserError("技能已被其他程序修改。请保留当前草稿，重新打开后合并。", 409)
            if previous.startswith(b"\xef\xbb\xbf"):
                raw = b"\xef\xbb\xbf" + raw
                if len(raw) > MAX_SKILL_BYTES:
                    raise UserError("技能文本最多 1 MiB。")
            if raw == previous:
                return self.get(skill_id)
            version_dir = _check_no_links(self.versions_root / row["id"])
            version_dir.mkdir(parents=True, exist_ok=True)
            backup = version_dir / (now().replace(":", "-") + "_" + uid()[:8] + ".md")
            with backup.open("xb") as handle:
                handle.write(previous)
                handle.flush()
                os.fsync(handle.fileno())
            temporary = path.parent / (".SKILL_" + uid() + ".tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._trusted_path(row, write=True)
                if _read(path)[2] != etag:
                    raise UserError("技能在保存期间发生变化，请保留草稿后重新打开。", 409)
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            content = raw.decode("utf-8-sig")
            record = self._record(path, "yingxu", content, hashlib.sha256(raw).hexdigest(), row)
            with self.store.lock, self.store.connection() as db:
                self._upsert(db, record)
            result = self._item(record)
            result.update(content=content, etag=record["etag"])
            return result

    def bind(self, project_id, skill_id, bound):
        if not isinstance(bound, bool):
            raise UserError("技能绑定状态必须为 true 或 false。")
        self.store.get_project(project_id)
        row = self._row(skill_id)
        if bound:
            self._trusted_path(row)
        with self.store.lock, self.store.connection() as db:
            if bound:
                db.execute("INSERT OR IGNORE INTO yx_project_skills(project_id,skill_id,created) VALUES(?,?,?)", (project_id, skill_id, now()))
            else:
                db.execute("DELETE FROM yx_project_skills WHERE project_id=? AND skill_id=?", (project_id, skill_id))
        return {"ok": True}

    def bound_skills(self, project_id):
        self.store.get_project(project_id)
        with self.store.connection() as db:
            rows = db.execute("SELECT s.* FROM yx_skills s JOIN yx_project_skills b ON b.skill_id=s.id WHERE b.project_id=? AND s.removed=0 ORDER BY s.name,s.id", (project_id,)).fetchall()
        return [self._item(row, True) for row in rows]

    def remove(self, skill_id):
        """Remove only from YingXu. Source files and bindings remain recoverable."""
        with self.lock, self.store.lock, self.store.connection() as db:
            row = db.execute('SELECT * FROM yx_skills WHERE id=? AND removed=0', (skill_id,)).fetchone()
            if row is None:
                raise UserError('技能不存在或已在回收站。', 404)
            db.execute('UPDATE yx_skills SET removed=1,removed_at=? WHERE id=?', (now(), skill_id))
        return {'ok': True, 'kind': 'skill', 'batch_id': skill_id, 'count': 1,
                'source': row['source'], 'files_preserved': True}

    def trash(self):
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM yx_skills WHERE removed=1 AND purged=0 ORDER BY removed_at DESC,id').fetchall()
        return {'entries': [{'id': row['id'], 'batch_id': row['id'], 'target_id': row['id'],
                             'kind': 'skill', 'project_id': None, 'name': row['name'],
                             'created': row['removed_at'], 'count': 1, 'source': row['source'],
                             'editable': bool(row['editable']), 'path': row['path']}
                            for row in rows], 'total': len(rows)}

    def restore(self, skill_id):
        with self.lock, self.store.lock, self.store.connection() as db:
            row = db.execute('SELECT * FROM yx_skills WHERE id=? AND removed=1 AND purged=0', (skill_id,)).fetchone()
            if row is None:
                raise UserError('这个技能不在回收站。', 404)
            if row['recycle_started']:
                raise UserError('技能曾提交 Windows 回收操作，不能直接恢复记录。请先核对原文件和 Windows 回收站；可继续清理剩余记录。',409)
            db.execute("UPDATE yx_skills SET removed=0,removed_at='' WHERE id=?", (skill_id,))
        return {'ok': True, 'kind': 'skill', 'count': 1, 'available': bool(row['available'])}
