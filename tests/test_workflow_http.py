"""Synthetic HTTP journey for collection -> pin -> full/delta handoff."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server import Application, Server


class WorkflowHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        with patch('yingxu.skills.Path.home', return_value=self.root/'empty-home'):
            self.app = Application(self.root/'data', self.root/'projects')
        self.app._skills_startup.result(timeout=10)
        self.server = Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.app.close()
        self.temp.cleanup()

    def request(self, path, body=None, token=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        headers = {'Origin':f'http://127.0.0.1:{self.server.server_port}', 'Content-Type':'application/json'}
        if token:
            headers['X-YingXu-Token'] = self.app.token
        try:
            connection.request('GET' if body is None else 'POST', path, None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_collect_pin_handoff_acknowledge_and_delta(self):
        status, project = self.request('/api/projects', {'name':'Synthetic workflow'})
        self.assertEqual(status, 201, project)
        status, skill = self.request('/api/skills', {'name':'Test method','description':'synthetic','content':'# Test\n\nA method.'})
        self.assertEqual(status, 201, skill)
        status, preview = self.request('/api/skill-collections/preview', {'skill_id':skill['id']})
        self.assertEqual(status, 200, preview)
        status, collected = self.request('/api/skill-collections/collect', {'token':preview['token']})
        self.assertEqual(status, 201, collected)
        status, binding = self.request('/api/skill-collections/bind', {'id':collected['id'],'project_id':project['id'],'bound':True})
        self.assertEqual(status, 200, binding)
        status, listing = self.request('/api/skill-collections?project='+project['id'])
        self.assertTrue(listing['collections'][0]['pinned'])
        payload = dict(project_id=project['id'],client_id='test-client',conversation_id='scene-one',task='First task',asset_ids=[])
        status, first = self.request('/api/handoffs', payload)
        self.assertEqual(status, 201, first)
        self.assertEqual(first['mode'], 'full')
        status, second = self.request('/api/handoffs', payload | {'task':'Not sent yet'})
        self.assertEqual(second['mode'], 'full')
        status, ack = self.request('/api/handoffs/acknowledge', {'snapshot_id':first['snapshot_id']})
        self.assertEqual(status, 200, ack)
        status, third = self.request('/api/handoffs', payload | {'task':'Continue task'})
        self.assertEqual(status, 201, third)
        self.assertEqual(third['mode'], 'delta')
        self.assertEqual(third['base_snapshot_id'], first['snapshot_id'])
        status, exported = self.request('/api/skill-collections/export', {'project_id':project['id']})
        self.assertEqual(status, 200, exported)
        self.assertTrue(Path(exported['path']).is_relative_to(self.root))

    def test_new_mutations_and_update_status_require_token(self):
        for path, payload in [('/api/skill-collections/preview',{'skill_id':'unknown'}),
                              ('/api/handoffs',{}),('/api/handoffs/acknowledge',{}),
                              ('/api/updates/automatic/start',{})]:
            self.assertEqual(self.request(path,payload,token=False)[0],403,path)
        self.assertEqual(self.request('/api/updates/automatic/status',token=False)[0],403)
        with patch.object(self.app.update_service,'start_automatic_updates') as start:
            self.assertEqual(self.request('/api/updates/automatic/start',{})[0],200)
            start.assert_called_once()
        self.assertEqual(self.request('/api/skill-collections/classify',{'path':'private'})[0],400)


if __name__ == '__main__':
    unittest.main()
