import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from server import Application, Server


class ProjectFolderHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='yingxu-folder-http-')
        self.root=Path(self.tmp.name).resolve()
        with patch('yingxu.skills.Path.home',return_value=self.root/'empty-home'):
            self.app=Application(self.root/'data',self.root/'projects')
        self.server=Server(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.source=self.root/'source';self.source.mkdir()
        (self.source/'doc.md').write_text('hello',encoding='utf-8')

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.app.close();self.tmp.cleanup()

    def request(self,method,path,body=None,token=True):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=15)
        headers={'Content-Type':'application/json'}
        if token:headers['X-YingXu-Token']=self.app.token
        connection.request(method,path,json.dumps(body).encode() if body is not None else None,headers)
        response=connection.getresponse();result=response.status,json.loads(response.read());connection.close();return result

    def test_open_folder_auth_and_job_result(self):
        route='/api/project-library/open-folder';body={'path':str(self.source),'folder_id':None}
        self.assertEqual(self.request('POST',route,body,False)[0],403)
        status,result=self.request('POST',route,body);self.assertEqual(status,202,result)
        for _ in range(200):
            _,job=self.request('GET','/api/jobs/'+result['job_id'])
            if job['state'] in ('done','error'):break
            time.sleep(.02)
        self.assertEqual(job['state'],'done',job)
        _,projects=self.request('GET','/api/projects')
        self.assertEqual(len(projects['projects']),1)
        project=projects['projects'][0]
        self.assertEqual(job['project_id'],project['id'])
        self.assertNotEqual(project['root'],str(self.source))
        self.assertEqual(project['counts']['total'],1)
        self.assertEqual((self.source/'doc.md').read_text('utf-8'),'hello')

    def test_rejects_bad_payload_and_migration_in_progress(self):
        route='/api/project-library/open-folder'
        for body in ({},{'path':42},{'path':str(self.source),'mode':'reference'},{'path':str(self.source),'folder_id':'missing'}):
            self.assertIn(self.request('POST',route,body)[0],(400,404))
        self.app.migration_jobs.active='synthetic'
        try:self.assertEqual(self.request('POST',route,{'path':str(self.source)})[0],409)
        finally:self.app.migration_jobs.active=None
        self.assertEqual(self.app.store.list_projects(),[])
