"""Synthetic task/MCP journeys; no AI execution or real media playback."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server import Application, Server


class AITaskHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='yingxu-ai-http-')
        self.root=Path(self.tmp.name).resolve()
        self.home=self.root/'home'
        self.source=self.home/'.codex/skills/example/SKILL.md'
        self.source.parent.mkdir(parents=True)
        self.source.write_text('---\nname: example\n---\n\nPinned A\n',encoding='utf-8')
        with patch('yingxu.skills.Path.home',return_value=self.home):
            self.app=Application(self.root/'data',self.root/'projects')
        self.app._skills_startup.result(timeout=15)
        self.project=self.app.store.create_project('合成视频项目')
        self.other=self.app.store.create_project('隔离项目')
        self.server=Server(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(10)
        self.app.close();self.tmp.cleanup()

    def request(self,path,body=None,method=None,authorized=True,bearer=None):
        headers={'Content-Type':'application/json','Origin':self.base,'Accept':'application/json, text/event-stream'}
        if authorized:headers['X-YingXu-Token']=self.app.token
        if bearer:headers.update(Authorization=bearer,**{'MCP-Protocol-Version':'2025-11-25'})
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=30)
        try:
            conn.request(method or ('GET' if body is None else 'POST'),path,None if body is None else json.dumps(body).encode(),headers)
            response=conn.getresponse();raw=response.read()
            return response.status,json.loads(raw) if raw else None
        finally:conn.close()

    def ok(self,path,body=None,method=None):
        status,result=self.request(path,body,method)
        self.assertIn(status,(200,201),result)
        return result

    def task(self,project=None,title='制作候选视频'):
        return self.ok('/api/ai-tasks',{'project_id':(project or self.project)['id'],'title':title,'kind':'video','goal':'输出合成候选文件','acceptance':['用户预览后决定采用']})

    def freeze(self,task,pins=None,goal='本轮制作'):
        task=self.ok('/api/ai-tasks/'+task['id'])
        return self.ok('/api/ai-tasks/'+task['id']+'/runs',{'expected_revision':task['revision'],'goal':goal,'client_id':'codex','conversation_id':'same-chat','input_item_ids':[],'skill_pins':pins or []})

    def prefix(self,task,run):return '/api/ai-tasks/'+task['id']+'/runs/'+run['id']

    def handoff(self,task,run,conversation='same-chat'):
        return self.ok(self.prefix(task,run)+'/handoff',{'client_id':'codex','conversation_id':conversation})

    def tool(self,bearer,name,args):
        return self.request('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}},authorized=False,bearer=bearer)

    def test_ui_read_requires_session_and_writes_respect_migration(self):
        task=self.task()
        self.assertEqual(self.request('/api/ai-tasks?project_id='+self.project['id'],authorized=False)[0],403)
        self.assertEqual(self.ok('/api/ai-tasks?project_id='+self.project['id'])['total'],1)
        self.assertEqual(self.request('/api/ai-tasks?project_id='+self.project['id']+'&limit=oops')[0],400)
        self.app.migration_jobs.active='synthetic-reservation'
        try:self.assertEqual(self.request('/api/ai-tasks',{'project_id':self.project['id'],'title':'blocked'})[0],409)
        finally:self.app.migration_jobs.active=None
        self.assertEqual(self.ok('/api/ai-tasks/'+task['id'])['status'],'待开始')

    def test_create_retry_keeps_one_task_and_rejects_changed_payload(self):
        body={'project_id':self.project['id'],'title':'断连重试','kind':'video','idempotency_key':'persisted-create-request'}
        first=self.ok('/api/ai-tasks',body)
        self.assertEqual(self.ok('/api/ai-tasks',body)['id'],first['id'])
        self.assertEqual(self.ok('/api/ai-tasks?project_id='+self.project['id'])['total'],1)
        self.assertEqual(self.request('/api/ai-tasks',{**body,'title':'不同内容'})[0],409)

    def test_mcp_long_task_pages_stay_bounded_and_do_not_omit_artifacts(self):
        task=self.task();run=self.freeze(task)
        self.ok('/api/mcp/configure',{'enabled':True,'project_id':self.project['id']})
        bearer=self.ok('/api/mcp/connection',{})['config']['mcpServers']['yingxu']['headers']['Authorization']
        bounded={**run,'artifact_total':200,'input_snapshot':{
            'goal':'目标'*50000,'acceptance':['验收'*500 for _ in range(100)],
            'inputs':[{'item_id':f'{n:032x}','name':'中文输入'*40,'kind':'video','sha256':'a'*64,'verification':'sha256'} for n in range(200)],
            'skill_pins':[{'collection_id':'col_'+f'{n:032x}','version':'b'*64,'name':'中文技能'*40} for n in range(32)]},
            'artifacts':[{'id':f'{n:032x}','item_id':f'{n+200:032x}','role':'视频角色'*20,'sha256':'c'*64,'review_status':'accepted','review_notes':'中文审核'*1000} for n in range(200)]}
        offset=0;seen=[]
        with patch.object(self.app.ai_tasks,'get_run',return_value=bounded):
            for _ in range(201):
                status,response=self.tool(bearer,'read_collaboration_task',{'task_id':task['id'],'run_id':run['id'],'offset':offset,'limit':48})
                self.assertEqual(status,200,response)
                self.assertFalse(response['result']['isError'])
                self.assertLessEqual(len(json.dumps(response,ensure_ascii=False).encode()),256*1024)
                page=response['result']['structuredContent']
                self.assertTrue(page['text_truncated'])
                self.assertEqual(len(page['inputs']),200)
                self.assertEqual(len(page['skills']),32)
                self.assertGreater(len(page['artifacts']),0)
                seen.extend(row['artifact_id'] for row in page['artifacts'])
                if 'next_offset' not in page:break
                self.assertEqual(page['next_offset'],offset+len(page['artifacts']))
                offset=page['next_offset']
            else:self.fail('Artifact pagination did not finish')
        self.assertEqual(seen,[f'{n:032x}' for n in range(200)])

    def test_round_receipt_review_and_complete_are_actual_item_operations(self):
        task=self.task();run=self.freeze(task);prefix=self.prefix(task,run)
        folder=Path(run['directories']['generated']['path'])
        media=folder/'candidate.mp4';media.write_bytes(b'synthetic-media-range-only')
        preview=self.ok(prefix+'/candidates');self.assertEqual(preview['files'][0]['relative_path'],'candidate.mp4')
        plan={'idempotency_key':'same-receipt','files':[{'relative_path':'candidate.mp4','role':'candidate'}]}
        result=self.ok(prefix+'/receive',plan);self.assertEqual(result['state'],'completed')
        self.assertEqual(self.ok(prefix+'/receive',plan)['id'],result['id'])
        entry=result['results'][0];item=self.app.store.get_item(entry['item_id'])
        self.assertEqual(Path(item['path']),media)
        self.assertEqual(self.ok('/api/ai-tasks/'+task['id']+'/receipts/'+result['id'])['id'],result['id'])
        artifact=self.ok('/api/ai-tasks/'+task['id'])['artifacts'][0]
        self.ok('/api/ai-tasks/'+task['id']+'/artifacts/'+artifact['id']+'/review',{'expected_revision':artifact['revision'],'decision':'accepted','notes':'合成验收记录'},'PATCH')
        latest=self.ok('/api/ai-tasks/'+task['id'])
        done=self.ok('/api/ai-tasks/'+task['id']+'/complete',{'expected_revision':latest['revision'],'confirmed':True})
        self.assertEqual(done['status'],'已完成')

    def test_changed_file_cannot_reuse_review(self):
        task=self.task();run=self.freeze(task);prefix=self.prefix(task,run)
        file=Path(run['directories']['generated']['path'])/'report.txt';file.write_text('first',encoding='utf-8')
        self.ok(prefix+'/receive',{'idempotency_key':'r','files':[{'relative_path':'report.txt'}]})
        artifact=self.ok('/api/ai-tasks/'+task['id'])['artifacts'][0]
        file.write_text('changed',encoding='utf-8')
        self.assertEqual(self.request('/api/ai-tasks/'+task['id']+'/artifacts/'+artifact['id']+'/review',{'expected_revision':artifact['revision'],'decision':'accepted'},'PATCH')[0],409)
        self.assertEqual(self.ok('/api/ai-tasks/'+task['id'])['artifacts'][0]['review_status'],'pending')

    def test_task_baselines_are_isolated_and_only_explicit_ack_advances(self):
        one=self.task(title='任务一');two=self.task(title='任务二')
        run1=self.freeze(one);run2=self.freeze(two)
        first=self.handoff(one,run1);self.assertEqual(first['mode'],'full')
        self.assertEqual(self.handoff(one,run1)['mode'],'full')
        self.ok('/api/ai-tasks/'+one['id']+'/handoffs/'+first['id']+'/ack',{})
        self.assertEqual(self.handoff(one,run1)['mode'],'delta')
        self.assertEqual(self.handoff(two,run2)['mode'],'full')
        next_run=self.freeze(one,goal='仅修改视频结尾')
        delta=self.handoff(one,next_run)
        self.assertEqual(delta['mode'],'delta');self.assertIn('仅修改视频结尾',delta['prompt'])
        self.assertEqual(self.handoff(one,next_run,'new-chat')['mode'],'full')

    def test_mcp_reads_frozen_skill_and_cannot_write_or_cross_projects(self):
        skill=self.app.skills.list()['skills'][0]
        preview=self.app.skills.collections.preview(skill['id'])
        collected=self.app.skills.collections.collect(preview['token'])
        task=self.task();run=self.freeze(task,[{'collection_id':collected['id'],'version':collected['version']}])
        other=self.task(self.other);other_run=self.freeze(other)
        self.ok('/api/mcp/configure',{'enabled':True,'project_id':self.project['id']})
        connection=self.ok('/api/mcp/connection',{})
        bearer=connection['config']['mcpServers']['yingxu']['headers']['Authorization']
        self.source.write_text('---\nname: example\n---\n\nChanged source B\n',encoding='utf-8')
        with patch.object(self.app.skills,'refresh',side_effect=AssertionError('MCP must not scan')):
            status,data=self.tool(bearer,'read_run_skill',{'task_id':task['id'],'run_id':run['id'],'collection_id':collected['id']})
        self.assertEqual(status,200,data);self.assertFalse(data['result']['isError'])
        text=data['result']['structuredContent']['text'];self.assertIn('Pinned A',text);self.assertNotIn('Changed source B',text)
        status,data=self.tool(bearer,'read_collaboration_task',{'task_id':task['id'],'run_id':run['id']})
        self.assertFalse(data['result']['isError']);self.assertNotIn(str(self.root),json.dumps(data))
        self.assertEqual(data['result']['structuredContent']['skills'][0]['version'],collected['version'])
        self.assertTrue(self.tool(bearer,'read_collaboration_task',{'task_id':other['id'],'run_id':other_run['id']})[1]['result']['isError'])
        self.assertEqual(self.request(self.prefix(task,run)+'/receive',{'idempotency_key':'b','files':[]},authorized=False,bearer=bearer)[0],403)
        self.assertEqual(self.tool(bearer,'write_resource',{})[1]['error']['code'],-32602)
