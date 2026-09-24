import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from yingxu.incremental_update import select_release
from yingxu.store import UserError
from test_incremental_support import Fixture


class IncrementalUpdateTests(unittest.TestCase):
    def assert_no_runtime_requests(self, fixture):
        left, right = fixture.data_range('runtime/python313.zip')
        for kind, start, end in fixture.requests:
            if kind == 'zip':
                self.assertTrue(end < left or start > right, (start, end, left, right))

    def test_real_range_plan_download_only_changes_and_preserves_install(self):
        with Fixture() as fixture:
            manager = fixture.manager()
            status = fixture.plan(manager)
            self.assertEqual(status['state'], 'planned', status)
            self.assertEqual(status['changed_files'], 4)  # three members plus manifest
            self.assertEqual(status['reused_files'], 3)
            self.assertEqual(status['removed_files'], 1)
            expected = sum(fixture.info[name].compress_size for name in ('server.py', 'frontend/index.html', 'yingxu/new.py'))
            self.assertEqual(status['total_download_bytes'], expected)
            # No change payload is transferred until explicit download.
            for name in ('server.py', 'frontend/index.html', 'yingxu/new.py'):
                left, right = fixture.data_range(name)
                self.assertFalse(any(kind == 'zip' and start <= right and end >= left for kind, start, end in fixture.requests))
            status = fixture.download(manager)
            self.assertEqual(status['state'], 'ready', status)
            self.assertEqual(status['download_bytes'], expected)
            plan = json.loads(manager.ready_plan(status['plan_id']).read_bytes())
            stage = manager.ready_plan(status['plan_id']).parent / 'stage'
            self.assertEqual({str(path.relative_to(stage)).replace('\\', '/') for path in stage.rglob('*') if path.is_file()},
                             {'server.py', 'frontend/index.html', 'yingxu/new.py', 'RELEASE_MANIFEST.json'})
            self.assertEqual((stage / 'server.py').read_bytes(), fixture.new['server.py'])
            self.assertEqual((fixture.install / 'server.py').read_bytes(), fixture.old['server.py'])
            self.assertEqual(plan['install_root'], str(fixture.install.resolve()))
            self.assertEqual(plan['state'], 'ready')
            from yingxu.incremental_install import validate_plan
            self.assertEqual(validate_plan(fixture.data, fixture.install, status['plan_id'])['plan']['id'], status['plan_id'])
            self.assert_no_runtime_requests(fixture)

    def test_resume_validates_complete_members_and_never_uses_partial(self):
        with Fixture() as fixture:
            manager = fixture.manager()
            self.assertEqual(fixture.plan(manager)['state'], 'planned')
            fixture.failure_range = fixture.data_range('frontend/index.html')[0]
            self.assertEqual(fixture.download(manager)['state'], 'error')
            folder = fixture.data / 'updates/incremental' / manager.status()['plan_id']
            self.assertEqual((folder / 'stage/server.py').read_bytes(), fixture.new['server.py'])
            self.assertFalse(list(folder.rglob('*.part')))
            # A crash may leave an owned partial, which is discarded rather than
            # appended to or interpreted as a complete member during retry.
            (folder / 'stage/.member-interrupted.part').write_bytes(b'unverified partial')
            fixture.failure_range = None
            before = len(fixture.requests)
            status = fixture.download(manager)
            self.assertEqual(status['state'], 'ready', status)
            self.assertFalse(list(folder.rglob('*.part')))
            self.assertFalse(any(kind == 'zip' and start == fixture.data_range('server.py')[0]
                                 for kind, start, end in fixture.requests[before:]))
            # A fresh manager constructs the same immutable identity and rehashes
            # completed members; a corrupted member is downloaded again.
            restarted = fixture.manager()
            self.assertEqual(fixture.plan(restarted)['plan_id'], status['plan_id'])
            (folder / 'stage/server.py').write_bytes(b'corrupted!')
            before = len(fixture.requests)
            self.assertEqual(fixture.download(restarted)['state'], 'ready')
            expected = fixture.info['server.py'].compress_size
            self.assertEqual(restarted.status()['total_download_bytes'], expected)
            self.assertEqual(restarted.status()['download_bytes'], expected)
            self.assertEqual((folder / 'stage/server.py').read_bytes(), fixture.new['server.py'])
            self.assert_no_runtime_requests(fixture)

    def test_local_edits_before_or_after_preview_are_never_overwritten(self):
        with Fixture() as fixture:
            manager = fixture.manager()
            original = (fixture.install / 'server.py').read_bytes()
            (fixture.install / 'server.py').write_bytes(b'user customization')
            self.assertEqual(fixture.plan(manager)['state'], 'error')
            self.assertIn('本地修改', manager.status()['message'])
            (fixture.install / 'server.py').write_bytes(original)
            self.assertEqual(fixture.plan(manager)['state'], 'planned')
            (fixture.install / 'server.py').write_bytes(b'changed after preview')
            before = len(fixture.requests)
            self.assertEqual(fixture.download(manager)['state'], 'error')
            self.assertEqual(len(fixture.requests), before)
            self.assertEqual((fixture.install / 'server.py').read_bytes(), b'changed after preview')

    def test_local_edit_equal_to_new_release_is_still_a_conflict(self):
        with Fixture() as fixture:
            (fixture.install / 'server.py').write_bytes(fixture.new['server.py'])
            manager = fixture.manager()
            status = fixture.plan(manager)
            self.assertEqual(status['state'], 'error')
            self.assertIn('本地修改', status['message'])

    def test_latest_unverifiable_release_does_not_fall_back_to_current(self):
        with Fixture() as fixture:
            current = json.loads(json.dumps(fixture.releases[0]))
            current['tag_name'] = 'yingxu-v0.4.18'
            current['html_url'] = current['html_url'].replace('0.4.19', '0.4.18')
            for asset in current['assets']:
                asset['name'] = asset['name'].replace('0.4.19', '0.4.18')
                asset['browser_download_url'] = asset['browser_download_url'].replace('0.4.19', '0.4.18')
            fixture.releases[0]['assets'][0]['digest'] = None
            fixture.releases.append(current)
            manager = fixture.manager()
            status = fixture.plan(manager)
            self.assertEqual(status['state'], 'error')
            self.assertEqual(status['latest_version'], '0.4.19')

    def test_unmanaged_collision_and_modified_obsolete_are_preserved(self):
        with Fixture() as fixture:
            path = fixture.install / 'yingxu/new.py'
            path.write_bytes(fixture.new['yingxu/new.py'])
            manager = fixture.manager()
            self.assertEqual(fixture.plan(manager)['state'], 'error')
            self.assertIn('未登记', manager.status()['message'])
            path.unlink()
            obsolete = fixture.install / 'yingxu/obsolete.py'
            obsolete.write_bytes(b'private local edits')
            self.assertEqual(fixture.plan(manager)['state'], 'error')
            self.assertEqual(obsolete.read_bytes(), b'private local edits')

    def test_missing_obsolete_is_recorded_absent_but_missing_required_fails(self):
        with Fixture() as fixture:
            (fixture.install / 'yingxu/obsolete.py').unlink()
            manager = fixture.manager()
            self.assertEqual(fixture.plan(manager)['state'], 'planned')
            plan = manager._context['plan']
            self.assertEqual(plan['removed'], [{'path': 'yingxu/obsolete.py', 'expected_old_sha256': None}])
            (fixture.install / 'server.py').unlink()
            self.assertEqual(fixture.plan(manager)['state'], 'error')

    def test_old_release_without_bound_manifest_does_not_fallback_to_full_download(self):
        with Fixture() as fixture:
            fixture.metadata.pop('release_manifest_sha256')
            fixture.refresh_metadata()
            manager = fixture.manager()
            status = fixture.plan(manager)
            self.assertEqual(status['state'], 'error')
            self.assertIn('完整包', status['message'])
            self.assertIn('NOXEVYR/yingxu', status['release_url'])
            self.assertFalse(any(kind == 'zip' for kind, _, _ in fixture.requests))

    def test_external_asset_and_internal_manifest_digests_are_required(self):
        with Fixture() as fixture:
            fixture.releases[0]['assets'][1]['digest'] = 'sha256:' + '0' * 64
            manager = fixture.manager()
            self.assertEqual(fixture.plan(manager)['state'], 'error')
            self.assertFalse(any(kind == 'zip' for kind, _, _ in fixture.requests))
            fixture.metadata['release_manifest_sha256'] = '0' * 64
            fixture.refresh_metadata()
            self.assertEqual(fixture.plan(manager)['state'], 'error')
            self.assertIn('SHA-256', manager.status()['message'])

    def test_range_unsupported_wrong_or_short_response_never_downloads_full_archive(self):
        with Fixture() as fixture:
            manager = fixture.manager()
            for mode in ('unsupported', 'wrong', 'truncated'):
                fixture.range_mode = mode
                status = fixture.plan(manager)
                self.assertEqual(status['state'], 'error', status)
                self.assertNotEqual(status['message'], '')
            self.assertTrue(all(end - start + 1 == 22 for kind, start, end in fixture.requests if kind == 'zip'))

    def test_no_update_only_reads_release_metadata(self):
        with Fixture(new_version='0.4.18') as fixture:
            status = fixture.plan(fixture.manager())
            self.assertEqual(status['state'], 'current')
            self.assertEqual([row[0] for row in fixture.requests], ['api'])

    def test_plan_id_is_required_and_public_status_never_exposes_local_paths(self):
        with Fixture() as fixture:
            manager = fixture.manager()
            with self.assertRaises(UserError):
                manager.download('guess')
            self.assertEqual(fixture.plan(manager)['state'], 'planned')
            with self.assertRaises(UserError):
                manager.download('../stage')
            self.assertNotIn(str(fixture.root), json.dumps(manager.status()))
            manager.close()
            with self.assertRaises(UserError):
                manager.download(manager.status()['plan_id'])

    def test_planning_single_flight_and_close_cancels_before_payload(self):
        with Fixture() as fixture:
            manager = fixture.manager()
            started, finish = threading.Event(), threading.Event()
            def held_releases(network):
                started.set()
                finish.wait(3)
                return fixture.releases
            with patch.object(manager, '_releases', side_effect=held_releases) as fetch:
                manager.plan()
                self.assertTrue(started.wait(2))
                self.assertEqual(manager.plan()['state'], 'planning')
                self.assertEqual(fetch.call_count, 1)
                manager.close()
                finish.set()
                self.assertEqual(fixture.wait(manager)['state'], 'error')
            self.assertEqual(fixture.requests, [])

    def test_persisted_plan_change_is_not_accepted_for_install(self):
        with Fixture() as fixture:
            manager = fixture.manager()
            fixture.plan(manager)
            status = fixture.download(manager)
            path = manager.ready_plan(status['plan_id'])
            path.write_text('{}', encoding='utf-8')
            with self.assertRaises(UserError):
                manager.ready_plan(status['plan_id'])

    def test_version_and_repository_selection(self):
        with Fixture() as fixture:
            rows = fixture.releases
            self.assertEqual(select_release(rows)['version'], '0.4.19')
            rows[0]['draft'] = True
            self.assertIsNone(select_release(rows))
            rows[0]['draft'] = False
            rows[0]['assets'][0]['browser_download_url'] = 'https://github.com/another/repo/file.zip'
            self.assertIsNone(select_release(rows))


if __name__ == '__main__':
    unittest.main()
