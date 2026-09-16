import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server import Application, Server


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


if __name__ == '__main__':
    unittest.main()
