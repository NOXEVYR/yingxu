import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server import Application, Server


class UpdatesHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name).resolve()
        with patch('yingxu.skills.Path.home',return_value=root/'empty-home'):
            self.app=Application(root/'data',root/'projects')
        self.server=Server(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(5);self.app.close();self.temp.cleanup()

    def request(self,path,method='POST',body=None,headers=None):
        auth={'Origin':f'http://127.0.0.1:{self.server.server_port}','X-YingXu-Token':self.app.token,'Content-Type':'application/json'}
        auth.update(headers or {})
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.request(method,path,json.dumps(body or {}) if method=='POST' else None,auth)
            response=connection.getresponse();return response.status,json.loads(response.read())
        finally:connection.close()

    def test_auth_manual_only_and_no_project_mutation(self):
        with patch('yingxu.updates.check_update',return_value={'update_available':False}) as check,patch('yingxu.updates.open_release',return_value={'ok':True}) as launch:
            for path in ['/api/bootstrap','/api/settings','/api/health']:
                self.assertEqual(self.request(path,'GET')[0],200)
            check.assert_not_called();launch.assert_not_called()
            for path in ['/api/updates/check','/api/updates/open']:
                for headers in [{'Origin':'https://evil.invalid'},{'X-YingXu-Token':'wrong'}]:
                    self.assertEqual(self.request(path,headers=headers)[0],403)
            check.assert_not_called();launch.assert_not_called()
            self.assertEqual(self.request('/api/updates/check',body={'url':'https://evil.invalid'})[0],400)
            with patch.object(self.app.migration_jobs,'mutation',side_effect=AssertionError('Must not reserve a project writer')):
                self.assertEqual(self.request('/api/updates/check')[0],200)
                self.assertEqual(self.request('/api/updates/open',body={'tag':'yingxu-v0.4.11'})[0],200)
            check.assert_called_once();launch.assert_called_once_with('yingxu-v0.4.11')
