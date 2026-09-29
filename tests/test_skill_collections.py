import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yingxu.skill_collections import SkillCollections
from yingxu.skills import SkillLibrary
from yingxu.store import Store, UserError


class SkillCollectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.home_patch = patch("yingxu.skills.Path.home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.store = Store(self.root / "data", self.root / "projects")
        self.project = self.store.create_project("收藏测试")
        self.second_project = self.store.create_project("另一个项目")
        self.skill_dir = self.home / ".codex" / "skills" / "示例技能"
        self.skill_dir.mkdir(parents=True)
        self.skill_file = self.skill_dir / "SKILL.md"
        self.skill_file.write_text("---\nname: 示例技能\n---\n\n只读指引 v1\n", encoding="utf-8")
        (self.skill_dir / "scripts").mkdir()
        (self.skill_dir / "scripts" / "run.py").write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
        (self.skill_dir / "references").mkdir()
        (self.skill_dir / "references" / "note.txt").write_bytes(b"\xef\xbb\xbfnotes\r\n")
        self.library = SkillLibrary(self.store)
        self.source = self.library.list()["skills"][0]

    def collect(self):
        preview = self.library.collections.preview(self.source["id"])
        return preview, self.library.collections.collect(preview["token"])

    def test_preview_collects_complete_package_without_changing_source(self):
        before = {path.relative_to(self.skill_dir).as_posix(): path.read_bytes()
                  for path in self.skill_dir.rglob("*") if path.is_file()}
        preview, saved = self.collect()
        self.assertEqual(preview["file_count"], 3)
        self.assertEqual(saved["skill_id"], self.source["id"])
        self.assertEqual(saved["file_count"], 3)
        package = Path(saved["path"])
        self.assertEqual((package / "SKILL.md").read_bytes(), before["SKILL.md"])
        self.assertEqual((package / "scripts" / "run.py").read_bytes(), before["scripts/run.py"])
        self.assertEqual((package / "references" / "note.txt").read_bytes(), before["references/note.txt"])
        self.assertEqual({path.relative_to(self.skill_dir).as_posix(): path.read_bytes()
                          for path in self.skill_dir.rglob("*") if path.is_file()}, before)
        self.assertEqual(saved["manifest"]["schema"], "yingxu.skill-collection.v1")
        self.assertEqual(saved["manifest"]["package_sha256"], saved["version"])
        self.assertEqual(self.library.collections.collect(preview["token"])["version"], saved["version"])

    def test_preview_token_rejects_source_changes(self):
        preview = self.library.collections.preview(self.source["id"])
        self.skill_file.write_text("---\nname: 示例技能\n---\n\n已变更\n", encoding="utf-8")
        with self.assertRaises(UserError) as error:
            self.library.collections.collect(preview["token"])
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.library.collections.list()["total"], 0)

    def test_new_versions_are_immutable_and_project_bindings_pin_versions(self):
        _preview, first = self.collect()
        first_version = first["version"]
        first_content = self.skill_file.read_bytes().decode("utf-8")
        self.library.bind(self.project["id"], self.source["id"], True)
        bound_v1 = self.library.bound_skills(self.project["id"])[0]
        self.assertTrue(bound_v1["pinned"])
        self.assertEqual(bound_v1["version"], first_version)

        old_bytes = Path(first["path"], "SKILL.md").read_bytes()
        self.skill_file.write_text("---\nname: 示例技能\n---\n\n只读指引 v2\n", encoding="utf-8")
        self.library.refresh()
        _preview, second = self.collect()
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first_version, second["version"])
        self.assertEqual(Path(first["path"], "SKILL.md").read_bytes(), old_bytes)
        self.assertEqual(len(self.library.collections.list()["collections"][0]["versions"]), 2)

        # Repeating the legacy source-binding call must preserve an existing pin.
        self.library.bind(self.project["id"], self.source["id"], True)
        self.assertEqual(self.library.bound_skills(self.project["id"])[0]["version"], first_version)
        self.library.bind(self.second_project["id"], self.source["id"], True)
        self.assertEqual(self.library.bound_skills(self.second_project["id"])[0]["version"], second["version"])

        self.skill_file.unlink()
        self.library.refresh()
        pinned = self.library.bound_skills(self.project["id"])[0]
        self.assertTrue(pinned["available"])
        self.assertEqual(self.library.collections.get(first["id"], first_version)["content"], first_content)

    def test_existing_unpinned_binding_migration_is_preserved(self):
        _preview, saved = self.collect()
        with self.store.connection() as db:
            db.execute("INSERT INTO yx_project_skills(project_id,skill_id,created,collection_id,collection_version) VALUES(?,?,?,'','')",
                       (self.project["id"], self.source["id"], "old-binding"))
        self.library.bind(self.project["id"], self.source["id"], True)
        binding = self.library.bound_skills(self.project["id"])[0]
        self.assertFalse(binding["pinned"])
        self.assertEqual(binding["collection_id"], "")
        self.assertEqual(binding["version"], "")
        self.assertEqual(self.library.collections.list(self.project["id"])["collections"][0]["version"],
                         saved["version"])

    def test_classification_is_separate_from_source_and_project_export_is_pinned_only(self):
        _preview, saved = self.collect()
        classified = self.library.collections.classify(saved["id"], "visual", ["连续性", "镜头", "连续性"])
        self.assertEqual(classified["category"], "visual")
        self.assertEqual(classified["tags"], ["连续性", "镜头"])
        custom = self.library.collections.classify(saved["id"], "我自己的用途", ["自定义标签"])
        category = next(item for item in self.library.collections.list()["categories"] if item["id"] == custom["category"])
        self.assertFalse(category["builtin"])
        self.assertEqual(category["label"], "我自己的用途")

        self.library.bind(self.project["id"], self.source["id"], True)
        export = self.library.collections.export(self.project["id"])
        export_root = Path(export["path"])
        self.assertEqual(export["skills"], 1)
        self.assertEqual(export["skipped_unpinned"], [])
        self.assertTrue((export_root / "skills" / saved["id"] / "SKILL.md").is_file())
        manifest = json.loads((export_root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "yingxu.skill-collection-export.v1")
        self.assertEqual(manifest["skills"][0]["version"], saved["version"])
        self.assertEqual(manifest["skills"][0]["files"][0]["sha256"], saved["manifest"]["files"][0]["sha256"])
        repeated = self.library.collections.export(self.project["id"])
        self.assertEqual(repeated["path"], export["path"])
        self.assertEqual(repeated["manifest_sha256"], export["manifest_sha256"])
        self.assertEqual(len(list(Path(export["path"]).parent.glob("export_*"))), 1)
        tampered = export_root / "skills" / saved["id"] / "SKILL.md"
        tampered.write_text("tampered", encoding="utf-8")
        with self.assertRaises(UserError):
            self.library.collections.export(self.project["id"])

    def test_preview_flags_possible_parent_references_without_claiming_readiness(self):
        self.skill_file.write_text(
            "---\nname: 示例技能\n---\n\n读取 ../plugins/runtime.py\n", encoding="utf-8")
        self.library.refresh()
        source = self.library.list()["skills"][0]
        preview = self.library.collections.preview(source["id"])
        self.assertEqual(preview["package_scope"], "skill-directory-only")
        self.assertEqual(preview["dependency_status"], "not-checked")
        self.assertIn("../plugins/runtime.py", preview["possible_external_dependencies"])
        self.assertTrue(any("目录外的运行时" in warning for warning in preview["warnings"]))
        self.assertTrue(any("人工核对" in warning for warning in preview["warnings"]))
        collected = self.library.collections.collect(preview["token"])
        self.assertEqual(collected["possible_external_dependencies"], ["../plugins/runtime.py"])
        self.assertEqual(collected["dependency_status"], "not-checked")

    def test_rejects_traversal_credentials_and_oversize_packages(self):
        for value in ("../escape", "/absolute", "a\\..\\escape", "C:/absolute"):
            with self.subTest(value=value), self.assertRaises(UserError):
                self.library.collections._check_name(value)
        with self.assertRaises(UserError):
            self.library.collections._check_name(".env")
        with patch("yingxu.skill_collections.MAX_PACKAGE_BYTES", 1):
            with self.assertRaises(UserError):
                self.library.collections.preview(self.source["id"])

    def test_lowercase_skill_filename_uses_manifest_name(self):
        lower = self.skill_dir / "skill.md"
        self.skill_file.rename(self.skill_dir / "renaming.tmp")
        (self.skill_dir / "renaming.tmp").rename(lower)
        self.library.refresh()
        self.source = self.library.list()["skills"][0]
        preview, saved = self.collect()
        self.assertEqual(Path(saved["path"], "skill.md").read_bytes(), lower.read_bytes())
        # Emulate a case-sensitive FS even on the Windows runner.
        original_read = Path.read_bytes
        def case_sensitive_read(path):
            if path.name == "SKILL.md" and path.parent == Path(saved["path"]):
                raise FileNotFoundError("wrong case")
            return original_read(path)
        with patch.object(Path, "read_bytes", case_sensitive_read):
            self.assertIn("只读指引", self.library.collections.get(saved["id"])["content"])

    def test_rejects_directory_links(self):
        linked = self.skill_dir / "linked"
        try:
            linked.symlink_to(self.skill_dir / "references", target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation is unavailable in this test account")
        with self.assertRaises(UserError):
            self.library.collections.preview(self.source["id"])


if __name__ == "__main__":
    unittest.main()
