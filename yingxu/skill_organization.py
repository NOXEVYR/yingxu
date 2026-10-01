"""Persistent folders and user metadata for indexed skills.

Organization is deliberately independent from source files, collection versions,
and project bindings. Queries read the SQLite index only and never scan disks.
"""

from __future__ import annotations

import json

from .store import UserError, json_text, now, uid


MAX_FOLDERS = 500
MAX_FOLDER_DEPTH = 3
MAX_FOLDER_NAME_LENGTH = 80
MAX_METADATA_BATCH = 2000
MAX_TAGS = 32
MAX_TAG_LENGTH = 40
MAX_TAG_INPUT = 128
MAX_NOTES_LENGTH = 4000

_UNSET = object()
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
}


class SkillOrganization:
    """Manage logical organization owned by a :class:`SkillLibrary`."""

    def __init__(self, library):
        self.library = library
        self.store = library.store
        with self.store.lock, self.store.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS yx_skill_organization_folders(
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              parent_id TEXT REFERENCES yx_skill_organization_folders(id) ON DELETE RESTRICT,
              created TEXT NOT NULL,
              updated TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS yx_skill_organization_folder_sibling
              ON yx_skill_organization_folders(COALESCE(parent_id,''),name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS yx_skill_organization_folder_parent
              ON yx_skill_organization_folders(parent_id,name COLLATE NOCASE,id);
            CREATE TABLE IF NOT EXISTS yx_skill_organization_metadata(
              skill_id TEXT PRIMARY KEY REFERENCES yx_skills(id) ON DELETE RESTRICT,
              folder_id TEXT REFERENCES yx_skill_organization_folders(id) ON DELETE RESTRICT,
              tags TEXT NOT NULL DEFAULT '[]',
              notes TEXT NOT NULL DEFAULT '',
              updated TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS yx_skill_organization_metadata_folder
              ON yx_skill_organization_metadata(folder_id,skill_id);
            CREATE TABLE IF NOT EXISTS yx_skill_organization_migrations(
              name TEXT PRIMARY KEY, completed TEXT NOT NULL);
            """)
        self._migrate_legacy_collection_tags()

    def _migrate_legacy_collection_tags(self):
        """Copy old collection tags once without changing collection categories/tags."""
        with self.store.lock, self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            done = db.execute(
                "SELECT 1 FROM yx_skill_organization_migrations WHERE name=?",
                ("collection-tags-v1",),
            ).fetchone()
            if done:
                return
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='yx_skill_collections'"
            ).fetchone()
            if table:
                rows = db.execute(
                    "SELECT source_skill_id,tags FROM yx_skill_collections ORDER BY id"
                ).fetchall()
                for row in rows:
                    skill_id = str(row["source_skill_id"] or "")
                    if not skill_id or not db.execute(
                            "SELECT 1 FROM yx_skills WHERE id=?", (skill_id,)).fetchone():
                        continue
                    try:
                        legacy = json.loads(row["tags"] or "[]")
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(legacy, list):
                        continue
                    existing = db.execute(
                        "SELECT tags FROM yx_skill_organization_metadata WHERE skill_id=?",
                        (skill_id,),
                    ).fetchone()
                    try:
                        current = json.loads(existing["tags"] or "[]") if existing else []
                    except (TypeError, ValueError):
                        current = []
                    if not isinstance(current, list):
                        current = []
                    merged = []
                    seen = set()
                    for tag in [*current, *legacy]:
                        if not isinstance(tag, str):
                            continue
                        tag = tag.strip()
                        if not tag or len(tag) > MAX_TAG_LENGTH or any(ord(char) < 32 for char in tag):
                            continue
                        key = tag.casefold()
                        if key not in seen:
                            merged.append(tag)
                            seen.add(key)
                    if not merged:
                        continue
                    db.execute("""INSERT INTO yx_skill_organization_metadata
                        (skill_id,folder_id,tags,notes,updated) VALUES(?,NULL,?,'',?)
                        ON CONFLICT(skill_id) DO UPDATE SET tags=excluded.tags,updated=excluded.updated""",
                               (skill_id, json_text(merged), now()))
            db.execute(
                "INSERT INTO yx_skill_organization_migrations(name,completed) VALUES(?,?)",
                ("collection-tags-v1", now()),
            )

    @staticmethod
    def _name(value):
        if not isinstance(value, str):
            raise UserError("文件夹名称必须是文本。")
        name = value.strip()
        if (not name or len(name) > MAX_FOLDER_NAME_LENGTH or name in {".", ".."}
                or name.endswith((".", " "))
                or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
                or name.split(".")[0].upper() in _RESERVED_NAMES):
            raise UserError(f"文件夹名称需为 1–{MAX_FOLDER_NAME_LENGTH} 个有效字符。")
        return name

    @staticmethod
    def _parent_id(value):
        if value is None or value == "":
            return None
        if not isinstance(value, str) or len(value) > 128 or any(ord(char) < 32 for char in value):
            raise UserError("父文件夹无效。")
        return value

    @staticmethod
    def _folders(db):
        return {row["id"]: dict(row) for row in db.execute(
            "SELECT id,name,parent_id FROM yx_skill_organization_folders"
        )}

    @staticmethod
    def _depth(folders, folder_id):
        depth = 0
        seen = set()
        while folder_id:
            if folder_id in seen or folder_id not in folders:
                raise UserError("文件夹层级数据无效。", 409)
            seen.add(folder_id)
            depth += 1
            folder_id = folders[folder_id]["parent_id"]
        return depth

    @classmethod
    def _subtree_height(cls, folders, folder_id):
        children = {}
        for row in folders.values():
            if row["parent_id"]:
                children.setdefault(row["parent_id"], []).append(row["id"])

        def visit(current, seen):
            if current in seen:
                raise UserError("文件夹层级数据无效。", 409)
            descendants = children.get(current, ())
            return 1 + max((visit(child, seen | {current}) for child in descendants), default=0)

        return visit(folder_id, set())

    @staticmethod
    def _metadata_tags(value):
        try:
            tags = json.loads(value or "[]")
        except (TypeError, ValueError) as exc:
            raise UserError("技能标签数据损坏，未写入整理信息。", 409) from exc
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise UserError("技能标签数据损坏，未写入整理信息。", 409)
        return tags

    @staticmethod
    def _metadata_row(row):
        return {
            "skill_id": row["skill_id"],
            "folder_id": row["folder_id"] or "",
            "tags": SkillOrganization._metadata_tags(row["tags"]),
            "notes": row["notes"] or "",
        }

    @staticmethod
    def _visible_ids(db):
        """Return indexed, usable skill identities without touching skill files."""
        rows = db.execute("""
            SELECT id FROM yx_skills WHERE available=1 AND removed=0 AND purged=0
            UNION
            SELECT c.source_skill_id AS id
              FROM yx_skill_collections c
              JOIN yx_skill_collection_versions v
                ON v.collection_id=c.id AND v.version=c.current_version
             WHERE c.current_version!=''
        """).fetchall()
        return {row["id"] for row in rows}

    def list(self):
        with self.library.lock, self.store.connection() as db:
            folders = self._folders(db)
            metadata_rows = [dict(row) for row in db.execute(
                "SELECT skill_id,folder_id,tags,notes FROM yx_skill_organization_metadata ORDER BY skill_id"
            )]
            visible_ids = self._visible_ids(db)
        counts = {}
        for row in metadata_rows:
            if row["folder_id"] and row["skill_id"] in visible_ids:
                counts[row["folder_id"]] = counts.get(row["folder_id"], 0) + 1

        result = []
        for folder_id, row in folders.items():
            depth = self._depth(folders, folder_id)
            if depth > MAX_FOLDER_DEPTH:
                raise UserError("文件夹层级超过限制。", 409)
            ancestors = []
            current = folder_id
            while current:
                ancestors.append(folders[current]["name"])
                current = folders[current]["parent_id"]
            result.append({
                "id": folder_id,
                "name": row["name"],
                "parent_id": row["parent_id"] or "",
                "path": "/".join(reversed(ancestors)),
                "count": counts.get(folder_id, 0),
            })
        result.sort(key=lambda item: (item["path"].casefold(), item["id"]))
        return {
            "folders": result,
            "metadata": [self._metadata_row(row) for row in metadata_rows],
            "total": len(visible_ids),
        }

    def _folder(self, db, folder_id):
        folders = self._folders(db)
        row = folders.get(str(folder_id))
        if row is None:
            raise UserError("技能文件夹不存在。", 404)
        depth = self._depth(folders, row["id"])
        ancestors = []
        current = row["id"]
        while current:
            ancestors.append(folders[current]["name"])
            current = folders[current]["parent_id"]
        visible_ids = self._visible_ids(db)
        count = sum(1 for member in db.execute(
            "SELECT skill_id FROM yx_skill_organization_metadata WHERE folder_id=?",
            (row["id"],),
        ) if member["skill_id"] in visible_ids)
        return {
            "id": row["id"], "name": row["name"], "parent_id": row["parent_id"] or "",
            "path": "/".join(reversed(ancestors)), "count": count,
        }

    def create_folder(self, name, parent_id=""):
        name = self._name(name)
        parent_id = self._parent_id(parent_id)
        with self.library.lock, self.store.lock, self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            folders = self._folders(db)
            if len(folders) >= MAX_FOLDERS:
                raise UserError(f"技能文件夹最多 {MAX_FOLDERS} 个。")
            if parent_id and parent_id not in folders:
                raise UserError("父文件夹不存在。", 404)
            depth = (self._depth(folders, parent_id) if parent_id else 0) + 1
            if depth > MAX_FOLDER_DEPTH:
                raise UserError(f"文件夹最多嵌套 {MAX_FOLDER_DEPTH} 层。")
            if db.execute(
                    "SELECT 1 FROM yx_skill_organization_folders WHERE parent_id IS ? AND name=? COLLATE NOCASE",
                    (parent_id, name)).fetchone():
                raise UserError("同一层级已有同名文件夹。", 409)
            folder_id = "fld_" + uid()
            timestamp = now()
            db.execute("""INSERT INTO yx_skill_organization_folders
                (id,name,parent_id,created,updated) VALUES(?,?,?,?,?)""",
                       (folder_id, name, parent_id, timestamp, timestamp))
            return self._folder(db, folder_id)

    def update_folder(self, folder_id, name=_UNSET, parent_id=_UNSET):
        if name is _UNSET and parent_id is _UNSET:
            raise UserError("请提供要修改的文件夹名称或父文件夹。")
        if not isinstance(folder_id, str) or not folder_id or len(folder_id) > 128:
            raise UserError("技能文件夹标识无效。")
        clean_name = self._name(name) if name is not _UNSET else _UNSET
        clean_parent = self._parent_id(parent_id) if parent_id is not _UNSET else _UNSET
        with self.library.lock, self.store.lock, self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            folders = self._folders(db)
            current = folders.get(str(folder_id))
            if current is None:
                raise UserError("技能文件夹不存在。", 404)
            next_name = current["name"] if clean_name is _UNSET else clean_name
            next_parent = current["parent_id"] if clean_parent is _UNSET else clean_parent
            if next_parent and next_parent not in folders:
                raise UserError("父文件夹不存在。", 404)
            if next_parent:
                self._depth(folders, next_parent)
            if next_parent == current["id"]:
                raise UserError("文件夹不能移动到自身或其子文件夹中。")
            if next_parent:
                ancestor = next_parent
                while ancestor:
                    if ancestor == current["id"]:
                        raise UserError("文件夹不能移动到自身或其子文件夹中。")
                    ancestor = folders[ancestor]["parent_id"]
            if db.execute("""SELECT 1 FROM yx_skill_organization_folders
                WHERE parent_id IS ? AND name=? COLLATE NOCASE AND id!=?""",
                          (next_parent, next_name, str(folder_id))).fetchone():
                raise UserError("同一层级已有同名文件夹。", 409)
            new_depth = self._depth(folders, next_parent) + 1 if next_parent else 1
            if new_depth + self._subtree_height(folders, current["id"]) - 1 > MAX_FOLDER_DEPTH:
                raise UserError(f"文件夹最多嵌套 {MAX_FOLDER_DEPTH} 层。")
            db.execute("UPDATE yx_skill_organization_folders SET name=?,parent_id=?,updated=? WHERE id=?",
                       (next_name, next_parent, now(), str(folder_id)))
            return self._folder(db, str(folder_id))

    def delete_folder(self, folder_id):
        if not isinstance(folder_id, str) or not folder_id or len(folder_id) > 128:
            raise UserError("技能文件夹标识无效。")
        with self.library.lock, self.store.lock, self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            folder = db.execute(
                "SELECT id FROM yx_skill_organization_folders WHERE id=?", (str(folder_id),)
            ).fetchone()
            if folder is None:
                raise UserError("技能文件夹不存在。", 404)
            if db.execute(
                    "SELECT 1 FROM yx_skill_organization_folders WHERE parent_id=? LIMIT 1",
                    (str(folder_id),)).fetchone():
                raise UserError("请先移动或删除此文件夹中的子文件夹。", 409)
            if db.execute(
                    "SELECT 1 FROM yx_skill_organization_metadata WHERE folder_id=? LIMIT 1",
                    (str(folder_id),)).fetchone():
                raise UserError("请先将成员技能移出此文件夹。", 409)
            db.execute("DELETE FROM yx_skill_organization_folders WHERE id=?", (str(folder_id),))
        return {"ok": True}

    @staticmethod
    def _clean_tags(tags):
        if not isinstance(tags, list) or len(tags) > MAX_TAG_INPUT or any(
                not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > MAX_TAG_LENGTH
                or any(ord(char) < 32 for char in tag) for tag in tags):
            raise UserError(f"标签输入最多 {MAX_TAG_INPUT} 项，每个标签需为 1–{MAX_TAG_LENGTH} 个有效字符。")
        result = []
        seen = set()
        for tag in tags:
            tag = tag.strip()
            key = tag.casefold()
            if key not in seen:
                result.append(tag)
                seen.add(key)
                if len(result) > MAX_TAGS:
                    raise UserError(f"标签去重后最多保留 {MAX_TAGS} 个。")
        return result

    def update_metadata(self, skill_ids, folder_id=_UNSET, tags=_UNSET, notes=_UNSET,
                        tags_mode="append"):
        if not isinstance(skill_ids, list) or not skill_ids or len(skill_ids) > MAX_METADATA_BATCH:
            raise UserError(f"请提供 1–{MAX_METADATA_BATCH} 个技能。")
        if any(not isinstance(skill_id, str) or not skill_id or len(skill_id) > 128
               or any(ord(char) < 32 for char in skill_id) for skill_id in skill_ids):
            raise UserError("技能标识无效。")
        if folder_id is _UNSET and tags is _UNSET and notes is _UNSET:
            raise UserError("请提供要更新的文件夹、标签或备注。")
        if not isinstance(tags_mode, str) or tags_mode not in {"append", "replace"}:
            raise UserError("tags_mode 只能是 append 或 replace。")
        new_tags = self._clean_tags(tags) if tags is not _UNSET else _UNSET
        if notes is not _UNSET:
            if not isinstance(notes, str) or len(notes) > MAX_NOTES_LENGTH or "\x00" in notes:
                raise UserError(f"备注最多 {MAX_NOTES_LENGTH} 个字符。")
        if folder_id is not _UNSET:
            folder_id = self._parent_id(folder_id)

        with self.library.lock, self.store.lock, self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            canonical_ids = []
            seen_ids = set()
            for skill_id in skill_ids:
                collection = db.execute(
                    "SELECT source_skill_id FROM yx_skill_collections WHERE id=?", (skill_id,)
                ).fetchone()
                canonical = collection["source_skill_id"] if collection else skill_id
                if canonical not in seen_ids:
                    canonical_ids.append(canonical)
                    seen_ids.add(canonical)
            if notes is not _UNSET and len(canonical_ids) != 1:
                raise UserError("备注只能一次修改一个技能。")
            known = set()
            for offset in range(0, len(canonical_ids), 500):
                chunk = canonical_ids[offset:offset + 500]
                known.update(row["id"] for row in db.execute(
                    "SELECT id FROM yx_skills WHERE id IN (%s)" % ",".join("?" for _ in chunk),
                    chunk,
                ))
            missing = [skill_id for skill_id in canonical_ids if skill_id not in known]
            if missing:
                raise UserError("部分技能已不存在，未保存任何整理信息。", 404)
            if folder_id is not _UNSET and folder_id:
                if not db.execute(
                        "SELECT 1 FROM yx_skill_organization_folders WHERE id=?", (folder_id,)).fetchone():
                    raise UserError("目标文件夹不存在。", 404)

            updated = []
            for skill_id in canonical_ids:
                current = db.execute(
                    "SELECT skill_id,folder_id,tags,notes FROM yx_skill_organization_metadata WHERE skill_id=?",
                    (skill_id,),
                ).fetchone()
                next_folder = (current["folder_id"] if current else None) if folder_id is _UNSET else folder_id
                old_tags = self._metadata_tags(current["tags"] if current else "[]")
                if new_tags is _UNSET:
                    next_tags = old_tags
                elif tags_mode == "replace":
                    next_tags = new_tags
                else:
                    next_tags = self._clean_tags([*old_tags, *new_tags])
                next_notes = (current["notes"] if current else "") if notes is _UNSET else notes
                db.execute("""INSERT INTO yx_skill_organization_metadata
                    (skill_id,folder_id,tags,notes,updated) VALUES(?,?,?,?,?)
                    ON CONFLICT(skill_id) DO UPDATE SET folder_id=excluded.folder_id,
                      tags=excluded.tags,notes=excluded.notes,updated=excluded.updated""",
                           (skill_id, next_folder, json_text(next_tags), next_notes, now()))
                updated.append({"skill_id": skill_id, "folder_id": next_folder or "",
                                "tags": next_tags, "notes": next_notes})
            return {"metadata": updated, "updated": len(updated)}


__all__ = ["SkillOrganization"]
