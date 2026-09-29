"""Synthetic SQLite-only regression tests for local AI handoff snapshots."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yingxu.handoffs import HandoffService
from yingxu.skills import SkillLibrary
from yingxu.store import Store, UserError


class FakeSkills:
    def __init__(self):
        self.entries = []

    def bound_skills(self, project_id):
        return list(self.entries)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yingxu-handoff-test-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.store = Store(root / 'data', root / 'projects')
        self.project = self.store.create_project('合成交接项目', '只用于测试的项目目标')
        self.pid = self.project['id']
        self.skills = FakeSkills()
        self.service = HandoffService(self.store, self.skills, history_limit=3)

    def item(self, name, content='合成资料\n'):
        return self.store.create_item({
            'project_id': self.pid, 'name': name, 'category': 'scripts', 'content': content,
        })

    def test_new_conversation_is_full_and_copy_does_not_acknowledge(self):
        asset = self.item('场景资料', '秘密正文不应进入快照\n')
        self.skills.entries = [{
            'id': 'skill-1', 'name': '合成技能', 'description': '只供参考',
            'source': 'yingxu', 'source_label': '映序本地', 'path': 'D:/skills/SKILL.md',
            'etag': 'rev-1', 'content': '本字段不能被复制',
        }]

        first = self.service.create(self.pid, 'codex', 'conversation-A', '完成场景草稿', [asset['id']])

        self.assertEqual(first['mode'], 'full')
        self.assertIsNone(first['base_snapshot_id'])
        self.assertFalse(first['acknowledged'])
        self.assertEqual(first['status'], 'generated')
        self.assertEqual(first['snapshot']['assets'][0]['text_hash_status'], 'hashed_on_demand')
        self.assertEqual(first['snapshot']['assets'][0]['text_sha256'], hashlib.sha256(Path(asset['path']).read_bytes()).hexdigest())
        self.assertNotIn('秘密正文', json.dumps(first['snapshot'], ensure_ascii=False))
        self.assertNotIn('本字段不能被复制', json.dumps(first['snapshot'], ensure_ascii=False))
        self.assertIn('完成场景草稿', first['prompt'])
        self.assertNotIn('秘密正文', first['prompt'])
        self.assertEqual(self.service.status(self.pid, 'codex', 'conversation-A')['pending_count'], 1)

    def test_only_acknowledged_baseline_creates_delta_and_task_stays_full(self):
        first = self.service.create(self.pid, 'codex', 'conversation-A', '第一版任务', [])
        generated_only = self.service.create(self.pid, 'codex', 'conversation-A', '尚未确认的任务', [])
        self.assertEqual(generated_only['mode'], 'full')

        accepted = self.service.acknowledge(first['snapshot_id'])
        self.assertTrue(accepted['acknowledged'])
        self.assertTrue(accepted['is_current_baseline'])
        delta = self.service.create(self.pid, 'codex', 'conversation-A', '第二版任务\n保留原有角色', [])

        self.assertEqual(delta['mode'], 'delta')
        self.assertEqual(delta['base_snapshot_id'], first['snapshot_id'])
        self.assertEqual(delta['snapshot']['task']['current'], '第二版任务\n保留原有角色')
        self.assertIn('-第一版任务', delta['snapshot']['task']['diff_from_baseline'])
        self.assertIn('+第二版任务', delta['prompt'])
        self.assertIn('第二版任务\n保留原有角色', delta['prompt'])
        self.assertNotIn('完整当前资料索引', delta['prompt'])
        self.assertIn('完整交接', self.service.create(self.pid, 'codex', 'conversation-A', '再次完整任务', [], force_full=True)['prompt'])

    def test_delta_lists_only_skill_changes_and_full_request_has_complete_fallback(self):
        self.skills.entries = [{
            'id': 'skill-old', 'name': '原技能', 'description': '原摘要', 'source': 'yingxu',
            'path': 'D:/skills/old/SKILL.md', 'etag': 'v1',
        }, {
            'id': 'skill-stays', 'name': '沿用技能', 'description': '保留', 'source': 'yingxu',
            'path': 'D:/skills/stays/SKILL.md', 'etag': 'v1',
        }]
        baseline = self.service.create(self.pid, 'codex', 'A', '完整目标', [])
        self.service.acknowledge(baseline['id'])
        self.skills.entries = [{
            'id': 'skill-new', 'name': '新增技能', 'description': '新增摘要', 'source': 'codex',
            'path': 'D:/skills/new/SKILL.md', 'etag': 'v2',
        }, {
            'id': 'skill-stays', 'name': '沿用技能', 'description': '保留', 'source': 'yingxu',
            'path': 'D:/skills/stays/SKILL.md', 'etag': 'v1',
        }]

        delta = self.service.create(self.pid, 'codex', 'A', '继续目标', [])

        self.assertEqual(delta['changes']['skills']['added'][0]['id'], 'skill-new')
        self.assertEqual(delta['changes']['skills']['removed_from_project'][0]['id'], 'skill-old')
        self.assertIn('skill-new', delta['prompt'])
        self.assertIn('skill-old', delta['prompt'])
        self.assertNotIn('skill-stays', delta['prompt'])
        self.assertNotIn('沿用技能', delta['prompt'])
        self.assertEqual(len(delta['snapshot']['skills']), 2)

        full = self.service.create(self.pid, 'codex', 'A', '完整重交接', [], force_full=True)
        self.assertEqual(full['mode'], 'full')
        self.assertIn('新增技能', full['prompt'])
        self.assertIn('沿用技能', full['prompt'])

    def test_scope_uses_project_client_and_explicit_conversation(self):
        first = self.service.create(self.pid, 'codex', 'A', '任务', [])
        self.service.acknowledge(first['id'])

        self.assertEqual(self.service.create(self.pid, 'codex', 'B', '任务', [])['mode'], 'full')
        self.assertEqual(self.service.create(self.pid, 'dsh', 'A', '任务', [])['mode'], 'full')
        other = self.store.create_project('另一个合成项目')
        self.assertEqual(self.service.create(other['id'], 'codex', 'A', '任务', [])['mode'], 'full')
        with self.assertRaises(UserError):
            self.service.create(self.pid, 'codex', '', '任务', [])

    def test_diff_tracks_stable_ids_and_separates_scope_from_project_removal(self):
        keep = self.item('保留资料', '第一版\n')
        omit = self.item('临时范围资料')
        base = self.service.create(self.pid, 'codex', 'A', '任务', [keep['id'], omit['id']])
        self.service.acknowledge(base['id'])

        Path(keep['path']).write_text('第二版\n', encoding='utf-8')
        current = self.service.create(self.pid, 'codex', 'A', '任务', [keep['id']])
        changes = current['changes']['assets']
        self.assertEqual(changes['changed'][0]['id'], keep['id'])
        self.assertEqual(changes['out_of_scope'][0]['id'], omit['id'])
        self.assertTrue(changes['out_of_scope'][0]['retain_original'])
        self.assertFalse(changes['out_of_scope'][0]['delete_request'])

        with self.store.connection() as db:
            db.execute('UPDATE items SET removed=1 WHERE id=?', (omit['id'],))
        removed = self.service.create(self.pid, 'codex', 'A', '任务', [keep['id']])
        self.assertEqual(removed['changes']['assets']['project_removed'][0]['id'], omit['id'])
        self.assertFalse(removed['changes']['assets']['project_removed'][0]['delete_request'])

    def test_selected_assets_only_and_media_is_not_hashed(self):
        selected = self.item('只选这个')
        self.item('没有选择这个')
        root = Path(self.project['root'])
        video_path = root / '合成视频.mp4'
        video_path.write_bytes(b'large-media-placeholder')
        source = next(row for row in self.store.sources(self.pid) if row['path'] == str(root))
        self.store.index_files(source, [video_path])
        with self.store.connection() as db:
            video_id = db.execute('SELECT id FROM items WHERE project_id=? AND path=?', (self.pid, str(video_path))).fetchone()[0]

        result = self.service.create(self.pid, 'codex', 'A', '任务', [selected['id'], video_id])

        self.assertEqual({row['id'] for row in result['snapshot']['assets']}, {selected['id'], video_id})
        video = next(row for row in result['snapshot']['assets'] if row['id'] == video_id)
        self.assertEqual(video['text_hash_status'], 'not_text')
        self.assertIsNone(video['text_sha256'])
        empty = self.service.create(self.pid, 'codex', 'B', '任务', [])
        self.assertEqual(empty['snapshot']['assets'], [])

    def test_private_client_config_is_rejected_when_selected_and_filtered_from_all_scope(self):
        public = self.item('公开资料')
        private = self.item('client_config', 'api_key=synthetic-private\n')

        with self.assertRaises(UserError):
            self.service.create(self.pid, 'codex', 'A', '任务', [private['id']])
        all_assets = self.service.create(self.pid, 'codex', 'B', '任务', None)
        self.assertEqual([row['id'] for row in all_assets['snapshot']['assets']], [public['id']])
        self.assertEqual(all_assets['snapshot']['excluded_asset_count'], 1)
        self.assertNotIn('synthetic-private', all_assets['prompt'])

    def test_acknowledgement_is_idempotent_and_refuses_stale_regression(self):
        first = self.service.create(self.pid, 'codex', 'A', '一', [])
        self.service.acknowledge(first['id'])
        older = self.service.create(self.pid, 'codex', 'A', '二', [])
        newer = self.service.create(self.pid, 'codex', 'A', '三', [])
        current = self.service.acknowledge(newer['id'])
        self.assertTrue(current['is_current_baseline'])
        with self.assertRaises(UserError) as error:
            self.service.acknowledge(older['id'])
        self.assertEqual(error.exception.status, 409)
        again = self.service.acknowledge(newer['id'])
        self.assertTrue(again['is_current_baseline'])
        self.assertEqual(self.service.status(self.pid, 'codex', 'A')['acknowledged_baseline']['snapshot_id'], newer['id'])

    def test_older_baseline_survives_bounded_history_pruning_and_payload_is_immutable(self):
        baseline = self.service.create(self.pid, 'codex', 'A', '基线', [])
        self.service.acknowledge(baseline['id'])
        for index in range(5):
            self.service.create(self.pid, 'codex', 'A', f'未确认 {index}', [])

        history = self.service.list_scope(self.pid, 'codex', 'A')
        self.assertLessEqual(len(history), 4)  # newest three, plus a protected older baseline
        self.assertTrue(any(row['id'] == baseline['id'] and row['is_current_baseline'] for row in history))
        self.assertEqual(self.service.get(baseline['id'])['snapshot_digest'], baseline['snapshot_digest'])
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connection() as db:
                db.execute("UPDATE handoff_snapshots SET snapshot_json='{}' WHERE id=?", (baseline['id'],))

    def test_snapshot_digest_covers_immutable_snapshot(self):
        result = self.service.create(self.pid, 'codex', 'A', '摘要验收', [])
        snapshot = dict(result['snapshot'])
        digest = snapshot.pop('snapshot_digest')
        payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)

    def test_pinned_skill_source_edit_and_pin_switch_use_immutable_collection_versions(self):
        home = Path(self.temp.name) / 'home'
        skill_dir = home / '.codex' / 'skills' / '导演技能'
        skill_dir.mkdir(parents=True)
        source_file = skill_dir / 'skill.md'
        source_file.write_text('---\nname: 导演技能\n---\n\n来源版本一\n', encoding='utf-8')

        with patch('yingxu.skills.Path.home', return_value=home):
            library = SkillLibrary(self.store)
        source = library.list()['skills'][0]
        preview_v1 = library.collections.preview(source['id'])
        version_v1 = library.collections.collect(preview_v1['token'])
        library.bind(self.pid, source['id'], True, collection_id=version_v1['id'], version=version_v1['version'])
        service = HandoffService(self.store, library)
        baseline = service.create(self.pid, 'codex', 'A', '继续导演任务', [])
        pinned_v1 = baseline['snapshot']['skills'][0]
        self.assertEqual(pinned_v1['id'], source['id'])
        self.assertEqual(pinned_v1['source_skill_id'], source['id'])
        self.assertEqual(pinned_v1['source_id'], 'codex')
        self.assertEqual(pinned_v1['collection_id'], version_v1['id'])
        self.assertTrue(pinned_v1['pinned'])
        self.assertEqual(pinned_v1['version'], version_v1['version'])
        self.assertEqual(pinned_v1['path'], str(Path(version_v1['path']) / 'skill.md'))
        self.assertNotEqual(pinned_v1['path'], source['path'])
        service.acknowledge(baseline['id'])

        source_file.write_text('---\nname: 导演技能\n---\n\n来源版本二\n', encoding='utf-8')
        library.refresh()
        preview_v2 = library.collections.preview(source['id'])
        version_v2 = library.collections.collect(preview_v2['token'])
        self.assertNotEqual(version_v1['version'], version_v2['version'])

        still_pinned = service.create(self.pid, 'codex', 'A', '继续导演任务', [])
        current_v1 = still_pinned['snapshot']['skills'][0]
        self.assertEqual(still_pinned['changes']['skills']['changed'], [])
        self.assertEqual(current_v1['version'], version_v1['version'])
        self.assertEqual(current_v1['path'], pinned_v1['path'])
        self.assertNotIn(str(source_file), json.dumps(current_v1, ensure_ascii=False))

        library.bind(self.pid, source['id'], True, collection_id=version_v2['id'], version=version_v2['version'])
        switched = service.create(self.pid, 'codex', 'A', '继续导演任务', [])
        current_v2 = switched['snapshot']['skills'][0]
        self.assertEqual(switched['changes']['skills']['changed'][0]['id'], source['id'])
        self.assertEqual(current_v2['id'], source['id'])
        self.assertEqual(current_v2['source_id'], 'codex')
        self.assertTrue(current_v2['pinned'])
        self.assertEqual(current_v2['collection_id'], version_v2['id'])
        self.assertEqual(current_v2['version'], version_v2['version'])
        self.assertEqual(current_v2['path'], str(Path(version_v2['path']) / 'skill.md'))
        self.assertNotEqual(current_v2['path'], str(source_file))
        self.assertIn(version_v2['version'], switched['prompt'])

    def test_removed_source_keeps_pinned_skill_and_broken_pin_never_falls_back(self):
        home = Path(self.temp.name) / 'home'
        skill_dir = home / '.codex' / 'skills' / '只读技能'
        skill_dir.mkdir(parents=True)
        source_file = skill_dir / 'SKILL.md'
        source_file.write_text('---\nname: 只读技能\n---\n\n固定版本内容\n', encoding='utf-8')

        with patch('yingxu.skills.Path.home', return_value=home):
            library = SkillLibrary(self.store)
        source = library.list()['skills'][0]
        preview = library.collections.preview(source['id'])
        collected = library.collections.collect(preview['token'])
        library.bind(self.pid, source['id'], True, collection_id=collected['id'], version=collected['version'])
        source_file.unlink()
        library.refresh()
        library.remove(source['id'])
        bound = library.bound_skills(self.pid)[0]
        self.assertTrue(bound['pinned'])
        self.assertEqual(bound['version'], collected['version'])

        service = HandoffService(self.store, library)
        result = service.create(self.pid, 'codex', 'A', '继续已固定技能', [])
        skill = result['snapshot']['skills'][0]
        self.assertEqual(skill['id'], source['id'])
        self.assertEqual(skill['path'], str(Path(collected['path']) / 'SKILL.md'))
        self.assertNotEqual(skill['path'], str(source_file))
        self.assertFalse(source_file.exists())

        with patch.object(library.collections, 'get', side_effect=UserError('synthetic broken collection', 409)):
            with self.assertRaises(UserError) as error:
                service.create(self.pid, 'codex', 'B', '不能退回可变源', [])
        self.assertEqual(error.exception.status, 409)
        self.assertIn('收藏技能版本不可用', str(error.exception))


if __name__ == '__main__':
    unittest.main()
