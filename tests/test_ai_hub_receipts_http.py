"""Actual workbench HTTP: route/auth mocks plus isolated real-service receipts."""
from unittest.mock import patch
import test_ai_tasks_http as fixture


class HubReceiptHttpTests(fixture.AITaskHttpTests):
    def test_real_receipt_service_over_tcp_is_idempotent_and_keeps_pending_review(self):
        import test_ai_hub_receipts as receipt_fixture
        sample=receipt_fixture.HubReceiptTests()
        sample.setUp();self.addCleanup(sample.doCleanups)
        binding,_receipt,body,_path=sample.result()
        path='/api/ai-tasks/'+sample.task['id']+'/runs/'+sample.run['id']+'/hub-calls/'+binding['binding_id']+'/receipts'
        with patch.multiple(self.app,ai_tasks=sample.tasks,ai_hub_calls=sample.service,ai_hub_receipts=sample.links):
            received=self.ok(path,body)
            self.assertEqual(received['state'],'completed')
            self.assertEqual(self.ok(path,body)['id'],received['id'])
            self.assertEqual(self.ok(path+'/'+received['id'])['id'],received['id'])
            self.assertEqual(len(self.ok(path)['items']),1)
            detail=sample.detail();self.assertEqual(detail['receipts_total'],1);self.assertEqual(detail['artifacts_total'],1)
            self.assertEqual(detail['artifacts'][0]['review_status'],'pending')
            sample.tasks.freeze_run(sample.task['id'],{'expected_revision':detail['revision'],
                'client_id':'synthetic','conversation_id':'next'})
            self.assertEqual(self.ok(path,body)['id'],received['id'])
            self.assertEqual(self.request(path,{**body,'idempotency_key':'new-key'})[0],409)

    def path(self):
        task=self.task();run=self.freeze(task)
        return task,run,self.prefix(task,run)+'/hub-calls/'+'b'*32+'/receipts'

    def test_receipt_read_uses_workbench_authorization_and_no_mcp_bearer(self):
        task,run,path=self.path()
        self.assertEqual(self.request(path,authorized=False)[0],403)
        self.assertEqual(self.request(path,authorized=False,bearer='synthetic-mcp')[0],403)
        with patch.object(self.app.ai_hub_receipts,'list',return_value={'items':[]}) as listing:
            self.assertEqual(self.ok(path),{'items':[]})
            listing.assert_called_once_with(task['id'],run['id'],'b'*32)
        self.assertEqual(self.request(path+'?unexpected=true')[0],400)

    def test_receive_original_scope_and_resume_empty_body(self):
        task,run,path=self.path();body={'idempotency_key':'original','result_id':'🟡',
            'results_manifest_sha256':'a'*64,'relative_path':'original.png'}
        with patch.object(self.app.ai_hub_receipts,'receive',return_value={'state':'pending'}) as receive:
            self.assertEqual(self.ok(path,body),{'state':'pending'})
            receive.assert_called_once_with(task['id'],run['id'],'b'*32,body)
        selected='c'*32
        with patch.object(self.app.ai_hub_receipts,'resume',return_value={'state':'completed'}) as resume:
            self.assertEqual(self.request(path+'/'+selected+'/resume',{'unexpected':True})[0],400)
            self.assertEqual(self.ok(path+'/'+selected+'/resume',{}),{'state':'completed'})
            resume.assert_called_once_with(task['id'],run['id'],'b'*32,selected)

    def test_real_receipt_route_checks_scope_before_files_and_rejects_guessed_bindings(self):
        task,run,path=self.path()
        self.assertEqual(self.request(path)[0],404)
        body={'idempotency_key':'original','result_id':'🟡','results_manifest_sha256':'a'*64,'relative_path':'outside.png'}
        self.assertEqual(self.request(path,body)[0],404)
        self.assertEqual(self.app.ai_tasks.get_task(task['id'])['artifacts_total'],0)

    def test_receipt_route_rejected_during_update_and_migration(self):
        _task,_run,path=self.path()
        self.app.migration_jobs.active='synthetic-reservation'
        try:self.assertEqual(self.request(path,{})[0],409)
        finally:self.app.migration_jobs.active=None
        self.app.update_service.committed=True
        try:self.assertEqual(self.request(path,{})[0],409)
        finally:self.app.update_service.committed=False


for _name in tuple(name for name in dir(fixture.AITaskHttpTests) if name.startswith('test_')):
    if _name not in HubReceiptHttpTests.__dict__:setattr(HubReceiptHttpTests,_name,None)
