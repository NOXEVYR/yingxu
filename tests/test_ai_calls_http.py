"""Actual HTTP workbench authorization and durable calls using synthetic services."""
import json
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time
import unittest
from unittest.mock import patch

from test_ai_tasks_http import AITaskHttpTests as _TaskFixture
from yingxu.ai_call_jobs import AICallService
from server import Handler


class ProviderFixture:
    def __init__(self):
        self.submissions=0
        self.lose_response=False
        self.jobs={}
        self.results=None
        owner=self
        def capabilities():
            return {'protocol':'yingxu-http-v1','service_id':'fixture-http','revision':'1','execution_binding':'declaration_sha256',
                'operations':[{'id':'make','name':'合成文本','input_schema':{'type':'object',
                    'properties':{'prompt':{'type':'string','maxLength':200}},'required':['prompt'],
                    'additionalProperties':False},'supports':{'query':True,'lookup':True,'cancel':False,'idempotency':True}}]}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send(self,value,status=200):
                raw=json.dumps(value,ensure_ascii=False).encode('utf-8')
                self.send_response(status);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                if self.path=='/capabilities':
                    return self.send(capabilities())
                if self.path.startswith('/requests/'):
                    value=owner.jobs.get(self.path.rsplit('/',1)[-1])
                    return self.send(value or {'error':'not found'},200 if value else 404)
                if self.path.startswith('/jobs/'):
                    value=next((v for v in owner.jobs.values() if v['job_id']==self.path.rsplit('/',1)[-1]),None)
                    return self.send(value or {'error':'not found'},200 if value else 404)
                self.send({'error':'not found'},404)
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                if self.path!='/jobs':return self.send({'error':'not found'},404)
                digest=hashlib.sha256(json.dumps(capabilities(),ensure_ascii=False).encode('utf-8')).hexdigest()
                if body.get('expected_declaration_sha256')!=digest:return self.send({},409)
                key=body['request_id']
                if key not in owner.jobs:
                    owner.submissions+=1
                    owner.jobs[key]={'service_id':'fixture-http','request_id':key,'job_id':'job-'+str(owner.submissions),
                        'state':'succeeded','results':owner.results if owner.results is not None else [{'name':'合成说明.txt','kind':'text','description':'仅结果描述'}]}
                    owner.jobs[key]['declaration_sha256']=digest
                if owner.lose_response:
                    self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                self.send(owner.jobs[key],202)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3)


