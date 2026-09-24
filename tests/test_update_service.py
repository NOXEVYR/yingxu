"""Exercise the live HTTP write gate without downloading or touching installations."""
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_updates_http import UpdatesHttpTests
from yingxu.store import UserError


class IncrementalServiceHttpTests(UpdatesHttpTests):
    def setUp(self):
        super().setUp()
        self.manager = Mock()
        self.manager.status.return_value = {'state': 'ready', 'plan_id': 'a'*32}
        self.manager.plan.return_value = {'state': 'planning'}
        self.manager.download.return_value = {'state': 'downloading'}
        self.app.update_service.manager = self.manager
        self.installer = SimpleNamespace(
            prepare_install=Mock(return_value={'prepared': True, 'ticket': 'b'*32}),
            commit_install=Mock(return_value={'committed': True, 'ticket': 'b'*32}),
            cancel_install=Mock(return_value={'cancelled': True}))
        p = patch.dict('sys.modules', {'yingxu.incremental_install': self.installer})
        p.start(); self.addCleanup(p.stop)

    def prepare(self):
        return self.request('/api/updates/install/prepare', body={'plan_id': 'a'*32, 'native_pid': 123})

    def test_status_auth_and_no_implicit_network(self):
        for path in ('/api/settings', '/api/bootstrap', '/api/updates/status'):
            self.assertEqual(self.request(path, 'GET')[0], 200)
        self.manager.plan.assert_not_called(); self.manager.download.assert_not_called()
        self.assertEqual(self.request('/api/updates/status', 'GET', headers={'X-YingXu-Token': 'wrong'})[0], 403)
        self.assertEqual(self.request('/api/updates/plan', body={'url': 'https://invalid'})[0], 400)
        self.assertEqual(self.request('/api/updates/download', body={'plan_id': 'a'*32})[0], 200)
        self.manager.download.assert_called_once_with('a'*32)

    def test_prepare_blocks_future_writes_but_allows_read_and_cancel(self):
        self.assertEqual(self.prepare()[0], 200)
        self.assertEqual(self.request('/api/projects', body={'name': 'blocked'})[0], 409)
        self.assertEqual(self.request('/api/updates/plan')[0], 409)
        self.assertEqual(self.request('/api/projects', 'GET')[0], 200)
        self.assertEqual(self.request('/api/updates/install/cancel', body={'ticket': 'wrong'})[0], 409)
        self.assertEqual(self.request('/api/updates/install/cancel', body={'ticket': 'b'*32})[0], 200)
        self.assertEqual(self.request('/api/updates/plan')[0], 200)

    def test_prepare_rejects_inflight_writes_and_background_work(self):
        with self.app.update_service.mutation('POST', '/api/upload'):
            self.assertEqual(self.prepare()[0], 409)
        with self.app.migration_jobs.lock:
            self.app.migration_jobs.active = 'busy'
        self.assertEqual(self.prepare()[0], 409)
        self.app.migration_jobs.active = None
        self.app.jobs.jobs['synthetic'] = {'state': 'running'}
        self.assertEqual(self.prepare()[0], 409)
        self.app.jobs.jobs.clear()
        self.installer.prepare_install.assert_not_called()

    def test_prepare_failure_never_blocks_existing_app(self):
        self.installer.prepare_install.side_effect = RuntimeError('fixture helper unavailable')
        self.assertEqual(self.prepare()[0], 409)
        self.assertIsNone(self.app.update_service.pending)
        self.assertEqual(self.request('/api/updates/plan')[0], 200)

    def test_expired_ticket_cancels_and_reopens_write_gate(self):
        self.assertEqual(self.prepare()[0], 200)
        self.app.update_service.pending['expires'] = time.monotonic()-1
        self.assertEqual(self.request('/api/updates/plan')[0], 200)
        self.installer.cancel_install.assert_called_once()

    def test_commit_failure_keeps_app_running_and_can_cancel(self):
        self.assertEqual(self.prepare()[0], 200)
        self.installer.commit_install.side_effect = RuntimeError('fixture not ready')
        self.assertEqual(self.request('/api/updates/install/commit', body={'ticket': 'b'*32})[0], 409)
        self.assertEqual(self.request('/api/health', 'GET')[0], 200)
        self.assertFalse(self.app.update_service.committed)
        self.assertEqual(self.request('/api/updates/install/cancel', body={'ticket': 'b'*32})[0], 200)

    def test_commit_flushes_then_stops_server(self):
        self.assertEqual(self.prepare()[0], 200)
        code, result = self.request('/api/updates/install/commit', body={'ticket': 'b'*32})
        self.assertEqual(code, 200); self.assertTrue(result['committed'])
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(self.app.update_service.committed)
        with self.assertRaises(UserError):
            with self.app.update_service.mutation('POST', '/api/upload'): pass

    def test_lost_prepare_response_can_resolve_bound_ticket(self):
        code, result = self.request('/api/updates/install/status', 'GET')
        self.assertEqual(code, 200); self.assertFalse(result['prepared'])
        self.assertEqual(self.prepare()[0], 200)
        code, result = self.request('/api/updates/install/status', 'GET')
        self.assertEqual(code, 200); self.assertEqual(result['ticket'], 'b'*32)
        self.assertEqual(result['native_pid'], 123); self.assertEqual(result['plan_id'], 'a'*32)
        self.assertEqual(self.request('/api/updates/install/status', 'GET', headers={'X-YingXu-Token': 'bad'})[0], 403)

    def test_last_result_is_local_bounded_and_does_not_start_update(self):
        import json
        path = self.app.store.data_root/'updates/incremental-install/last-install.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'state': 'rolled_back', 'version': '0.4.19', 'message': 'fixture', 'restarted': False}), encoding='utf-8')
        code, result = self.request('/api/updates/status', 'GET')
        self.assertEqual(code, 200); self.assertEqual(result['last_install']['state'], 'rolled_back')
        self.assertFalse(result['last_install']['restarted'])
        self.manager.plan.assert_not_called()
        path.write_bytes(b'x'*16385)
        self.assertEqual(self.request('/api/updates/status', 'GET')[1]['last_install']['state'], 'failed')
