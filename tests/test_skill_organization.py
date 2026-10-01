import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yingxu.skill_organization import MAX_FOLDER_DEPTH, SkillOrganization
from yingxu.skills import SkillLibrary
from yingxu.store import Store, UserError


class SkillOrganizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = (self.root / "home").resolve()
        self.home.mkdir()
        self.home_patch = patch("yingxu.skills.Path.home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.store = Store((self.root / "data").resolve(), (self.root / "projects").resolve())
        self.skills = [
            self.make_skill("demo-one", "演示技能一"),
            self.make_skill("demo-two", "演示技能二"),
        ]
        self.library = SkillLibrary(self.store)
        self.organization = self.library.organization
        self.rows = {row["name"]: row for row in self.library.list()["skills"]}

    def make_skill(self, folder, name):
        skill_file = (self.home / ".codex" / "skills" / folder / "SKILL.md").resolve()
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(f"---\nname: {name}\n---\n\n合成测试技能。\n", encoding="utf-8")
        return skill_file

    def skill_ids(self):
        return [self.rows["演示技能一"]["id"], self.rows["演示技能二"]["id"]]

    def test_uncollected_external_skills_can_be_organized_without_disk_mutation(self):
        before = [path.read_bytes() for path in self.skills]
        folder = self.organization.create_folder("剧本工具")
        saved = self.organization.update_metadata(
            [self.rows["演示技能一"]["id"]], folder_id=folder["id"],
            tags=["分镜", "检查"], notes="优先在预演前运行。",
        )
        self.assertEqual(saved["updated"], 1)
        with patch.object(self.library, "refresh", side_effect=AssertionError("organization must not scan")):
            listing = self.organization.list()
            self.organization.update_metadata([self.rows["演示技能一"]["id"]], tags=["新标签"])
        self.assertEqual(listing["total"], 2)
        self.assertEqual(next(row for row in listing["folders"] if row["id"] == folder["id"])["count"], 1)
        metadata = next(row for row in listing["metadata"] if row["skill_id"] == self.rows["演示技能一"]["id"])
        self.assertEqual(metadata["tags"], ["分镜", "检查"])
        self.assertEqual(metadata["notes"], "优先在预演前运行。")
        self.assertEqual([path.read_bytes() for path in self.skills], before)

    def test_append_is_default_fields_are_preserved_and_notes_are_single_skill(self):
        first, second = self.skill_ids()
        self.organization.update_metadata([first], tags=["镜头", "连续性"], notes="单技能备注")
        result = self.organization.update_metadata([first], tags=["镜头", "角色"])
        self.assertEqual(result["metadata"][0]["tags"], ["镜头", "连续性", "角色"])
        self.assertEqual(result["metadata"][0]["notes"], "单技能备注")
        with self.assertRaises(UserError):
            self.organization.update_metadata([first, second], tags=["批量"], notes="不得部分修改")
        current = next(row for row in self.organization.list()["metadata"] if row["skill_id"] == first)
        self.assertEqual(current["tags"], ["镜头", "连续性", "角色"])
        self.assertEqual(current["notes"], "单技能备注")
        self.organization.update_metadata([first], tags=[], tags_mode="replace")
        current = next(row for row in self.organization.list()["metadata"] if row["skill_id"] == first)
        self.assertEqual(current["tags"], [])
        self.assertEqual(current["notes"], "单技能备注")

    def test_full_tag_append_deduplicates_before_limit_and_batch_failure_rolls_back(self):
        first, second = self.skill_ids()
        full_tags = [f"标签{i:02d}" for i in range(32)]
        self.organization.update_metadata([first], tags=full_tags, tags_mode="replace")
        duplicate_append = self.organization.update_metadata([first], tags=full_tags)
        self.assertEqual(duplicate_append["metadata"][0]["tags"], full_tags)

        self.organization.update_metadata([second], tags=full_tags[:-1], tags_mode="replace")
        with self.assertRaises(UserError):
            # The second ID can be updated first, but overflow on the first must roll back the batch.
            self.organization.update_metadata([second, first], tags=["新增第33项"])
        metadata = {row["skill_id"]: row["tags"] for row in self.organization.list()["metadata"]}
        self.assertEqual(metadata[first], full_tags)
        self.assertEqual(metadata[second], full_tags[:-1])
        with self.assertRaises(UserError):
            self.organization.update_metadata([first], tags=["重复"] * 129)
        self.assertEqual({row["skill_id"]: row["tags"] for row in self.organization.list()["metadata"]}[first],
                         full_tags)

    def test_metadata_survives_library_restart_and_refresh(self):
        first = self.skill_ids()[0]
        folder = self.organization.create_folder("重启后保留")
        self.organization.update_metadata(
            [first], folder_id=folder["id"], tags=["持久标签"], notes="重启后仍在。"
        )

        reopened = SkillLibrary(self.store)
        refreshed = reopened.refresh()
        self.assertEqual(refreshed["total"], 2)
        listing = reopened.organization.list()
        self.assertIn(folder["id"], [row["id"] for row in listing["folders"]])
        metadata = next(row for row in listing["metadata"] if row["skill_id"] == first)
        self.assertEqual(metadata, {
            "skill_id": first, "folder_id": folder["id"],
            "tags": ["持久标签"], "notes": "重启后仍在。",
        })

    def test_batch_validation_is_atomic_and_collection_ids_share_source_metadata(self):
        first, second = self.skill_ids()
        folder = self.organization.create_folder("批量整理")
        with self.assertRaises(UserError):
            self.organization.update_metadata([first, "missing-skill"], folder_id=folder["id"], tags=["不得写入"])
        self.assertEqual(self.organization.list()["metadata"], [])

        preview = self.library.collections.preview(first)
        collection = self.library.collections.collect(preview["token"])
        classified = self.library.collections.classify(collection["id"], "视觉与分镜", ["旧收藏标签"])
        result = self.organization.update_metadata(
            [collection["id"], first], folder_id=folder["id"], tags=["新整理标签"], notes="收藏仍用源技能身份。"
        )
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["metadata"][0]["skill_id"], first)
        self.assertEqual(result["metadata"][0]["tags"], ["新整理标签"])
        self.assertEqual(classified["category"], "visual")
        stored_collection = self.library.collections.list()["collections"][0]
        self.assertEqual(stored_collection["category"], "visual")
        self.assertEqual(stored_collection["tags"], ["旧收藏标签"])
        self.assertEqual(second in [item["skill_id"] for item in result["metadata"]], False)

    def test_legacy_collection_tags_migrate_once_without_changing_use_category(self):
        first = self.skill_ids()[0]
        preview = self.library.collections.preview(first)
        collection = self.library.collections.collect(preview["token"])
        self.library.collections.classify(collection["id"], "开发与工具", ["旧标签一", "旧标签二"])
        with self.store.connection() as db:
            db.execute("DELETE FROM yx_skill_organization_migrations WHERE name='collection-tags-v1'")

        migrated = SkillOrganization(self.library)
        metadata = next(row for row in migrated.list()["metadata"] if row["skill_id"] == first)
        self.assertEqual(metadata["tags"], ["旧标签一", "旧标签二"])
        again = SkillOrganization(self.library)
        self.assertEqual(next(row for row in again.list()["metadata"] if row["skill_id"] == first)["tags"],
                         ["旧标签一", "旧标签二"])
        saved = self.library.collections.list()["collections"][0]
        self.assertEqual(saved["category"], "development")
        self.assertEqual(saved["tags"], ["旧标签一", "旧标签二"])

    def test_folder_hierarchy_rename_move_and_delete_boundaries(self):
        root = self.organization.create_folder("根目录")
        child = self.organization.create_folder("子目录", root["id"])
        leaf = self.organization.create_folder("末级目录", child["id"])
        self.assertTrue(leaf["id"].startswith("fld_"))
        self.assertEqual(leaf["path"], "根目录/子目录/末级目录")
        with self.assertRaises(UserError):
            self.organization.create_folder("超深", leaf["id"])
        self.assertEqual(MAX_FOLDER_DEPTH, 3)
        with self.assertRaises(UserError):
            self.organization.update_folder(root["id"], parent_id=leaf["id"])
        with self.assertRaises(UserError):
            self.organization.create_folder("根目录")
        renamed = self.organization.update_folder(child["id"], name="子目录改名")
        self.assertEqual(renamed["path"], "根目录/子目录改名")
        with self.assertRaises(UserError):
            self.organization.delete_folder(child["id"])
        self.organization.delete_folder(leaf["id"])
        self.organization.delete_folder(child["id"])
        self.organization.delete_folder(root["id"])

    def test_members_prevent_folder_delete_even_if_skill_is_removed(self):
        first = self.skill_ids()[0]
        folder = self.organization.create_folder("保留成员")
        self.organization.update_metadata([first], folder_id=folder["id"])
        with self.assertRaises(UserError) as error:
            self.organization.delete_folder(folder["id"])
        self.assertIn("移出", str(error.exception))
        self.library.remove(first)
        listing = self.organization.list()
        self.assertIn(first, [row["skill_id"] for row in listing["metadata"]])
        self.assertEqual(next(row for row in listing["folders"] if row["id"] == folder["id"])["count"], 0)
        with self.assertRaises(UserError):
            self.organization.delete_folder(folder["id"])
        self.organization.update_metadata([first], folder_id="")
        self.organization.delete_folder(folder["id"])

    def test_invalid_names_tags_notes_and_folder_ids_are_rejected(self):
        first = self.skill_ids()[0]
        for name in ("", "../逃逸", "含/分隔符", "NUL", "x" * 81):
            with self.subTest(name=name), self.assertRaises(UserError):
                self.organization.create_folder(name)
        for tags in ([""], ["x" * 41], [f"tag-{index}" for index in range(33)], [1]):
            with self.subTest(tags=tags), self.assertRaises(UserError):
                self.organization.update_metadata([first], tags=tags)
        with self.assertRaises(UserError):
            self.organization.update_metadata([first], notes="x" * 4001)
        with self.assertRaises(UserError):
            self.organization.update_metadata([first], folder_id="missing-folder")
        self.assertEqual(self.organization.list()["metadata"], [])


if __name__ == "__main__":
    unittest.main()
