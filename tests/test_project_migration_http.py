import http.client
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server import Application, Server
from yingxu.migration_jobs import MigrationJobs
from yingxu.update_service import UpdateService


class MigrationHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yingxu-migration-http-')
        self.root = Path(self.temp.name).resolve()
        with patch('yingxu.skills.Path.home', return_value=self.root/'empty-home'):
            self.app = Application(self.root/'data', self.root/'projects')
        self.server = Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.close()
        self.temp.cleanup()

    def request(self, method, path, data=None, token=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        headers = {'X-YingXu-Token': self.app.token if token is None else token}
        body = None
        if data is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(data).encode()
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status, value = response.status, json.loads(response.read())
        connection.close()
        return status, value

    def test_http_copy_switch_restart_and_retry_preserve_identity_and_source(self):
        folder = self.app.project_library.create_folder({'name': '练习分类'})
        project = self.app.store.create_project('练习', folder_id=folder['id'])
        item = self.app.store.create_item({'project_id': project['id'], 'name': '剧本',
                                           'category': 'scripts', 'content': '# 原文\n'})
        original = Path(item['path'])
        destination = self.root/'new-location'
        destination.mkdir()
        payload = {'root': str(destination), 'project_ids': [project['id']]}
        self.assertEqual(self.request('POST', '/api/project-storage/migration/preview', payload, token='')[0], 403)
        status, preview = self.request('POST', '/api/project-storage/migration/preview', payload)
        self.assertEqual(status, 200, preview)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(self.app.store.project_root, self.root/'projects')
        status, accepted = self.request('POST', '/api/project-storage/migration', {'token': preview['token']})
        self.assertEqual(status, 202, accepted)
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            status, job = self.request('GET', '/api/project-storage/migration/jobs/'+accepted['job_id'])
            self.assertEqual(status, 200, job)
            if job['state'] != 'running':
                break
            time.sleep(.02)
        self.assertEqual(job['state'], 'done', job)
        self.assertEqual(self.request('POST', '/api/project-storage/migration', {'token': preview['token']})[1], accepted)
        current = self.app.store.get_item(item['id'])
        self.assertEqual(Path(current['path']), destination/'练习分类'/'练习'/'文本'/'剧本.md')
        self.assertEqual(original.read_bytes(), Path(current['path']).read_bytes())
        self.assertEqual(self.request('GET', '/api/project-storage')[1]['root'], str(destination))
        self.app.close()
        with patch('yingxu.skills.Path.home', return_value=self.root/'empty-home'):
            reopened = Application(self.root/'data', self.root/'projects')
        try:
            self.assertEqual(reopened.store.project_root, destination)
            self.assertEqual(reopened.store.get_item(item['id'])['path'], current['path'])
        finally:
            reopened.close()

    def test_http_write_gate_rejects_mutation_and_unauthorized_polling(self):
        with self.app.migration_jobs.lock:
            self.app.migration_jobs.active = 'synthetic-job'
        try:
            self.assertEqual(self.request('POST', '/api/projects', {'name': 'blocked'})[0], 409)
            self.assertEqual(self.request('PATCH', '/api/settings', {'confirm_delete': False})[0], 409)
            self.assertEqual(self.request('GET', '/api/health')[0], 200)
            self.assertEqual(self.app.store.list_projects(), [])
        finally:
            with self.app.migration_jobs.lock:
                self.app.migration_jobs.active = None


@unittest.skipUnless(os.name == 'nt', 'Windows picker subprocess handling')
class PickerMigrationHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yingxu-picker-migration-http-')
        self.root = Path(self.temp.name).resolve()
        self.picker_entered, self.picker_release = threading.Event(), threading.Event()
        self.migration_entered, self.migration_release = threading.Event(), threading.Event()
        self.executed = []
        # Exercise production HTTP handlers, picker and reservation gates without
        # initializing project storage or opening a real system dialog.
        self.app = Application.__new__(Application)
        self.app.token = 'synthetic-picker-migration-token'
        self.app.picker_lock = threading.Lock()
        self.app.native_picker = None
        self.app.store = SimpleNamespace(lock=threading.RLock(), data_root=self.root/'data')
        self.app.context = SimpleNamespace(_export_lock=threading.RLock())
        self.app.jobs = SimpleNamespace(lock=threading.Lock(), jobs={})
        self.app.changed = lambda: None
        migration = SimpleNamespace(
            preview=lambda body: {'token': 'synthetic-preview', 'files': 5, 'bytes': 6400},
            execute=self.execute_stub)
        self.app.migration_jobs = MigrationJobs(self.app, migration)
        self.app.update_service = UpdateService(self.app, self.root/'install', '0.4.19')
        self.server = Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.request_threads = []

    def tearDown(self):
        self.picker_release.set()
        self.migration_release.set()
        for thread in self.request_threads:
            thread.join(5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.app.migration_jobs.close()
        self.app.update_service.close()
        self.temp.cleanup()

    def execute_stub(self, body, progress=None):
        self.executed.append(body)
        self.migration_entered.set()
        if not self.migration_release.wait(5):
            raise RuntimeError('synthetic migration timed out')
        return {'projects': [], 'synthetic_only': True}

    def request(self, path, body=None, method='POST'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            headers = {'X-YingXu-Token': self.app.token, 'Content-Type': 'application/json'}
            connection.request(method, path, json.dumps(body or {}).encode() if method == 'POST' else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def fake_waiting_picker(self, args, **kwargs):
        self.assertEqual(kwargs['timeout'], 300)
        self.picker_entered.set()
        if not self.picker_release.wait(5):
            raise RuntimeError('synthetic picker timed out')
        return subprocess.CompletedProcess(args, 0, b'[]', b'')

    def assert_released(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.app.migration_jobs.writers == self.app.update_service.writers == 0 and not self.app.picker_lock.locked():
                return
            time.sleep(.01)
        self.fail('picker lock or writer reservation did not release')

    def test_pending_picker_allows_migration_but_preserves_busy_picker_and_active_gate(self):
        result = []
        with patch('server.subprocess.run', side_effect=self.fake_waiting_picker) as picker:
            thread = threading.Thread(target=lambda: result.append(self.request('/api/pick', {'kind': 'folder'})))
            self.request_threads.append(thread)
            thread.start()
            self.assertTrue(self.picker_entered.wait(2))
            self.assertTrue(self.app.picker_lock.locked())
            self.assertEqual(self.app.migration_jobs.writers, 0)
            duplicate = self.request('/api/pick', {'kind': 'folder'})
            self.assertEqual(duplicate[0], 409)
            self.assertIn('已有一个文件选择窗口', duplicate[1]['error'])
            status, preview = self.request('/api/project-storage/migration/preview', {'root': 'synthetic-target'})
            self.assertEqual(status, 200)
            accepted = self.request('/api/project-storage/migration', {'token': preview['token']})
            self.assertEqual(accepted[0], 202, accepted)
            self.assertTrue(self.migration_entered.wait(2))
            self.assertTrue(thread.is_alive())
            self.assertTrue(self.app.picker_lock.locked())
            blocked = self.request('/api/pick', {'kind': 'folder'})
            self.assertEqual(blocked[0], 409)
            self.assertIn('项目正在迁移', blocked[1]['error'])
            self.assertEqual(picker.call_count, 1)
            self.picker_release.set()
            thread.join(3)
            self.assertEqual(result, [(200, {'paths': []})])
        self.assert_released()
        self.migration_release.set()
        deadline = time.monotonic() + 3
        while self.app.migration_jobs.active and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.app.migration_jobs.get(accepted[1]['job_id'])['state'], 'done')
        self.assertEqual(self.executed, [{'token': 'synthetic-preview'}])

    def test_real_writer_rejects_migration_before_job_is_created(self):
        with self.app.migration_jobs.mutation('POST', '/api/upload'):
            with self.app.migration_jobs.mutation('POST', '/api/pick'):
                rejected = self.request('/api/project-storage/migration', {'token': 'synthetic-preview'})
                self.assertEqual(rejected[0], 409)
                self.assertIn('还有文件操作', rejected[1]['error'])
            self.assertEqual(self.app.migration_jobs.writers, 1)
        self.assert_released()
        self.assertEqual(self.app.migration_jobs.records, {})
        self.assertEqual(self.app.migration_jobs.tokens, {})
        self.assertEqual(self.executed, [])

    def test_cross_project_move_rejects_picker_before_subprocess(self):
        with self.app.migration_jobs.cross_project_move():
            with patch('server.subprocess.run') as picker:
                response = self.request('/api/pick', {'kind': 'folder'})
                self.assertEqual(response[0], 409)
                self.assertIn('跨项目移动', response[1]['error'])
                picker.assert_not_called()
        self.assert_released()

    def test_cancel_timeout_and_process_errors_release_lock_and_counts(self):
        outcomes = [
            ('cancel', subprocess.CompletedProcess([], 0, b'[]', b''), 200),
            ('timeout', subprocess.TimeoutExpired('synthetic-picker', 300), 400),
            ('nonzero', subprocess.CompletedProcess([], 1, b'', b'synthetic error'), 400),
            ('invalid_json', subprocess.CompletedProcess([], 0, b'not-json', b''), 400),
            ('spawn_error', FileNotFoundError('synthetic executable'), 409),
        ]
        for name, outcome, expected in outcomes:
            with self.subTest(case=name):
                kwargs = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
                with patch('server.subprocess.run', **kwargs), patch('server.traceback.print_exc'):
                    response = self.request('/api/pick', {'kind': 'folder'})
                self.assertEqual(response[0], expected, response)
                self.assert_released()
                self.assertEqual(self.app.migration_jobs.records, {})


if __name__ == '__main__':
    unittest.main()