class AICallHTTPTests(unittest.TestCase):
    _base_setup=_TaskFixture.setUp
    _base_teardown=_TaskFixture.tearDown
    request=_TaskFixture.request
    ok=_TaskFixture.ok
    task=_TaskFixture.task
    freeze=_TaskFixture.freeze

    def setUp(self):
        self._log_patch=patch.object(Handler,'log_message',lambda *_args:None)
        self._log_patch.start()
        self.addCleanup(self._log_patch.stop)
        self._base_setup()
        self.provider=ProviderFixture()
    def tearDown(self):
        try:self._base_teardown()
        finally:self.provider.close()
    def prepared(self):
        task=self.task();run=self.freeze(task)
        connection=self.ok('/api/ai-connections',{'name':'合成工具','base_url':self.provider.url,
            'provider':'yingxu-http-v1','credential_env':'','enabled':True,'expected_revision':0})
        self.ok('/api/ai-connections/'+connection['id']+'/check',{})
        prefix='/api/ai-tasks/'+task['id']+'/runs/'+run['id']+'/calls'
        body={'connection_id':connection['id'],'operation_id':'make','parameters':{'prompt':'不使用真实素材'},'idempotency_key':'http-test-request'}
        return task,run,connection,prefix,body
    def wait(self,prefix,attempt,states):
        until=time.monotonic()+5
        while time.monotonic()<until:
            value=self.ok(prefix+'/'+attempt['id'])
            if value['state'] in states:return value
            time.sleep(.05)
        self.fail('Synthetic call did not reach expected state: '+str({k:value.get(k) for k in ('state','error_code','notice')}))

    def test_connections_and_status_reads_require_workbench_session(self):
        for path in ('/api/ai-connections','/api/ai-calls/status'):
            self.assertEqual(self.request(path,authorized=False)[0],403)
            self.assertEqual(self.request(path)[0],200)
        self.assertEqual(self.request('/api/ai-connections?unexpected=1')[0],400)
        self.assertEqual(self.provider.submissions,0)

    def test_short_acceptance_same_key_and_results_do_not_auto_receive(self):
        task,run,connection,prefix,body=self.prepared()
        before=self.ok('/api/ai-tasks/'+task['id'])['status']
        status,first=self.request(prefix,body);self.assertEqual(status,202,first)
        status,again=self.request(prefix,body);self.assertEqual(status,202,again);self.assertEqual(first['id'],again['id'])
        result=self.wait(prefix,first,{'succeeded'})
        self.assertEqual(self.provider.submissions,1)
        self.assertEqual(result['parameters'],body['parameters'])
        self.assertEqual(result['results'][0]['name'],'合成说明.txt')
        self.assertEqual(self.ok('/api/ai-tasks/'+task['id'])['status'],before)
        self.assertEqual(self.ok('/api/ai-tasks/'+task['id']+'/runs/'+run['id'])['artifacts'],[])
        self.assertEqual(self.request(prefix,{**body,'parameters':{'prompt':'different'}})[0],409)

    def test_lost_provider_response_is_reconciled_without_resubmission_after_restart(self):
        task,run,connection,prefix,body=self.prepared();self.provider.lose_response=True
        status,attempt=self.request(prefix,body);self.assertEqual(status,202)
        self.wait(prefix,attempt,{'unknown'});self.assertEqual(self.provider.submissions,1)
        self.app.ai_calls.close(timeout=3)
        self.app.ai_calls=AICallService(self.app.store,self.app.ai_tasks,self.app.migration_jobs,self.app.ai_connections)
        self.assertEqual(self.ok(prefix+'/'+attempt['id'])['state'],'unknown')
        status,again=self.request(prefix,body);self.assertEqual(status,202);self.assertEqual(again['id'],attempt['id'])
        self.assertEqual(self.provider.submissions,1)
        self.assertEqual(self.request(prefix+'/'+attempt['id']+'/query',{})[0],202)
        self.wait(prefix,attempt,{'succeeded'});self.assertEqual(self.provider.submissions,1)

    def test_mcp_bearer_cannot_write_or_inspect_call_connections(self):
        task,run,connection,prefix,body=self.prepared()
        self.ok('/api/mcp/configure',{'enabled':True,'project_id':self.project['id']})
        bearer=self.ok('/api/mcp/connection',{})['config']['mcpServers']['yingxu']['headers']['Authorization']
        self.assertEqual(self.request(prefix,body,authorized=False,bearer=bearer)[0],403)
        self.assertEqual(self.request('/api/ai-connections',authorized=False,bearer=bearer)[0],403)
        self.assertEqual(self.provider.submissions,0)

    def test_migration_rejects_call_creation_and_configuration_without_touching_provider(self):
        task,run,connection,prefix,body=self.prepared()
        self.app.migration_jobs.active='synthetic-migration'
        try:
            self.assertEqual(self.request(prefix,body)[0],409)
            self.assertEqual(self.request('/api/ai-connections/'+connection['id']+'/check',{})[0],409)
        finally:self.app.migration_jobs.active=None
        self.assertEqual(self.provider.submissions,0)
        self.assertEqual(self.ok(prefix)['items'],[])

    def test_call_identity_wrong_run_and_unsupported_cancel_are_rejected(self):
        task,run,connection,prefix,body=self.prepared();status,attempt=self.request(prefix,body);self.assertEqual(status,202)
        self.wait(prefix,attempt,{'succeeded'})
        other=self.task(self.other)
        wrong='/api/ai-tasks/'+other['id']+'/runs/'+run['id']+'/calls/'+attempt['id']
        self.assertIn(self.request(wrong)[0],(403,404))
        self.assertIn(self.request(prefix+'/'+attempt['id']+'/cancel',{})[0],(400,409))
        self.assertEqual(self.provider.submissions,1)

    def test_control_actions_require_an_empty_object(self):
        task,run,connection,prefix,body=self.prepared()
        status,attempt=self.request(prefix,body);self.assertEqual(status,202)
        self.wait(prefix,attempt,{'succeeded'})
        for path in ('/api/ai-connections/'+connection['id']+'/check',prefix+'/'+attempt['id']+'/query'):
            for invalid in ([],False,0,''):
                self.assertEqual(self.request(path,invalid)[0],400)
        self.assertEqual(self.provider.submissions,1)

    def test_bounded_call_list_pagination_and_invalid_parameters(self):
        task,run,connection,prefix,body=self.prepared()
        for n in range(3):
            status,attempt=self.request(prefix,{**body,'idempotency_key':'page-'+str(n)})
            self.assertEqual(status,202);self.wait(prefix,attempt,{'succeeded'})
        first=self.ok(prefix+'?limit=2');second=self.ok(prefix+'?limit=2&offset=2')
        self.assertEqual(first['total'],3);self.assertEqual(len(first['items']),2);self.assertEqual(len(second['items']),1)
        self.assertEqual(len({v['id'] for v in first['items']+second['items']}),3)
        for invalid in ('limit=49','limit=oops','offset=-1','unknown=1'):
            self.assertEqual(self.request(prefix+'?'+invalid)[0],400)


del _TaskFixture
