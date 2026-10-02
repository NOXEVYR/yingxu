"""Original-run entry/lifecycle regression. Temporary Store, synthetic replies."""
import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from yingxu.ai_hub_calls import AIHubCallService
from yingxu.ai_providers import ProviderError
from yingxu.ai_tasks import AITaskService
from yingxu.hub_contract import canonical
from yingxu.store import Store,UserError


class Gate:
    def __init__(self):self.active=0;self.blocked=False
    @contextmanager
    def mutation(self,method,path):
        if self.blocked:raise UserError('synthetic gate blocked',409)
        self.active+=1
        try:yield
        finally:self.active-=1


class HubCallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='yingxu-hub-entry-tests-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.store=Store(self.root/'data',self.root/'projects');self.tasks=AITaskService(self.store)
        self.project=self.store.create_project('合成来源项目')
        self.task=self.tasks.create_task({'project_id':self.project['id'],'title':'合成来源任务','goal':'不调用模型'})
        self.run=self.tasks.freeze_run(self.task['id'],{'expected_revision':self.task['revision'],'client_id':'synthetic','conversation_id':'entry'})
        raw='{ "key": "synthetic.test", "inputs": [] }\r\n';fingerprint=hashlib.sha256(raw.encode()).hexdigest()
        self.descriptor={'protocol':'aihub-execution/1','schema_version':1,'accepted_transaction':'one_collaboration_database_commit',
            'provider_execution':'scoped_worker_adapter','input_digest':'sha256_exact_input_json_utf8','input_bytes':16000,
            'native_cancel':False,'routing':'declared_client','execution_authority_id':str(uuid.uuid4()),'ledger_epoch':str(uuid.uuid4()),
            'identity':{'service_instance_id':str(uuid.uuid4())},'workspace':{'binding_revision':'b'*64},
            'workspace_root':str(self.root/'hub'),'connection_revision':'c'*64}
        snapshot={k:copy.deepcopy(self.descriptor[k]) for k in ('identity','workspace','workspace_root','connection_revision')}
        snapshot.update(selected={'id':'b'*32,'client_id':'合成工作端'},declaration={'text':raw,'sha256':fingerprint})
        self.selected={'project_id':self.project['id'],'task_id':self.task['id'],'run_id':self.run['id'],
            'input_digest':self.run['input_digest'],'capability_id':'b'*32,'declaration_sha256':fingerprint,'snapshot':snapshot}
        self.source={'id':'a'*32,'revision':1,'role':'source','subject':str(uuid.uuid4()),'base_url':'http://127.0.0.1:19876',
            'execution_authority_id':self.descriptor['execution_authority_id'],'ledger_epoch':self.descriptor['ledger_epoch'],
            'workspace_root':self.descriptor['workspace_root']}
        self.secret='synthetic-scoped-secret'
        outer=self
        class Sources:
            def resolve(_self,source_id,revision=None):
                if source_id!=outer.source['id'] or revision!=outer.source['revision']:raise UserError('synthetic revision changed',409)
                return copy.deepcopy(outer.source),outer.secret
            def checked_descriptor(_self,source_id,revision):return copy.deepcopy(outer.descriptor)
        class Selections:
            def get(_self,task_id,run_id,selection_id):
                if (task_id,run_id,selection_id)!=(outer.task['id'],outer.run['id'],'d'*32):raise UserError('synthetic selection scope',404)
                return copy.deepcopy(outer.selected)
        self.sources,self.selections=Sources(),Selections();self.migration=Gate();self.updates=Gate()
        self.service=AIHubCallService(self.store,self.tasks,self.selections,self.sources,self.migration,self.updates)
        self.addCleanup(self.service.close)
        self.body={'source_id':'a'*32,'source_revision':1,'selection_id':'d'*32,'input_json':' {"prompt":"合成输入"}\r\n','idempotency_key':str(uuid.uuid4())}

    def prepared(self):return self.service.prepare(self.task['id'],self.run['id'],copy.deepcopy(self.body))
    def operate(self,binding,action):return self.service.operate(self.task['id'],self.run['id'],binding['binding_id'],action)
    def receipt(self,binding):
        frozen,_=self.service.bindings.submission_snapshot(self.project['id'],self.task['id'],self.run['id'],binding['binding_id'])
        body=frozen.body_copy()
        return {'protocol':'aihub-execution/1','execution_authority_id':frozen.service_authority,'ledger_epoch':frozen.ledger_epoch,
            'execution_id':str(uuid.uuid4()),'request_id':body['request_id'],'origin':body['origin'],'input_sha256':body['input_sha256'],
            'workspace_binding_revision':body['workspace_binding_revision'],'capability_id':body['capability_id'],
            'declaration_sha256':body['expected_declaration_sha256'],'executor':{'client_id':frozen.expected_client,'tool':'synthetic'},
            'queue_task_id':str(uuid.uuid4()),'dispatch_state':'queued_ready','provider_state':'not_started','cancel_requested':False,
            'provider_request_id':None,'results':[],'results_manifest_sha256':None,'result_identity_scope':'execution_authority_id/execution_id/result_id',
            'outcome':{},'evidence_source':'queue_ledger','native_execution_verified_by_hub':False,'native_cancel_by_hub':False,'materialization_error':None}

    def test_prepare_is_durable_same_key_and_never_networks(self):
        with patch('yingxu.hub_client.AIHubExecutionClient._request',side_effect=AssertionError('must not dispatch')):
            first=self.prepared();self.assertEqual(self.prepared()['binding_id'],first['binding_id'])
        self.assertEqual(first['local_state'],'prepared');self.assertEqual(self.service.list(self.task['id'],self.run['id'])['total'],1)
        reopened=AIHubCallService(self.store,self.tasks,self.selections,self.sources,self.migration,self.updates);self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(self.task['id'],self.run['id'],first['binding_id'])['request_id'],first['request_id'])
        self.assertFalse(hasattr(reopened,'_worker'))

    def test_same_key_changed_input_or_source_revision_is_conflict(self):
        self.prepared()
        for field,value in (('input_json','{"prompt":"changed"}'),('source_revision',2)):
            bad=copy.deepcopy(self.body);bad[field]=value
            with self.assertRaises(UserError) as raised:self.service.prepare(self.task['id'],self.run['id'],bad)
            self.assertEqual(raised.exception.status,409)

    def test_source_read_cannot_prepare_or_accept(self):
        self.source['role']='source_read'
        with self.assertRaises(UserError) as raised:self.prepared()
        self.assertEqual(raised.exception.status,403)
        self.source['role']='source';binding=self.prepared();self.source['role']='source_read'
        with self.assertRaises(UserError) as raised:self.operate(binding,'accept')
        self.assertEqual(raised.exception.status,403)
        self.assertEqual(self.service.get(self.task['id'],self.run['id'],binding['binding_id'])['local_state'],'prepared')

    def test_unknown_accept_is_reconciled_by_original_query_never_accept_again(self):
        binding=self.prepared();receipt=self.receipt(binding);seen=[]
        def response(_client,method,path,body):
            seen.append((method,path,copy.deepcopy(body)))
            if path.endswith('/accept'):raise ProviderError('lost','synthetic lost',True)
            return canonical(receipt)
        with patch('yingxu.hub_client.AIHubExecutionClient._request',new=response):
            with self.assertRaises(UserError):self.operate(binding,'accept')
            self.assertEqual(self.service.get(self.task['id'],self.run['id'],binding['binding_id'])['local_state'],'unknown')
            confirmed=self.operate(binding,'query')
            with self.assertRaises(UserError):self.operate(binding,'accept')
        self.assertEqual([s[1] for s in seen],['/api/execution/accept','/api/execution/status'])
        self.assertEqual(seen[0][2]['request_id'],seen[1][2]['request_id']);self.assertTrue(confirmed['hub_accepted'])
        self.assertEqual(self.service.status()['remote_pending'],1);self.assertFalse(self.service.status()['busy'])

    def test_tampered_receipt_stays_unknown_without_success(self):
        binding=self.prepared();bad=self.receipt(binding);bad['origin']['run_id']='wrong'
        with patch('yingxu.hub_client.AIHubExecutionClient._request',return_value=canonical(bad)):
            with self.assertRaises(UserError):self.operate(binding,'accept')
        view=self.service.get(self.task['id'],self.run['id'],binding['binding_id'])
        self.assertEqual(view['local_state'],'unknown');self.assertFalse(view['hub_accepted'])

    def test_config_change_blocks_original_operation_without_new_intent(self):
        binding=self.prepared();self.source['revision']=2
        with patch('yingxu.hub_client.AIHubExecutionClient._request',side_effect=AssertionError('no network')):
            with self.assertRaises(UserError):self.operate(binding,'accept')
        self.assertEqual(self.service.list(self.task['id'],self.run['id'])['total'],1)

    def test_cancel_without_execution_rejected_and_reported_cancel_not_native(self):
        binding=self.prepared()
        with self.assertRaises(UserError):self.operate(binding,'cancel')
        receipt=self.receipt(binding)
        with patch('yingxu.hub_client.AIHubExecutionClient._request',return_value=canonical(receipt)):self.operate(binding,'accept')
        cancelled=dict(receipt,dispatch_state='cancelled_before_claim',provider_state='cancelled',cancel_requested=True)
        with patch('yingxu.hub_client.AIHubExecutionClient._request',return_value=canonical(cancelled)):view=self.operate(binding,'cancel')
        self.assertTrue(view['cancel_requested']);self.assertFalse(view['native_verified']);self.assertEqual(self.service.status()['remote_pending'],0)

    def test_cross_scope_get_or_restore_rejected(self):
        binding=self.prepared();other=self.tasks.create_task({'project_id':self.project['id'],'title':'隔离','goal':'合成隔离目标'})
        other_run=self.tasks.freeze_run(other['id'],{'expected_revision':other['revision'],'client_id':'synthetic','conversation_id':'other'})
        with self.assertRaises(UserError):self.service.get(other['id'],other_run['id'],binding['binding_id'])
        with self.store.connection() as db:intent=db.execute('SELECT id FROM ai_hub_entry_intents').fetchone()['id']
        with self.assertRaises(UserError):self.service.restore_prepare(other['id'],other_run['id'],intent)

    def test_prepare_interrupt_rolls_back_both_intent_and_binding(self):
        original=self.service.bindings._prepare_in_transaction
        def interrupted(*args):
            original(*args)
            raise RuntimeError('synthetic transaction interrupt')
        with patch.object(self.service.bindings,'_prepare_in_transaction',side_effect=interrupted):
            with self.assertRaises(RuntimeError):self.prepared()
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ai_hub_entry_intents').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM ai_hub_bindings_candidate').fetchone()[0],0)
        self.assertEqual(self.prepared()['local_state'],'prepared')

    def test_legacy_gap_restores_only_original_intent_and_latest_scope(self):
        first=self.prepared()
        with self.store.connection() as db:
            db.execute("UPDATE ai_hub_entry_intents SET binding_id=''")
            db.execute('DELETE FROM ai_hub_bindings_candidate')
        incomplete=self.service.list(self.task['id'],self.run['id'])['incomplete']
        self.assertEqual(len(incomplete),1)
        restored=self.service.restore_prepare(self.task['id'],self.run['id'],incomplete[0]['intent_id'])
        self.assertEqual(self.prepared()['binding_id'],restored['binding_id'])
        self.assertEqual(first['request_id'],restored['request_id'])
        self.assertEqual(self.service.list(self.task['id'],self.run['id'])['incomplete'],[])
        with self.store.connection() as db:
            db.execute("UPDATE ai_hub_entry_intents SET binding_id=''")
            db.execute('DELETE FROM ai_hub_bindings_candidate')
        task=self.tasks.get_task(self.task['id'])
        self.tasks.freeze_run(task['id'],{'expected_revision':task['revision'],'client_id':'synthetic','conversation_id':'next'})
        with self.assertRaises(UserError) as raised:self.service.restore_prepare(self.task['id'],self.run['id'],incomplete[0]['intent_id'])
        self.assertEqual(raised.exception.status,409)
        self.assertEqual(self.service.list(self.task['id'],self.run['id'])['incomplete'],incomplete)

    def test_latest_run_guard_and_history_read_retained(self):
        binding=self.prepared();task=self.tasks.get_task(self.task['id'])
        self.tasks.freeze_run(task['id'],{'expected_revision':task['revision'],'client_id':'synthetic','conversation_id':'next'})
        with self.assertRaises(UserError) as raised:self.operate(binding,'accept')
        self.assertEqual(raised.exception.status,409)
        self.assertEqual(self.service.get(self.task['id'],self.run['id'],binding['binding_id'])['run_id'],self.run['id'])

    def test_inputs_secret_fields_and_echo_never_enter_intent_or_public(self):
        for value in ('{"api_key":"synthetic"}',json.dumps({'prompt':self.secret})):
            bad=dict(self.body,input_json=value)
            with self.assertRaises(UserError):self.service.prepare(self.task['id'],self.run['id'],bad)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM ai_hub_entry_intents').fetchone()[0],0)
        binding=self.prepared()
        self.assertNotIn(self.secret,json.dumps(binding))
        with self.store.connection() as db:
            dumped='\n'.join(db.iterdump());self.assertNotIn(self.secret,dumped)

    def test_escaped_token_keys_values_and_context_echo_rejected_before_storage(self):
        self.secret='X'*43
        for value in (json.dumps({'prompt':self.secret}),json.dumps({self.secret:'ordinary'})):
            escaped=value.replace('X','\\u0058',1)
            self.assertNotIn(self.secret,escaped)
            with self.assertRaises(UserError):self.service.prepare(self.task['id'],self.run['id'],dict(self.body,input_json=escaped))
        for target in ('title','snapshot','descriptor'):
            with self.subTest(target=target):
                if target=='title':
                    with self.store.connection() as db:db.execute('UPDATE ai_tasks SET title=? WHERE id=?',(self.secret,self.task['id']))
                elif target=='snapshot':self.selected['snapshot']['echo']=self.secret
                else:self.descriptor['echo']=self.secret
                with self.assertRaises(UserError):self.prepared()
                if target=='title':
                    with self.store.connection() as db:db.execute('UPDATE ai_tasks SET title=? WHERE id=?',('合成标题',self.task['id']))
                elif target=='snapshot':self.selected['snapshot'].pop('echo')
                else:self.descriptor.pop('echo')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ai_hub_entry_intents').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM ai_hub_bindings_candidate').fetchone()[0],0)

    def test_invalid_utf8_is_a_bounded_user_error(self):
        with self.assertRaises(UserError):self.service.prepare(self.task['id'],self.run['id'],dict(self.body,input_json='\ud800'))

    def test_both_gates_and_local_busy_protect_exit_migration_and_update(self):
        for gate in (self.migration,self.updates):
            gate.blocked=True
            with self.assertRaises(UserError):self.prepared()
            gate.blocked=False
        with self.service.local_operation():
            self.assertTrue(self.service.status()['busy']);self.assertEqual((self.migration.active,self.updates.active),(1,1))
            with self.assertRaises(UserError):self.prepared()
        self.assertFalse(self.service.status()['busy']);self.assertEqual((self.migration.active,self.updates.active),(0,0))

    def test_close_aborts_only_own_inflight_and_preserves_unknown(self):
        binding=self.prepared();entered=threading.Event();aborted=threading.Event();errors=[]
        def delayed(_client,*args):
            entered.set();aborted.wait(2);raise ProviderError('aborted','synthetic abort',True)
        def work():
            try:self.operate(binding,'accept')
            except UserError:errors.append('expected_unknown')
        with patch('yingxu.hub_client.AIHubExecutionClient._request',new=delayed),patch('yingxu.hub_client.AIHubExecutionClient.abort',side_effect=aborted.set):
            thread=threading.Thread(target=work);thread.start();self.assertTrue(entered.wait(1))
            result=self.service.close();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertTrue(result['worker_stopped']);self.assertEqual(errors,['expected_unknown'])
        self.assertEqual(self.service.get(self.task['id'],self.run['id'],binding['binding_id'])['local_state'],'unknown')
        with self.assertRaises(UserError):self.prepared()

    def test_malformed_history_cannot_break_status_or_shutdown(self):
        binding=self.prepared()
        self.service.bindings.mark_submit_intent(self.project['id'],self.task['id'],self.run['id'],binding['binding_id'])
        self.service.bindings.mark_unknown(self.project['id'],self.task['id'],self.run['id'],binding['binding_id'])
        for raw in ('{}', '[]', 'null', '{invalid', '{"provider_state":[]}'):
            with self.subTest(raw=raw):
                with self.store.connection() as db:
                    db.execute('UPDATE ai_hub_bindings_candidate SET receipt_json=? WHERE id=?',(raw,binding['binding_id']))
                self.assertEqual(self.service.status()['remote_pending'],1)
                self.assertTrue(self.service.close()['worker_stopped'])
                with self.store.connection() as db:
                    self.assertEqual(db.execute('SELECT receipt_json FROM ai_hub_bindings_candidate WHERE id=?',(binding['binding_id'],)).fetchone()[0],raw)

    def test_close_before_client_registration_prevents_dispatch(self):
        binding=self.prepared();entered=threading.Event();resume=threading.Event();errors=[]
        resolve=self.sources.resolve
        def delayed(*args):
            entered.set();resume.wait(2);return resolve(*args)
        def work():
            try:self.operate(binding,'accept')
            except UserError as error:errors.append(error.status)
        with patch.object(self.sources,'resolve',side_effect=delayed),patch('yingxu.hub_client.AIHubExecutionClient._request',side_effect=AssertionError('no network after closing')):
            thread=threading.Thread(target=work);thread.start();self.assertTrue(entered.wait(1))
            result=self.service.close(0);self.assertFalse(result['worker_stopped'])
            resume.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[409])
        self.assertEqual(self.service.get(self.task['id'],self.run['id'],binding['binding_id'])['local_state'],'unknown')

if __name__=='__main__':unittest.main()
