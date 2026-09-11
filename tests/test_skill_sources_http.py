"""Actual isolated HTTP contracts; never open the user's skill locations."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server import Application, Server


class SkillSourcesHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.external=self.root/'external'/'demo'/'SKILL.md';self.external.parent.mkdir(parents=True)
        self.external.write_bytes(b'---\nname: synthetic\n---\noriginal')
        with patch('yingxu.skills.Path.home',return_value=self.root/'empty-home'):
            self.app=Application(self.root/'data',self.root/'projects')
        self.server=Server(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
        self.app.jobs.pool.shutdown(wait=True);self.app.thumbnails.pool.shutdown(wait=True);self.app.context.close()
        self.assertEqual(self.external.read_bytes(),b'---\nname: synthetic\n---\noriginal')
        self.temp.cleanup()

    def request(self,path,method='GET',body=None,overrides=None):
        headers={'Origin':f'http://127.0.0.1:{self.server.server_port}',
                 'X-YingXu-Token':self.app.token,'Content-Type':'application/json'}
        for key,value in (overrides or {}).items():
            if value is None:headers.pop(key,None)
            else:headers[key]=value
        client=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        try:
            client.request(method,path,None if method=='GET' else json.dumps(body or {}),headers)
            response=client.getresponse();return response.status,json.loads(response.read())
        finally:client.close()

    def test_full_contract_and_original_preservation(self):
        status,initial=self.request('/api/skill-sources');self.assertEqual(status,200)
        self.assertEqual(initial['all_total'],0)
        self.assertTrue({'id','source','group','label','path','enabled','custom','removable','readonly','status','count'}<=set(initial['sources'][0]))
        status,added=self.request('/api/skill-sources','POST',{'path':str(self.external.parent.parent),'label':'HTTP 测试'})
        self.assertEqual(status,201,added)
        source=next(s for s in added['sources'] if s['custom']);sid=source['id']
        path='/api/skills?source=custom&source_id='+sid+'&q=synthetic'
        status,listing=self.request(path);self.assertEqual(status,200);self.assertEqual(listing['total'],1)
        iid=listing['skills'][0]['id']
        status,closed=self.request('/api/skill-sources/'+sid,'PATCH',{'enabled':False})
        self.assertEqual(status,200);self.assertEqual(closed['total'],0)
        status,enabled=self.request('/api/skill-sources/'+sid,'PATCH',{'enabled':True,'label':'已改名'})
        self.assertEqual(status,200);self.assertEqual(enabled['skills'][0]['id'],iid)
        status,removed=self.request('/api/skill-sources/'+sid,'DELETE')
        self.assertEqual(status,200);self.assertFalse(any(s['id']==sid for s in removed['sources']))

    def test_mutations_require_same_origin_and_token(self):
        for method,path,body in [('POST','/api/skill-sources',{'path':str(self.external.parent)}),
                                 ('PATCH','/api/skill-sources/dsh',{'enabled':False}),
                                 ('DELETE','/api/skill-sources/dsh',{}),
                                 ('POST','/api/skills/refresh',{})]:
            for overrides in ({'Origin':'https://foreign.invalid'},{'X-YingXu-Token':None},{'X-YingXu-Token':'wrong'}):
                with self.subTest(method=method,overrides=overrides):
                    self.assertEqual(self.request(path,method,body,overrides)[0],403)
        self.assertTrue(next(s for s in self.app.skills.source_list()['sources'] if s['id']=='dsh')['enabled'])
        self.assertFalse(any(s['custom'] for s in self.app.skills.source_list()['sources']))

    def test_extra_fields_unknown_ids_and_foreign_read_are_rejected(self):
        for path in ('/api/skills?path=private','/api/skill-sources?path=private','/api/skills?source=unknown'):
            self.assertEqual(self.request(path)[0],400)
        self.assertEqual(self.request('/api/skills?source_id=unknown')[0],404)
        self.assertEqual(self.request('/api/skill-sources/unknown','PATCH',{'enabled':False})[0],404)
        self.assertEqual(self.request('/api/skill-sources/dsh','PATCH',{'path':str(self.external.parent)})[0],400)
        self.assertEqual(self.request('/api/skill-sources','GET',overrides={'Origin':'https://foreign.invalid'})[0],403)
        self.assertEqual(self.request('/api/skill-sources','POST',{'path':str(self.external.parent),'execute':True})[0],400)
