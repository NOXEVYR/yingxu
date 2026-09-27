"""Cross-feature acceptance journey using an isolated HTTP server and synthetic files."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from server import Application, Server


class StabilityJourneyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yingxu-stability-')
        self.root = Path(self.temp.name).resolve()
        self.start()

    def start(self):
        with patch('yingxu.skills.Path.home', return_value=self.root/'empty-home'):
            self.app = Application(self.root/'data', self.root/'projects')
        self.server = Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.app.close()
        self.assertFalse(self.thread.is_alive())

    def tearDown(self):
        self.stop()
        self.temp.cleanup()

    def request(self, method, path, body=None, status=200):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        headers = {'X-YingXu-Token': self.app.token, 'Content-Type': 'application/json'}
        try:
            connection.request(method, path, json.dumps(body).encode('utf-8') if body is not None else None, headers)
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, status, result)
            return result
        finally:
            connection.close()

    def test_edit_rename_batch_move_restore_migrate_restart_preserves_document(self):
        source = self.request('POST', '/api/projects', {'name': '合成第一集'}, 201)
        target = self.request('POST', '/api/projects', {'name': '合成第二集'}, 201)
        item = self.request('POST', '/api/items', {'project_id': source['id'], 'name': '初稿',
                            'content': '原文', 'tags': ['独有标签']}, 201)
        iid = item['id']
        original = b'\xef\xbb\xbf' + '# 原始正文\r\n雨夜车站\r\n'.encode('utf-8')
        Path(item['path']).write_bytes(original)
        opened = self.request('GET', '/api/content/'+iid)
        renamed = self.request('POST', '/api/rename', {'id': iid, 'name': '修订文稿'})
        self.assertEqual(Path(renamed['path']).read_bytes(), original)
        draft = '# 修订正文\r\n雨夜车站，角色入场。\r\n'
        saved = self.request('PUT', '/api/content/'+iid, {'etag': opened['etag'], 'content': draft})
        expected = b'\xef\xbb\xbf' + draft.encode('utf-8')
        self.request('POST', '/api/items/batch-properties', {'project_id': source['id'],
                     'ids': [iid], 'tags_add': ['验收', '独有标签'], 'status': '已完成'})
        self.request('POST', '/api/move', {'ids': [iid], 'target_project_id': target['id'],
                     'category': 'references', 'folder_id': None})
        trashed = self.request('POST', '/api/trash/items', {'ids': [iid]})
        self.request('POST', '/api/trash/'+trashed['id']+'/restore', {})
        current = self.app.store.get_item(iid)
        self.assertEqual(current['project_id'], target['id'])
        self.assertEqual(current['status'], '已完成')
        self.assertEqual(set(current['tags']), {'独有标签', '验收'})
        self.assertEqual(Path(current['path']).read_bytes(), expected)
        self.request('PUT', '/api/content/'+iid, {'etag': opened['etag'], 'content': '过期草稿不可覆盖'}, 409)
        self.assertEqual(self.request('GET', '/api/content/'+iid)['etag'], saved['etag'])
        matches = self.request('GET', '/api/items?'+urlencode({'project': target['id'], 'q': '雨夜车站'}))
        self.assertEqual([entry['id'] for entry in matches['items']], [iid])
        old_path = Path(current['path'])
        destination = self.root/'迁移目的地'
        destination.mkdir()
        preview = self.request('POST', '/api/project-storage/migration/preview',
                               {'root': str(destination), 'project_ids': [source['id'], target['id']]})
        accepted = self.request('POST', '/api/project-storage/migration', {'token': preview['token']}, 202)
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            job = self.request('GET', '/api/project-storage/migration/jobs/'+accepted['job_id'])
            if job['state'] != 'running':
                break
            time.sleep(.02)
        self.assertEqual(job['state'], 'done', job)
        self.assertEqual(old_path.read_bytes(), expected)
        self.stop()
        self.start()
        self.assertEqual(self.app.store.project_root, destination)
        current = self.app.store.get_item(iid)
        self.assertTrue(Path(current['path']).is_relative_to(destination))
        self.assertEqual(Path(current['path']).read_bytes(), expected)
        self.assertEqual(self.request('GET', '/api/content/'+iid)['content'], draft)
        with self.app.store.connection() as db:
            versions = db.execute('SELECT path FROM versions WHERE item_id=?', (iid,)).fetchall()
        self.assertTrue(any(Path(row['path']).read_bytes() == original for row in versions))


if __name__ == '__main__':
    unittest.main()
