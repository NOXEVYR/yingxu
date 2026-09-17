"""Real HTTP contracts exercised with temporary synthetic application data."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from server import Application, Server


class CrossProjectHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        with patch('yingxu.skills.Path.home', return_value=self.root/'empty-home'):
            self.app = Application(self.root/'data', self.root/'projects')
        self.server = Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.source = self.app.store.create_project('source'); self.target = self.app.store.create_project('target')
        self.item = self.app.store.create_item({'project_id': self.source['id'], 'name': 'document', 'content': 'untouched'})

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.app.close(); self.tmp.cleanup()

    def request(self, method, path, body=None, token=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        headers = {'Content-Type': 'application/json'}
        if token:headers['X-YingXu-Token'] = self.app.token
        connection.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
        response = connection.getresponse(); result = response.status, json.loads(response.read()); connection.close(); return result

    def body(self):
        return {'ids': [self.item['id']], 'target_project_id': self.target['id'], 'category': 'references', 'folder_id': None}

    def test_authorized_move_retry_and_media_access(self):
        self.assertEqual(self.request('POST', '/api/move', self.body(), token=False)[0], 403)
        status, result = self.request('POST', '/api/move', self.body())
        self.assertEqual(status, 200, result)
        self.assertEqual(result['source_project_id'], self.source['id'])
        self.assertEqual(result['project_id'], self.target['id'])
        self.assertEqual(self.app.store.read_content(self.item['id'])['content'], 'untouched')
        status, retry = self.request('POST', '/api/move', self.body())
        self.assertEqual(status, 200, retry)
        self.assertEqual(retry['stats']['unchanged'], 1)
        self.assertEqual(self.app.store.list_items(self.target['id'])['total'], 1)

    def test_old_contract_and_same_project_work(self):
        body = {'ids': [self.item['id']], 'category': 'characters'}
        self.assertEqual(self.request('POST', '/api/move', body)[0], 200)
        body.update(target_project_id=self.source['id'], category='references')
        self.assertEqual(self.request('POST', '/api/move', body)[0], 200)
        self.assertEqual(self.app.store.get_item(self.item['id'])['project_id'], self.source['id'])

    def test_move_rejects_running_import_and_migration(self):
        with self.app.jobs.lock:self.app.jobs.jobs['synthetic'] = {'state': 'running'}
        self.assertEqual(self.request('POST', '/api/move', self.body())[0], 409)
        with self.app.jobs.lock:self.app.jobs.jobs.clear()
        self.app.migration_jobs.active = 'synthetic'
        try:self.assertEqual(self.request('POST', '/api/move', self.body())[0], 409)
        finally:self.app.migration_jobs.active = None
        self.assertTrue(Path(self.item['path']).exists())

    def test_reserved_move_blocks_writes_but_not_reads(self):
        with self.app.migration_jobs.cross_project_move():
            self.assertEqual(self.request('POST', '/api/projects', {'name': 'blocked'})[0], 409)
            self.assertEqual(self.request('GET', '/api/project-storage')[0], 200)
        self.assertEqual(self.request('POST', '/api/move', self.body())[0], 200)

    def test_post_commit_context_failure_is_warning_not_false_failure(self):
        with patch.object(self.app, 'changed', side_effect=RuntimeError('synthetic notification failure')):
            status, result = self.request('POST', '/api/move', self.body())
        self.assertEqual(status, 200, result)
        self.assertTrue(result['warnings'])
        self.assertEqual(self.app.store.get_item(self.item['id'])['project_id'], self.target['id'])


if __name__ == '__main__':unittest.main()
