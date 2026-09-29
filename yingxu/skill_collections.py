"""Explicit, bounded and read-only copies of complete SKILL directories.

Collections contain data only: this module never imports or executes a
collected script and never installs dependencies.  Each version is an
immutable directory that can be registered explicitly by another local tool.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time

from .store import UserError, clean_path, has_link, json_text, now, safe_name, uid

MAX_PACKAGE_BYTES = 50 * 1024 * 1024
MAX_PACKAGE_FILES = 2000
MAX_PACKAGE_ENTRIES = 4000
MAX_PACKAGE_SECONDS = 15.0
PREVIEW_TTL_SECONDS = 10 * 60
MAX_PREVIEWS = 100
MAX_TAGS = 16
MAX_TAG_LENGTH = 40
MAX_CUSTOM_CATEGORIES = 32
SCHEMA = "yingxu.skill-collection.v1"
EXPORT_SCHEMA = "yingxu.skill-collection-export.v1"

BUILTIN_CATEGORIES = (
    ("planning", "策划与文本"),
    ("visual", "视觉与分镜"),
    ("video", "视频与预演"),
    ("audio", "声音与配音"),
    ("development", "开发与工具"),
    ("delivery", "整理与交付"),
)

_SECRET_NAMES = {
    ".env", ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json",
    "secrets", "secrets.json", "id_rsa", "id_ed25519", "id_dsa", "token.json",
}
_SAFE_EXAMPLES = {".env.example", ".env.sample", ".env.template"}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_PARENT_REFERENCE = re.compile(r"(?<![\w.])(?:\.\.[\\/])+(?:[\w.-]+(?:[\\/][\w.-]+)*)?")
_PACKAGE_SCOPE_WARNING = (
    "此收藏只包含技能目录内的文件；目录外的运行时、插件和依赖不会随包复制，"
    "仍需按目标客户端单独核对兼容性。"
)
_PARENT_REFERENCE_WARNING = "SKILL.md 含有指向上级目录的相对路径；它可能依赖包外文件，请人工核对。"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _has_link_if_present(path):
    path = Path(path)
    return (path.exists() or path.is_symlink()) and has_link(path)


class SkillCollections:
    """Collection facade owned by :class:`SkillLibrary` as ``library.collections``."""

    def __init__(self, library):
        self.library = library
        self.store = library.store
        data_root = clean_path(self.store.data_root)
        self.root = data_root / "skill_collections"
        self.exports_root = data_root / "skill_collection_exports"
        self.root.mkdir(parents=True, exist_ok=True)
        self.exports_root.mkdir(parents=True, exist_ok=True)
        if has_link(self.root) or has_link(self.exports_root):
            raise UserError("技能收藏目录不能是符号链接或目录联接。")
        with self.store.lock, self.store.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS yx_skill_collections(
              id TEXT PRIMARY KEY,source_skill_id TEXT NOT NULL UNIQUE,
              origin_key TEXT NOT NULL UNIQUE,origin TEXT NOT NULL,
              name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',
              category TEXT NOT NULL DEFAULT '',tags TEXT NOT NULL DEFAULT '[]',
              current_version TEXT NOT NULL DEFAULT '',created TEXT NOT NULL,updated TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS yx_skill_collections_name ON yx_skill_collections(name,id);
            CREATE TABLE IF NOT EXISTS yx_skill_collection_versions(
              collection_id TEXT NOT NULL REFERENCES yx_skill_collections(id),version TEXT NOT NULL,
              package_sha256 TEXT NOT NULL,path TEXT NOT NULL,manifest TEXT NOT NULL,
              total_bytes INTEGER NOT NULL,file_count INTEGER NOT NULL,created TEXT NOT NULL,
              PRIMARY KEY(collection_id,version));
            CREATE TABLE IF NOT EXISTS yx_skill_collection_previews(
              token TEXT PRIMARY KEY,source_skill_id TEXT NOT NULL,origin_key TEXT NOT NULL,
              origin TEXT NOT NULL,name TEXT NOT NULL,description TEXT NOT NULL,
              version TEXT NOT NULL,manifest TEXT NOT NULL,total_bytes INTEGER NOT NULL,
              file_count INTEGER NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,
              collected_id TEXT NOT NULL DEFAULT '',collected_version TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS yx_skill_categories(
              id TEXT PRIMARY KEY,label TEXT NOT NULL UNIQUE COLLATE NOCASE,
              builtin INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(yx_project_skills)")}
            if "collection_id" not in columns:
                db.execute("ALTER TABLE yx_project_skills ADD COLUMN collection_id TEXT NOT NULL DEFAULT ''")
            if "collection_version" not in columns:
                db.execute("ALTER TABLE yx_project_skills ADD COLUMN collection_version TEXT NOT NULL DEFAULT ''")
            for category_id, label in BUILTIN_CATEGORIES:
                db.execute("INSERT OR IGNORE INTO yx_skill_categories(id,label,builtin,created) VALUES(?,?,1,?)",
                           (category_id, label, now()))

    def _source(self, source_skill_id):
        row = self.library._row(source_skill_id)
        skill_file = self.library._trusted_path(row)
        source_root = self.library.sources.get(row["source"])
        if source_root is None:
            raise UserError("技能来源不可用。", 404)
        root = skill_file.parent
        try:
            relative = root.relative_to(source_root).as_posix()
        except ValueError as exc:
            raise UserError("技能目录超出已登记来源。") from exc
        if not relative or relative in {".", ".."}:
            raise UserError("技能目录边界无效。")
        origin_key = row["source"] + "\0" + relative.casefold()
        origin = {
            "source_id": row["source"],
            "source_label": self.library._item(row)["source_label"],
            "relative_path": relative,
            "path": str(root),
        }
        return row, root, origin_key, origin

    @staticmethod
    def _check_name(relative):
        rel = PurePosixPath(relative)
        if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
            raise UserError("技能包包含越界路径。")
        for part in rel.parts:
            if ("\\" in part or ":" in part or any(ord(char) < 32 for char in part)
                    or part.endswith((" ", ".")) or part.split(".", 1)[0].upper() in _RESERVED):
                raise UserError("技能包包含无法安全复制的文件名。")
        name = rel.name.casefold()
        if name in _SECRET_NAMES or (name.startswith(".env.") and name not in _SAFE_EXAMPLES):
            raise UserError("技能包含疑似凭据文件；请先从技能目录移除后再收藏。")

    def _snapshot(self, source_skill_id):
        row, root, origin_key, origin = self._source(source_skill_id)
        if has_link(root) or not root.is_dir():
            raise UserError("技能目录不存在或包含链接。", 404)
        root = root.resolve(strict=True)
        deadline = time.monotonic() + MAX_PACKAGE_SECONDS
        files = []
        entries = 0
        total = 0
        stack = [(root, PurePosixPath())]
        folded_paths = set()
        while stack:
            folder, rel_dir = stack.pop()
            if time.monotonic() > deadline:
                raise UserError("技能包检查超时，请整理目录后重试。")
            try:
                with os.scandir(folder) as scan_entries:
                    children = []
                    for child in scan_entries:
                        if len(children) + entries >= MAX_PACKAGE_ENTRIES:
                            raise UserError("技能包项目过多，最多检查 4000 个文件和目录项。")
                        children.append(child)
                    children.sort(key=lambda entry: entry.name.casefold(), reverse=True)
            except OSError as exc:
                raise UserError("无法完整读取技能目录。") from exc
            for entry in children:
                entries += 1
                if entries > MAX_PACKAGE_ENTRIES:
                    raise UserError("技能包项目过多，最多检查 4000 个文件和目录项。")
                path = Path(entry.path)
                if has_link(path):
                    raise UserError("技能包含符号链接或目录联接；完整收藏已取消。")
                relative = (rel_dir / entry.name).as_posix()
                self._check_name(relative)
                if relative.casefold() in folded_paths:
                    raise UserError("技能包包含大小写相同的路径，无法安全复制。")
                folded_paths.add(relative.casefold())
                try:
                    info = path.lstat()
                except OSError as exc:
                    raise UserError("技能包在读取期间发生变化，请重新预览。", 409) from exc
                if stat.S_ISDIR(info.st_mode):
                    stack.append((path, rel_dir / entry.name))
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise UserError("技能包包含非普通文件，无法安全收藏。")
                if len(files) >= MAX_PACKAGE_FILES:
                    raise UserError("技能包文件超过 2000 个。")
                total += info.st_size
                if total > MAX_PACKAGE_BYTES:
                    raise UserError("技能包超过 50 MiB。")
                digest = hashlib.sha256()
                try:
                    with path.open("rb") as handle:
                        while True:
                            block = handle.read(1024 * 1024)
                            if not block:
                                break
                            digest.update(block)
                            if time.monotonic() > deadline:
                                raise UserError("技能包检查超时，请整理目录后重试。")
                    after = path.lstat()
                except OSError as exc:
                    raise UserError("技能包在读取期间发生变化，请重新预览。", 409) from exc
                if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise UserError("技能包在预览期间发生变化，请重新预览。", 409)
                files.append({"path": relative, "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                              "sha256": digest.hexdigest()})
        files.sort(key=lambda item: item["path"].casefold())
        if not any(item["path"].casefold() == "skill.md" for item in files):
            raise UserError("技能包缺少 SKILL.md。")
        package = {"files": files, "file_count": len(files), "total_bytes": total}
        version_files = [{key: item[key] for key in ("path", "size", "sha256")} for item in files]
        version = hashlib.sha256(_canonical({"files": version_files, "file_count": len(files),
                                             "total_bytes": total}).encode("utf-8")).hexdigest()
        return row, root, origin_key, origin, package, version

    def _dependency_review(self, skill_root):
        """Flag parent-relative SKILL.md paths as leads, not proven dependencies."""
        skill_file = next((p for p in Path(skill_root).iterdir() if p.name.casefold() == "skill.md"), Path(skill_root) / "SKILL.md")
        if has_link(skill_file) or not skill_file.is_file():
            return []
        try:
            content = skill_file.read_bytes().decode("utf-8-sig", errors="replace")
        except OSError as exc:
            raise UserError("无法检查 SKILL.md 中的相对路径。", 409) from exc
        references = []
        seen = set()
        for match in _PARENT_REFERENCE.finditer(content):
            reference = match.group(0).rstrip(".,;:!?)]}>")
            if reference and reference.casefold() not in seen:
                references.append(reference[:240])
                seen.add(reference.casefold())
                if len(references) >= 20:
                    break
        return references

    @staticmethod
    def _collection_id(origin_key):
        return "col_" + hashlib.sha256(origin_key.encode("utf-8")).hexdigest()[:32]

    def preview(self, skill_id):
        with self.library.lock:
            row, root, origin_key, origin, package, version = self._snapshot(str(skill_id))
            possible_external_dependencies = self._dependency_review(root)
            warnings = [_PACKAGE_SCOPE_WARNING]
            if possible_external_dependencies:
                warnings.append(_PARENT_REFERENCE_WARNING)
            token = uid()
            created = time.time()
            with self.store.lock, self.store.connection() as db:
                db.execute("DELETE FROM yx_skill_collection_previews WHERE expires<? AND collected_id=''", (created,))
                db.execute("""INSERT INTO yx_skill_collection_previews(
                    token,source_skill_id,origin_key,origin,name,description,version,manifest,
                    total_bytes,file_count,created,expires) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (token, row["id"], origin_key, json_text(origin), row["name"], row["description"], version,
                     json_text(package), package["total_bytes"], package["file_count"], created,
                     created + PREVIEW_TTL_SECONDS))
                db.execute("DELETE FROM yx_skill_collection_previews WHERE token IN (SELECT token FROM yx_skill_collection_previews ORDER BY created DESC LIMIT -1 OFFSET ?)",
                           (MAX_PREVIEWS,))
            return {"token": token, "skill_id": row["id"], "name": row["name"], "description": row["description"],
                    "origin": origin, "version": version, "file_count": package["file_count"],
                    "total_bytes": package["total_bytes"], "manifest": package,
                    "package_scope": "skill-directory-only", "dependency_status": "not-checked",
                    "possible_external_dependencies": possible_external_dependencies, "warnings": warnings}

    def _version_path(self, collection_id, version):
        if not re.fullmatch(r"col_[0-9a-f]{32}", str(collection_id)) or not re.fullmatch(r"[0-9a-f]{64}", str(version)):
            raise UserError("技能收藏版本标识无效。")
        path = self.root / collection_id / version
        if has_link(self.root) or _has_link_if_present(path.parent) or _has_link_if_present(path):
            raise UserError("收藏目录包含链接，无法安全读取。")
        return path

    def _verify_version(self, collection_id, version, manifest_text=None):
        path = self._version_path(collection_id, version)
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM yx_skill_collection_versions WHERE collection_id=? AND version=?",
                             (collection_id, version)).fetchone()
        if row is None:
            raise UserError("收藏版本不存在。", 404)
        if manifest_text is not None and row["manifest"] != manifest_text:
            raise UserError("预览清单发生变化，请重新预览。", 409)
        expected = json.loads(row["manifest"])
        if not isinstance(expected.get("files"), list):
            raise UserError("收藏版本清单格式无效。", 409)
        package_files = [{key: item[key] for key in ("path", "size", "sha256")} for item in expected["files"]]
        expected_version = hashlib.sha256(_canonical({
            "files": package_files, "file_count": expected["file_count"],
            "total_bytes": expected["total_bytes"],
        }).encode("utf-8")).hexdigest()
        if expected_version != version or row["package_sha256"] != version:
            raise UserError("收藏版本清单与版本号不匹配。", 409)
        files_root = path / "files"
        if not path.is_dir() or has_link(files_root) or not files_root.is_dir():
            raise UserError("收藏版本目录不存在。", 404)
        manifest_file = path / "manifest.json"
        if has_link(manifest_file) or not manifest_file.is_file():
            raise UserError("收藏版本清单缺失或不是普通文件。", 409)
        try:
            disk_manifest_text = (path / "manifest.json").read_text(encoding="utf-8")
            disk_manifest = json.loads(disk_manifest_text)
        except (OSError, UnicodeError, ValueError) as exc:
            raise UserError("收藏版本清单缺失或格式无效。", 409) from exc
        if (disk_manifest_text != row["manifest"]
                or disk_manifest.get("schema") != SCHEMA or disk_manifest.get("id") != collection_id
                or disk_manifest.get("version") != version or disk_manifest.get("files") != expected["files"]
                or disk_manifest.get("file_count") != expected["file_count"]
                or disk_manifest.get("total_bytes") != expected["total_bytes"]):
            raise UserError("收藏版本清单已改变。", 409)
        for item in expected["files"]:
            self._check_name(item["path"])
            file_path = path / "files" / Path(*PurePosixPath(item["path"]).parts)
            try:
                file_path.resolve(strict=True).relative_to(files_root.resolve(strict=True))
                if has_link(file_path) or not file_path.is_file() or file_path.stat().st_size != item["size"]:
                    raise UserError("收藏版本已改变，无法读取。", 409)
                if _sha256(file_path) != item["sha256"]:
                    raise UserError("收藏版本校验失败，无法读取。", 409)
            except (OSError, ValueError) as exc:
                raise UserError("收藏版本文件缺失或越界。", 409) from exc
        return dict(row), path, expected

    def collect(self, token):
        if not isinstance(token, str) or len(token) != 32:
            raise UserError("请先预览技能包，再明确收藏。")
        with self.library.lock:
            with self.store.connection() as db:
                preview = db.execute("SELECT * FROM yx_skill_collection_previews WHERE token=?", (token,)).fetchone()
            if preview is None:
                raise UserError("收藏预览已过期，请重新预览。", 409)
            preview = dict(preview)
            if preview["collected_id"]:
                return self.get(preview["collected_id"], preview["collected_version"])
            if preview["expires"] < time.time():
                raise UserError("收藏预览已过期，请重新预览。", 409)
            _row, _root, origin_key, _origin, package, version = self._snapshot(preview["source_skill_id"])
            if (origin_key != preview["origin_key"] or version != preview["version"]
                    or _canonical(package) != _canonical(json.loads(preview["manifest"]))):
                raise UserError("源技能自预览后已发生变化，请重新预览并确认新版本。", 409)

            with self.store.connection() as db:
                existing = db.execute("SELECT * FROM yx_skill_collections WHERE source_skill_id=? OR origin_key=?",
                                      (preview["source_skill_id"], origin_key)).fetchone()
            collection_id = existing["id"] if existing else self._collection_id(origin_key)
            final = self._version_path(collection_id, version)
            manifest = {
                "schema": SCHEMA, "id": collection_id, "skill_id": preview["source_skill_id"],
                "version": version, "package_sha256": version,
                "origin": json.loads(preview["origin"]), "name": preview["name"],
                "description": preview["description"], "created": now(), "files": package["files"],
                "file_count": package["file_count"], "total_bytes": package["total_bytes"],
            }
            possible_external_dependencies = self._dependency_review(_root)
            manifest["package_scope"] = "skill-directory-only"
            manifest["dependency_status"] = "not-checked"
            manifest["possible_external_dependencies"] = possible_external_dependencies
            manifest["warnings"] = [_PACKAGE_SCOPE_WARNING] + (
                [_PARENT_REFERENCE_WARNING] if possible_external_dependencies else [])
            manifest_text = json_text(manifest)
            with self.store.connection() as db:
                present = db.execute("SELECT 1 FROM yx_skill_collection_versions WHERE collection_id=? AND version=?",
                                     (collection_id, version)).fetchone()
            if present:
                _checked, _checked_path, saved_package = self._verify_version(collection_id, version)
                if _canonical(saved_package) != _canonical(package):
                    raise UserError("既有收藏版本清单不一致。", 409)
            else:
                parent = self.root / collection_id
                if has_link(self.root) or _has_link_if_present(parent):
                    raise UserError("收藏目标目录包含链接。")
                parent.mkdir(parents=True, exist_ok=True)
                if final.exists():
                    raise UserError("发现未登记的同名收藏目录；为保护原件，不能覆盖它。", 409)
                stage = parent / (".pending_" + uid())
                stage.mkdir()
                try:
                    files_root = stage / "files"
                    files_root.mkdir()
                    for item in package["files"]:
                        source = _root / Path(*PurePosixPath(item["path"]).parts)
                        self._check_name(item["path"])
                        target = files_root / Path(*PurePosixPath(item["path"]).parts)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if has_link(source) or not source.is_file():
                            raise UserError("源技能在复制期间发生变化，请重新预览。", 409)
                        digest = hashlib.sha256()
                        before = source.stat()
                        with source.open("rb") as incoming, target.open("xb") as outgoing:
                            while True:
                                block = incoming.read(1024 * 1024)
                                if not block:
                                    break
                                outgoing.write(block)
                                digest.update(block)
                        after = source.stat()
                        if (digest.hexdigest() != item["sha256"] or before.st_size != item["size"]
                                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                            raise UserError("源技能在复制期间发生变化，请重新预览。", 409)
                    (stage / "manifest.json").write_text(manifest_text, encoding="utf-8", newline="\n")
                    os.replace(stage, final)
                except BaseException:
                    if stage.exists():
                        shutil.rmtree(stage, ignore_errors=True)
                    raise

            with self.store.lock, self.store.connection() as db:
                created = existing["created"] if existing else now()
                if existing:
                    db.execute("UPDATE yx_skill_collections SET name=?,description=?,origin=?,current_version=?,updated=? WHERE id=?",
                               (preview["name"], preview["description"], preview["origin"], version, now(), collection_id))
                else:
                    db.execute("""INSERT INTO yx_skill_collections(
                        id,source_skill_id,origin_key,origin,name,description,current_version,created,updated)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                        (collection_id, preview["source_skill_id"], origin_key, preview["origin"], preview["name"],
                         preview["description"], version, created, now()))
                db.execute("""INSERT OR IGNORE INTO yx_skill_collection_versions(
                    collection_id,version,package_sha256,path,manifest,total_bytes,file_count,created)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (collection_id, version, version, str(final), manifest_text, package["total_bytes"],
                     package["file_count"], now()))
                db.execute("UPDATE yx_skill_collection_previews SET collected_id=?,collected_version=? WHERE token=?",
                           (collection_id, version, token))
            return self.get(collection_id, version)

    def _entry(self, row, project_id=""):
        versions = self._versions(row["id"])
        latest = row["current_version"]
        binding = None
        if project_id:
            with self.store.connection() as db:
                binding = db.execute("SELECT collection_id,collection_version FROM yx_project_skills WHERE project_id=? AND skill_id=?",
                                     (project_id, row["source_skill_id"])).fetchone()
        bound_version = binding["collection_version"] if binding and binding["collection_id"] == row["id"] else ""
        selected = bound_version or latest
        selected_row = next((item for item in versions if item["version"] == selected), None)
        origin = json.loads(row["origin"])
        return {
            "id": row["id"], "skill_id": row["source_skill_id"], "name": row["name"],
            "description": row["description"], "category": row["category"], "tags": json.loads(row["tags"]),
            "version": selected, "current_version": latest, "versions": versions,
            "path": str(Path(selected_row["path"]) / "files") if selected_row else "",
            "manifest_path": str(Path(selected_row["path"]) / "manifest.json") if selected_row else "",
            "origin": origin, "bound": bool(binding), "pinned": bool(bound_version),
            "bound_version": bound_version,
        }

    def _versions(self, collection_id):
        with self.store.connection() as db:
            rows = db.execute("SELECT version,package_sha256,path,total_bytes,file_count,created FROM yx_skill_collection_versions WHERE collection_id=? ORDER BY created DESC,version",
                              (collection_id,)).fetchall()
        return [dict(item) for item in rows]

    def list(self, project_id=""):
        if project_id:
            self.store.get_project(project_id)
        with self.library.lock, self.store.connection() as db:
            rows = db.execute("SELECT * FROM yx_skill_collections ORDER BY name COLLATE NOCASE,id").fetchall()
            categories = [dict(row) for row in db.execute("SELECT id,label,builtin FROM yx_skill_categories ORDER BY builtin DESC,label COLLATE NOCASE,id")]
        entries = [self._entry(dict(row), project_id) for row in rows]
        return {"collections": entries, "categories": categories, "total": len(entries)}

    def _get_collection(self, collection_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM yx_skill_collections WHERE id=?", (str(collection_id),)).fetchone()
        if row is None:
            raise UserError("本地收藏不存在。", 404)
        return dict(row)

    def get(self, collection_id, version=None):
        row = self._get_collection(collection_id)
        version = version or row["current_version"]
        verified, version_root, package = self._verify_version(collection_id, version)
        path = version_root / "files"
        skill_file = next((item for item in package["files"] if item["path"].casefold() == "skill.md"), None)
        if skill_file is None:
            raise UserError("收藏版本缺少 SKILL.md。", 409)
        content_path = path / skill_file["path"]
        try:
            raw = content_path.read_bytes()
            content = raw.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise UserError("无法读取收藏的 SKILL.md。", 409) from exc
        result = self._entry(row)
        result.update(version=version, path=str(path), manifest_path=str(version_root / "manifest.json"),
                      manifest=json.loads(verified["manifest"]), content=content,
                      etag=hashlib.sha256(raw).hexdigest(), file_count=verified["file_count"],
                      total_bytes=verified["total_bytes"], editable=False, available=True)
        result["name"] = result["manifest"].get("name", result["name"])
        result["description"] = result["manifest"].get("description", result["description"])
        result["origin"] = result["manifest"].get("origin", result["origin"])
        result["package_scope"] = result["manifest"].get("package_scope", "skill-directory-only")
        result["dependency_status"] = result["manifest"].get("dependency_status", "not-checked")
        result["possible_external_dependencies"] = result["manifest"].get("possible_external_dependencies", [])
        result["warnings"] = result["manifest"].get("warnings", [_PACKAGE_SCOPE_WARNING])
        return result

    def classify(self, collection_id, category=None, tags=None):
        with self.library.lock:
            row = self._get_collection(collection_id)
            if category is not None:
                if not isinstance(category, str) or len(category.strip()) > 40 or any(ord(c) < 32 for c in category):
                    raise UserError("用途分类需为 1–40 个字符。")
                label = category.strip()
                with self.store.lock, self.store.connection() as db:
                    known = db.execute("SELECT id FROM yx_skill_categories WHERE id=? OR label=? COLLATE NOCASE", (label, label)).fetchone()
                    if not label:
                        category_id = ""
                    elif known:
                        category_id = known["id"]
                    else:
                        count = db.execute("SELECT count(*) FROM yx_skill_categories WHERE builtin=0").fetchone()[0]
                        if count >= MAX_CUSTOM_CATEGORIES:
                            raise UserError("自定义用途分类最多 32 个。")
                        category_id = "custom_" + uid()
                        db.execute("INSERT INTO yx_skill_categories(id,label,builtin,created) VALUES(?,?,0,?)",
                                   (category_id, label, now()))
                    db.execute("UPDATE yx_skill_collections SET category=?,updated=? WHERE id=?",
                               (category_id, now(), collection_id))
            else:
                category_id = row["category"]
            if tags is not None:
                if not isinstance(tags, list) or len(tags) > MAX_TAGS or any(
                        not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > MAX_TAG_LENGTH
                        or any(ord(char) < 32 for char in tag) for tag in tags):
                    raise UserError("自定义标签最多 16 个，每个标签 1–40 个字符。")
                clean_tags = []
                seen = set()
                for tag in tags:
                    tag = tag.strip()
                    if tag.casefold() not in seen:
                        clean_tags.append(tag)
                        seen.add(tag.casefold())
                with self.store.lock, self.store.connection() as db:
                    db.execute("UPDATE yx_skill_collections SET tags=?,updated=? WHERE id=?",
                               (json_text(clean_tags), now(), collection_id))
            return self._entry(self._get_collection(collection_id))

    def latest_for_source(self, source_skill_id):
        with self.store.connection() as db:
            row = db.execute("SELECT id,current_version FROM yx_skill_collections WHERE source_skill_id=?",
                             (source_skill_id,)).fetchone()
        if row is None or not row["current_version"]:
            return None
        return {"collection_id": row["id"], "collection_version": row["current_version"]}

    def bind(self, project_id, collection_id, bound, version=None):
        if not isinstance(bound, bool):
            raise UserError("技能绑定状态必须为 true 或 false。")
        row = self._get_collection(collection_id)
        return self.library.bind(project_id, row["source_skill_id"], bound,
                                 collection_id=collection_id if bound else None,
                                 version=version if bound else None, _collection_only=True)

    def export(self, project_id=""):
        """Create a versioned, copied directory for explicit local integration.

        Skill files remain data and are copied without execution.  A project
        export only includes bindings with an explicit pinned collection
        version; legacy/unpinned bindings are returned in ``skipped_unpinned``.
        """
        if project_id:
            self.store.get_project(project_id)
            with self.store.connection() as db:
                bindings = db.execute("""SELECT b.skill_id,b.collection_id,b.collection_version
                    FROM yx_project_skills b WHERE b.project_id=? ORDER BY b.skill_id""", (project_id,)).fetchall()
            selected, skipped = [], []
            for binding in bindings:
                if not binding["collection_id"] or not binding["collection_version"]:
                    skipped.append(binding["skill_id"])
                    continue
                collection = self._get_collection(binding["collection_id"])
                selected.append((collection, binding["collection_version"]))
        else:
            selected, skipped = [], []
            with self.store.connection() as db:
                rows = db.execute("SELECT * FROM yx_skill_collections ORDER BY name COLLATE NOCASE,id").fetchall()
            selected = [(dict(row), row["current_version"]) for row in rows if row["current_version"]]
        files = 0
        total = 0
        items = []
        for collection, version in selected:
            version_row, _path, package = self._verify_version(collection["id"], version)
            version_manifest = json.loads(version_row["manifest"])
            files += version_row["file_count"]
            total += version_row["total_bytes"]
            if files > MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
                raise UserError("导出超过 50 MiB 或 2000 个文件，请减少技能后重试。")
            items.append({"id": collection["id"], "skill_id": collection["source_skill_id"],
                          "name": collection["name"], "version": version,
                          "category": collection["category"], "tags": json.loads(collection["tags"]),
                          "origin": json.loads(collection["origin"]),
                          "package_scope": version_manifest.get("package_scope", "skill-directory-only"),
                          "dependency_status": version_manifest.get("dependency_status", "not-checked"),
                          "possible_external_dependencies": version_manifest.get("possible_external_dependencies", []),
                          "warnings": version_manifest.get("warnings", [_PACKAGE_SCOPE_WARNING]),
                          "manifest": version_manifest["files"]})
        payload = {"schema": EXPORT_SCHEMA, "project_id": project_id,
                   "skills": [{**{key: value for key, value in item.items() if key != "manifest"},
                               "directory": "skills/" + item["id"],
                               "files": item["manifest"]} for item in items],
                   "skipped_unpinned": skipped, "total_bytes": total, "file_count": files}
        payload_text = json_text(payload)
        export_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        export_id = "export_" + export_hash[:32]
        if project_id:
            folder = safe_name(project_id)
        else:
            folder = "all"
        parent = self.exports_root / folder
        final = parent / export_id
        if has_link(self.exports_root) or _has_link_if_present(parent):
            raise UserError("导出目录包含链接。")
        parent.mkdir(parents=True, exist_ok=True)
        if _has_link_if_present(final):
            raise UserError("导出快照路径包含链接。", 409)
        if not final.exists():
            stage = parent / (".pending_" + uid())
            stage.mkdir()
            try:
                (stage / "skills").mkdir()
                for item in items:
                    collection = self._get_collection(item["id"])
                    _version_row, version_root, package = self._verify_version(item["id"], item["version"])
                    target_root = stage / "skills" / item["id"]
                    target_root.mkdir()
                    for file_item in package["files"]:
                        source = version_root / "files" / Path(*PurePosixPath(file_item["path"]).parts)
                        target = target_root / Path(*PurePosixPath(file_item["path"]).parts)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if has_link(source):
                            raise UserError("收藏版本出现链接，导出已取消。", 409)
                        digest = hashlib.sha256()
                        with source.open("rb") as incoming, target.open("xb") as outgoing:
                            while True:
                                block = incoming.read(1024 * 1024)
                                if not block:
                                    break
                                outgoing.write(block)
                                digest.update(block)
                        if digest.hexdigest() != file_item["sha256"]:
                            raise UserError("收藏版本在导出期间发生变化。", 409)
                (stage / "manifest.json").write_text(payload_text, encoding="utf-8", newline="\n")
                self._verify_export(stage, payload_text, items)
                os.replace(stage, final)
            except BaseException:
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)
                raise
        self._verify_export(final, payload_text, items)
        return {"path": str(final), "manifest_path": str(final / "manifest.json"),
                "manifest_sha256": export_hash, "project_id": project_id,
                "skills": len(items), "file_count": files, "total_bytes": total,
                "skipped_unpinned": skipped}

    @staticmethod
    def _verify_export(root, expected_manifest, items):
        """Require an existing content-addressed export to match byte for byte."""
        root = Path(root)
        if has_link(root) or not root.is_dir():
            raise UserError("导出快照目录不存在或包含链接。", 409)
        manifest_path = root / "manifest.json"
        if has_link(manifest_path) or not manifest_path.is_file():
            raise UserError("导出快照清单缺失或包含链接。", 409)
        try:
            actual_manifest = manifest_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise UserError("无法读取导出快照清单。", 409) from exc
        if actual_manifest != expected_manifest:
            raise UserError("同一导出快照的清单已改变，为保护原件没有覆盖它。", 409)

        expected_paths = {"manifest.json"}
        for item in items:
            skill_root = root / "skills" / item["id"]
            if has_link(skill_root) or not skill_root.is_dir():
                raise UserError("导出快照中的技能目录缺失或包含链接。", 409)
            for file_item in item["manifest"]:
                SkillCollections._check_name(file_item["path"])
                relative = PurePosixPath("skills") / item["id"] / PurePosixPath(file_item["path"])
                rel_text = relative.as_posix()
                expected_paths.add(rel_text)
                file_path = root / Path(*relative.parts)
                try:
                    file_path.resolve(strict=True).relative_to(root.resolve(strict=True))
                    if (has_link(file_path) or not file_path.is_file()
                            or file_path.stat().st_size != file_item["size"]
                            or _sha256(file_path) != file_item["sha256"]):
                        raise UserError("导出快照文件校验失败，为保护原件没有覆盖它。", 409)
                except (OSError, ValueError) as exc:
                    raise UserError("导出快照文件缺失或越界。", 409) from exc
        observed_paths = {"manifest.json"}
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            for directory in directories:
                candidate = current_path / directory
                if has_link(candidate):
                    raise UserError("导出快照包含目录链接。", 409)
            for filename in filenames:
                candidate = current_path / filename
                if has_link(candidate):
                    raise UserError("导出快照包含文件链接。", 409)
                observed_paths.add(candidate.relative_to(root).as_posix())
        if observed_paths != expected_paths:
            raise UserError("导出快照包含缺失或额外文件，为保护原件没有覆盖它。", 409)
