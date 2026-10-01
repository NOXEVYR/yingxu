"""Synthetic authenticated folder/metadata journey; never touches user data."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server import Application, Server


class SkillOrganizationHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root/'home'
        source = self.home/'.codex/skills/synthetic/SKILL.md'
        source.parent.mkdir(parents=True)
        source.write_text('---\nname: synthetic-external\n---\n# External method\n', encoding='utf-8')
        self.source = source
        self.original = source.read_bytes()
        with patch('yingxu.skills.Path.home', return_value=self.home):
            self.app = Application(self.root/'data', self.root/'projects')
        self.app._skills_startup.result(timeout=10)
        self.server = Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.skill = self.request('/api/skills')[1]['skills'][0]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.app.close()
        self.temporary.cleanup()

    def request(self, path, body=None, method=None, token=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        headers = {'Origin':f'http://127.0.0.1:{self.server.server_port}', 'Content-Type':'application/json'}
        if token:
            headers['X-YingXu-Token'] = self.app.token
        try:
            connection.request(method or ('GET' if body is None else 'POST'), path,
                               None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_uncollected_skill_folder_tags_notes_and_restore_after_scan(self):
        status, parent = self.request('/api/skill-folders', {'name':'开发工具'})
        self.assertEqual(status, 201, parent)
        status, child = self.request('/api/skill-folders', {'name':'项目管理', 'parent_id':parent['id']})
        self.assertEqual(status, 201, child)
        self.assertEqual(child['path'], '开发工具/项目管理')
        payload = {'skill_ids':[self.skill['id']], 'folder_id':child['id'],
                   'tags':['工具', '常用'], 'notes':'每个项目先确认适用范围。'}
        status, saved = self.request('/api/skill-metadata', payload)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved['metadata'][0]['notes'], payload['notes'])
        self.assertEqual(self.request('/api/skill-folders/'+child['id'], {}, 'DELETE')[0], 409)
        status, rename = self.request('/api/skill-folders/'+parent['id'], {'name':'我的工具'}, 'PATCH')
        self.assertEqual(status, 200, rename)
        with patch.object(self.app.skills, 'refresh', side_effect=AssertionError('Query must not scan')):
            listing = self.request('/api/skill-organization')[1]
        self.assertEqual(listing['folders'][1]['path'], '我的工具/项目管理')
        self.request('/api/skills/refresh', {})
        self.assertEqual(self.request('/api/skill-organization')[1]['metadata'], listing['metadata'])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_batch_error_is_atomic_and_append_preserves_distinct_notes(self):
        first = self.skill['id']
        second = self.request('/api/skills', {'name':'Local method', 'description':'Test', 'content':'# Test'})[1]['id']
        self.request('/api/skill-metadata', {'skill_ids':[first], 'tags':['A'], 'notes':'First'})
        self.request('/api/skill-metadata', {'skill_ids':[second], 'tags':['B'], 'notes':'Second'})
        status, saved = self.request('/api/skill-metadata', {'skill_ids':[first,second], 'tags':['C']})
        self.assertEqual(status, 200, saved)
        by_id = {x['skill_id']:x for x in saved['metadata']}
        self.assertEqual(by_id[first]['tags'], ['A','C'])
        self.assertEqual(by_id[second]['notes'], 'Second')
        before = self.request('/api/skill-organization')[1]['metadata']
        self.assertEqual(self.request('/api/skill-metadata', {'skill_ids':[first,second], 'notes':'Overwrite'})[0], 400)
        self.assertEqual(self.request('/api/skill-metadata', {'skill_ids':[first,'unknown'], 'tags':['D']})[0], 404)
        self.assertEqual(self.request('/api/skill-organization')[1]['metadata'], before)

    def test_folder_move_rejects_cycle_and_empty_delete_preserves_source(self):
        parent = self.request('/api/skill-folders', {'name':'Parent'})[1]
        child = self.request('/api/skill-folders', {'name':'Child', 'parent_id':parent['id']})[1]
        self.assertEqual(self.request('/api/skill-folders/'+parent['id'], {'parent_id':child['id']}, 'PATCH')[0], 400)
        self.assertEqual(self.request('/api/skill-folders/'+parent['id'], {}, 'DELETE')[0], 409)
        self.assertEqual(self.request('/api/skill-folders/'+child['id'], {}, 'DELETE')[0], 200)
        self.assertEqual(self.request('/api/skill-folders/'+parent['id'], {}, 'DELETE')[0], 200)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_mutations_require_token_and_allow_only_known_fields(self):
        folder = self.request('/api/skill-folders', {'name':'Test'})[1]
        for path, body, method in [('/api/skill-folders', {'name':'Denied'}, 'POST'),
                                  ('/api/skill-folders/'+folder['id'], {'name':'Denied'}, 'PATCH'),
                                  ('/api/skill-folders/'+folder['id'], {}, 'DELETE'),
                                  ('/api/skill-metadata', {'skill_ids':[self.skill['id']], 'tags':[]}, 'POST')]:
            self.assertEqual(self.request(path, body, method, token=False)[0], 403, path)
        for path, body, method in [('/api/skill-folders', {'name':'Denied','path':'outside'}, 'POST'),
                                  ('/api/skill-folders/'+folder['id'], {'path':'outside'}, 'PATCH'),
                                  ('/api/skill-metadata', {'skill_ids':[self.skill['id']], 'content':'write source'}, 'POST')]:
            self.assertEqual(self.request(path, body, method)[0], 400, path)
        self.assertEqual(self.request('/api/skill-organization?path=outside')[0], 400)


if __name__ == '__main__':
    unittest.main()
