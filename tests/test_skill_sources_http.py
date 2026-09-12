"""Actual isolated HTTP contracts; never open the user's skill locations."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server import Application, Server
from yingxu.skills import SkillLibrary


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

    def test_filter_labels_persist_without_changing_sources_files_or_bindings(self):
        status,added=self.request('/api/skill-sources','POST',{'path':str(self.external.parent.parent),'label':'合成来源'})
        self.assertEqual(status,201,added)
        sid=next(s['id'] for s in added['sources'] if s['custom'])
        iid=added['skills'][0]['id']
        status,project=self.request('/api/projects','POST',{'name':'标签测试'})
        self.assertEqual(status,201,project)
        self.assertEqual(self.request('/api/skills/bind','POST',{'project_id':project['id'],'skill_id':iid,'bound':True})[0],200)
        sources_before=self.request('/api/skill-sources')[1]['sources']

        # Label changes and filtered queries operate on the existing index only.
        with patch.object(self.app.skills,'refresh',side_effect=AssertionError('label operation must not scan')):
            status,created=self.request('/api/skill-source-labels','POST',{'label':'  创作能力  ','source_ids':[sid,sid,'yingxu']})
            self.assertEqual(status,201,created)
            label=next(g for g in created['groups'] if not g['builtin'])
            lid=label['id']
            self.assertEqual(label['label'],'创作能力')
            self.assertEqual(set(label['source_ids']),{sid,'yingxu'})
            self.assertEqual(len(label['source_ids']),2)
            self.assertEqual(label['count'],1)
            status,hidden=self.request('/api/skill-source-labels/custom','DELETE')
            self.assertEqual(status,200,hidden)
            self.assertTrue(next(g for g in hidden['groups'] if g['id']=='custom')['hidden'])
            status,filtered=self.request('/api/skills?source='+lid+'&project='+project['id'])
            self.assertEqual(status,200,filtered)
            self.assertEqual([(s['id'],s['bound']) for s in filtered['skills']],[(iid,True)])
            self.assertEqual(self.request('/api/skill-sources')[1]['sources'],sources_before)

        # Recreate the library over the same isolated database, as at app startup.
        with patch('yingxu.skills.Path.home',return_value=self.root/'empty-home'):
            self.app.skills=SkillLibrary(self.app.store)
        status,reloaded=self.request('/api/skill-sources')
        self.assertEqual(status,200,reloaded)
        self.assertEqual(next(g for g in reloaded['groups'] if g['id']==lid),label)
        self.assertTrue(next(g for g in reloaded['groups'] if g['id']=='custom')['hidden'])
        self.assertEqual(self.request('/api/skill-source-labels/custom','PATCH',{'hidden':False})[0],200)
        status,deleted=self.request('/api/skill-source-labels/'+lid,'DELETE')
        self.assertEqual(status,200,deleted)
        self.assertFalse(any(g['id']==lid for g in deleted['groups']))
        self.assertFalse(next(g for g in deleted['groups'] if g['id']=='custom')['hidden'])
        self.assertEqual(self.request('/api/skills?source='+lid)[0],400)
        status,listing=self.request('/api/skills?project='+project['id'])
        self.assertEqual(status,200,listing)
        self.assertEqual([(s['id'],s['bound']) for s in listing['skills']],[(iid,True)])
        self.assertTrue(next(s for s in deleted['sources'] if s['id']==sid)['enabled'])

    def test_filter_label_mutations_require_same_origin_and_token(self):
        status,created=self.request('/api/skill-source-labels','POST',{'label':'权限测试','source_ids':['yingxu']})
        self.assertEqual(status,201,created)
        lid=next(g['id'] for g in created['groups'] if not g['builtin'])
        self.assertEqual(self.request('/api/skill-source-labels/codex','DELETE')[0],200)
        before=self.request('/api/skill-sources')[1]
        operations=[('POST','/api/skill-source-labels',{'label':'不应创建','source_ids':['yingxu']}),
                    ('DELETE','/api/skill-source-labels/'+lid,{}),
                    ('DELETE','/api/skill-source-labels/yingxu',{}),
                    ('PATCH','/api/skill-source-labels/codex',{'hidden':False})]
        for method,path,body in operations:
            for overrides in ({'Origin':'https://foreign.invalid'},{'X-YingXu-Token':None},{'X-YingXu-Token':'wrong'}):
                with self.subTest(method=method,path=path,overrides=overrides):
                    status,result=self.request(path,method,body,overrides)
                    self.assertEqual(status,403,result)
        self.assertEqual(self.request('/api/skill-sources')[1],before)

    def test_filter_label_validation_and_unknown_operations_do_not_mutate(self):
        invalid=[{}, {'label':'','source_ids':['yingxu']}, {'label':'x'*41,'source_ids':['yingxu']},
                 {'label':'a\nb','source_ids':['yingxu']}, {'label':'x','source_ids':[]},
                 {'label':'x','source_ids':'yingxu'}, {'label':'x','source_ids':['missing']},
                 {'label':'x','source_ids':[None]}, {'label':'x','source_ids':['yingxu'],'path':str(self.root)}]
        before=self.request('/api/skill-sources')[1]
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.request('/api/skill-source-labels','POST',body)[0],400)
        self.assertEqual(self.request('/api/skill-source-labels','POST',{'label':'cOdEx','source_ids':['yingxu']})[0],409)
        for method,path,body,expected in [
            ('DELETE','/api/skill-source-labels/label_missing',{},404),
            ('PATCH','/api/skill-source-labels/label_missing',{'hidden':False},404),
            ('PATCH','/api/skill-source-labels/codex',{'hidden':True},400),
            ('PATCH','/api/skill-source-labels/codex',{'hidden':0},400),
            ('PATCH','/api/skill-source-labels/codex',{'hidden':False,'extra':True},400),
            ('DELETE','/api/skill-source-labels/codex',{'extra':True},400),
            ('POST','/api/skill-source-labels?extra=1',{'label':'不应创建','source_ids':['yingxu']},400),
            ('DELETE','/api/skill-source-labels/codex?extra=1',{},400),
            ('PATCH','/api/skill-source-labels/codex?extra=1',{'hidden':False},400)]:
            with self.subTest(method=method,path=path,body=body):
                self.assertEqual(self.request(path,method,body)[0],expected)
        self.assertEqual(self.request('/api/skill-sources')[1],before)
