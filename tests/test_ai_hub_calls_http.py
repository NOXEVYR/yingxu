"""Actual workbench HTTP, synthetic service replies, no model/network outside localhost."""
import threading
from unittest.mock import patch
import test_ai_tasks_http as fixture


class HubEntryHttpTests(fixture.AITaskHttpTests):
    # Reuse setup/helpers without inheriting all original scenario methods.
    def test_hub_read_requires_workbench_session_not_mcp_bearer(self):
        self.assertEqual(self.request('/api/ai-hub-sources',authorized=False)[0],403)
        self.assertEqual(self.request('/api/ai-hub-sources',authorized=False,bearer='synthetic-mcp')[0],403)
        data=self.ok('/api/ai-hub-sources')
        self.assertIn('source_authority',data['identity']);self.assertEqual(data['items'],[])
        task=self.task();run=self.freeze(task);path=self.prefix(task,run)+'/hub-calls'
        self.assertEqual(self.request(path,authorized=False)[0],403)
        self.assertEqual(self.ok(path)['items'],[])
        self.assertEqual(self.request(path+'?unexpected=true')[0],400)

    def test_hub_actions_require_empty_body_and_original_route_scope(self):
        task=self.task();run=self.freeze(task);path=self.prefix(task,run)+'/hub-calls/'+'b'*32
        self.assertEqual(self.request(path+'/query',{'unexpected':True})[0],400)
        self.assertEqual(self.request(path+'/query',{})[0],404)
        self.assertEqual(self.request('/api/ai-hub-sources/'+'b'*32+'/check',{'unexpected':True})[0],400)
        self.assertEqual(self.request('/api/ai-hub-sources/'+'b'*32+'/check',{})[0],404)

    def test_hub_local_busy_aggregated_into_native_exit_and_two_mutation_gates(self):
        source='a'*32;entered=threading.Event();release=threading.Event();status=[]
        def check(_source):entered.set();release.wait(3);return {'id':source,'checked':True}
        with patch.object(self.app.ai_hub_sources,'check',side_effect=check):
            thread=threading.Thread(target=lambda:status.append(self.request('/api/ai-hub-sources/'+source+'/check',{})[0]));thread.start()
            self.assertTrue(entered.wait(1))
            try:
                aggregate=self.ok('/api/ai-calls/status');self.assertTrue(aggregate['local_busy']);self.assertTrue(aggregate['hub']['busy'])
                self.assertGreater(self.app.update_service.writers,0)
                self.assertGreater(self.app.migration_jobs.writers,0)
            finally:release.set();thread.join(5)
        self.assertEqual(status,[200]);self.assertFalse(self.ok('/api/ai-calls/status')['busy'])

    def test_hub_write_rejected_during_migration_and_update_prepare(self):
        self.app.migration_jobs.active='synthetic-reservation'
        try:self.assertEqual(self.request('/api/ai-hub-sources',{'synthetic':True})[0],409)
        finally:self.app.migration_jobs.active=None
        self.app.update_service.committed=True
        try:self.assertEqual(self.request('/api/ai-hub-sources',{'synthetic':True})[0],409)
        finally:self.app.update_service.committed=False


# Do not double-run inherited base scenarios in this focused module.
for _name in tuple(name for name in dir(fixture.AITaskHttpTests) if name.startswith('test_')):
    if _name not in HubEntryHttpTests.__dict__:setattr(HubEntryHttpTests,_name,None)
