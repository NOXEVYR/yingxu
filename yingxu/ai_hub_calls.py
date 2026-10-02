"""Explicit source execution on original runs; no worker, polling or media library."""
from contextlib import contextmanager
import json
import threading
import time

from .ai_providers import ProviderError, canonical, fields, identifier, safe_value
from .hub_bindings import HubBindings
from .hub_client import AIHubExecutionClient, validate_receipt
from .hub_contract import ContractError
from .hub_prepare import prepare as freeze_request
from .store import UserError, now, uid


class AIHubCallService:
    @staticmethod
    def _safe_context(value, secret):
        safe_value(value, (secret,))
        # safe_value checks field names for credential words, not a token value
        # used as a decoded JSON key. Check both keys and values before storage.
        def keys(node):
            if isinstance(node, dict):
                for key, item in node.items():
                    if secret and secret in key:
                        raise UserError('私密值不能进入执行记录。')
                    keys(item)
            elif isinstance(node, list):
                for item in node:keys(item)
        keys(value)

    def __init__(self, store, tasks, selections, sources, migration=None, updates=None):
        self.store,self.tasks,self.selections,self.sources=store,tasks,selections,sources
        self.migration,self.updates=migration,updates
        self.bindings=HubBindings(store,tasks)
        self._condition=threading.Condition(threading.RLock())
        self._active=False;self._client=None;self._closed=False
        with store.lock,store.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS ai_hub_entry_intents(
                id TEXT PRIMARY KEY,task_id TEXT NOT NULL,run_id TEXT NOT NULL,project_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,payload_json TEXT NOT NULL,context_json TEXT NOT NULL,
                binding_id TEXT NOT NULL DEFAULT '',created TEXT NOT NULL,
                UNIQUE(task_id,run_id,idempotency_key));
                CREATE TRIGGER IF NOT EXISTS ai_hub_entry_intent_immutable BEFORE UPDATE OF
                id,task_id,run_id,project_id,idempotency_key,payload_json,context_json,created ON ai_hub_entry_intents
                BEGIN SELECT RAISE(ABORT,'hub source intent immutable'); END;''')

    @contextmanager
    def local_operation(self):
        with self._condition:
            if self._closed:raise UserError('曜核来源调用已关闭。',409)
            if self._active:raise UserError('已有曜核本地操作在途，请稍后核对。',409)
            self._active=True
        try:
            # Also protects callers that run outside an HTTP Handler.
            from contextlib import ExitStack
            with ExitStack() as stack:
                if self.updates:stack.enter_context(self.updates.mutation('POST','/api/ai-hub-sources'))
                if self.migration:stack.enter_context(self.migration.mutation('POST','/api/ai-hub-sources'))
                yield
        finally:
            with self._condition:
                self._client=None;self._active=False;self._condition.notify_all()

    def _scope(self,task_id,run_id):
        identifier(task_id,'任务标识');identifier(run_id,'轮次标识')
        with self.store.connection() as db:
            task=self.tasks._task(db,task_id);run=self.tasks._run(db,task_id,run_id)
        return task,run

    def _intent(self,task_id,run_id,binding_id):
        task,_=self._scope(task_id,run_id)
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM ai_hub_entry_intents WHERE task_id=? AND run_id=? AND binding_id=?',
                (task_id,run_id,binding_id)).fetchone()
        if row is None:raise UserError('曜核执行记录不属于原轮。',404)
        return task,dict(row)

    def _finish_prepare(self,row):
        context=json.loads(row['context_json'])
        binding=self.bindings.prepare('entry-'+row['id'],context['attempt'],context['selection'],
            context['descriptor'],context['input_json'],**context['options'])
        with self.store.lock,self.store.connection() as db:
            db.execute("UPDATE ai_hub_entry_intents SET binding_id=? WHERE id=? AND binding_id=''",(binding['binding_id'],row['id']))
            saved=db.execute('SELECT binding_id FROM ai_hub_entry_intents WHERE id=?',(row['id'],)).fetchone()['binding_id']
        if saved!=binding['binding_id']:raise UserError('原执行绑定需要核对。',409)
        return binding

    def prepare(self,task_id,run_id,body):
        fields(body,('source_id','source_revision','selection_id','input_json','idempotency_key'),
                    ('source_id','source_revision','selection_id','input_json','idempotency_key'))
        identifier(body['source_id'],'来源标识');identifier(body['selection_id'],'选型标识')
        identifier(body['idempotency_key'],'请求标识')
        if type(body['source_revision']) is not int or body['source_revision']<1:raise UserError('来源修订无效。')
        try:
            if not isinstance(body['input_json'],str) or len(body['input_json'].encode('utf-8'))>16000:
                raise UserError('输入JSON最多16000字节。')
        except UnicodeError:raise UserError('输入JSON必须是有效UTF-8文本。') from None
        payload=canonical(body)
        with self.local_operation():
            task,run=self._scope(task_id,run_id)
            with self.store.connection() as db:
                old=db.execute('SELECT * FROM ai_hub_entry_intents WHERE task_id=? AND run_id=? AND idempotency_key=?',
                    (task_id,run_id,body['idempotency_key'])).fetchone()
            if old:
                if old['payload_json']!=payload:raise UserError('同一次请求不能改变来源、选型或输入。',409)
                if old['binding_id']:return self.get(task_id,run_id,old['binding_id'])
                return self._finish_prepare(dict(old))
            source,secret=self.sources.resolve(body['source_id'],body['source_revision'])
            if source['role']!='source':raise UserError('历史回查授权不能提交执行。',403)
            try:decoded=json.loads(body['input_json'])
            except (ValueError,RecursionError):raise UserError('输入JSON无效。') from None
            self._safe_context(decoded,secret)
            self._safe_context(body,secret)
            descriptor=self.sources.checked_descriptor(source['id'],source['revision'])
            selected=self.selections.get(task_id,run_id,body['selection_id'])
            attempt={k:selected[k] for k in ('project_id','task_id','run_id','input_digest')}
            attempt.update(id=uid(),request_id=uid(),connection_id=source['id'],connection_revision=source['revision'])
            selection={**{k:attempt[k] for k in ('project_id','task_id','run_id','input_digest','connection_id','connection_revision')},
                'snapshot':selected['snapshot'],'capability_id':selected['capability_id'],'declaration_sha256':selected['declaration_sha256']}
            options={'source_authority':source['subject'],'input_revision':run['input_digest'],
                'hub_project':'yingxu-'+task['project_id'],'title':task['title'][:160]}
            # Validate before either intent or binding is stored.
            context_value={'attempt':attempt,'selection':selection,'descriptor':descriptor,'input_json':body['input_json'],'options':options}
            self._safe_context(context_value,secret)
            try:frozen=freeze_request(attempt,selection,descriptor,body['input_json'],**options)
            except ContractError:raise UserError('来源、能力快照或JSON输入不符合执行契约。',409) from None
            self._safe_context(frozen.body_copy(),secret)
            context=canonical(context_value)
            with self.tasks.operation_lock,self.store.lock,self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                self.bindings._scope(db,task['project_id'],task_id,run_id,writable=True)
                if db.execute('SELECT count(*) FROM ai_hub_entry_intents').fetchone()[0]>=5000:raise UserError('曜核调用记录已达到容量上限。',409)
                row={'id':uid(),'task_id':task_id,'run_id':run_id,'project_id':task['project_id'],
                    'idempotency_key':body['idempotency_key'],'payload_json':payload,'context_json':context,'created':now()}
                db.execute('INSERT INTO ai_hub_entry_intents(id,task_id,run_id,project_id,idempotency_key,payload_json,context_json,created) VALUES(?,?,?,?,?,?,?,?)',
                    tuple(row[k] for k in ('id','task_id','run_id','project_id','idempotency_key','payload_json','context_json','created')))
                binding=self.bindings._prepare_in_transaction(db,'entry-'+row['id'],attempt,frozen)
                db.execute('UPDATE ai_hub_entry_intents SET binding_id=? WHERE id=?',(binding['binding_id'],row['id']))
                return binding

    def get(self,task_id,run_id,binding_id):
        identifier(binding_id,'执行记录')
        task,_row=self._intent(task_id,run_id,binding_id)
        return self.bindings.get(task['project_id'],task_id,run_id,binding_id)

    def list(self,task_id,run_id,limit=48,offset=0):
        task,_=self._scope(task_id,run_id)
        if type(limit) is not int or not 1<=limit<=48 or type(offset) is not int or not 0<=offset<=5000:raise UserError('执行分页无效。')
        with self.store.connection() as db:
            rows=db.execute("SELECT binding_id FROM ai_hub_entry_intents WHERE task_id=? AND run_id=? AND binding_id<>'' ORDER BY rowid DESC LIMIT ? OFFSET ?",(task_id,run_id,limit,offset)).fetchall()
            total=db.execute("SELECT count(*) FROM ai_hub_entry_intents WHERE task_id=? AND run_id=? AND binding_id<>''",(task_id,run_id)).fetchone()[0]
            incomplete=db.execute("SELECT id,created FROM ai_hub_entry_intents WHERE task_id=? AND run_id=? AND binding_id='' ORDER BY rowid DESC LIMIT 48",(task_id,run_id)).fetchall()
        return {'items':[self.bindings.get(task['project_id'],task_id,run_id,r['binding_id']) for r in rows],
            'incomplete':[{'intent_id':r['id'],'created':r['created']} for r in incomplete],
            'total':total,'limit':limit,'offset':offset}

    def restore_prepare(self,task_id,run_id,intent_id):
        identifier(intent_id,'原准备记录')
        with self.local_operation():
            self._scope(task_id,run_id)
            with self.store.connection() as db:
                row=db.execute('SELECT * FROM ai_hub_entry_intents WHERE id=? AND task_id=? AND run_id=?',(intent_id,task_id,run_id)).fetchone()
            if row is None:raise UserError('准备记录不属于原轮。',404)
            if row['binding_id']:return self.get(task_id,run_id,row['binding_id'])
            return self._finish_prepare(dict(row))

    def operate(self,task_id,run_id,binding_id,action):
        if action not in ('accept','query','cancel'):raise UserError('来源动作无效。')
        with self.local_operation():
            task,row=self._intent(task_id,run_id,binding_id)
            context=json.loads(row['context_json']);attempt=context['attempt']
            scope=(task['project_id'],task_id,run_id,binding_id)
            frozen,previous=self.bindings.submission_snapshot(*scope)
            source,secret=self.sources.resolve(attempt['connection_id'],attempt['connection_revision'])
            if (source['subject']!=frozen.body_copy()['origin']['authority_id'] or
                source['execution_authority_id']!=frozen.service_authority or source['ledger_epoch']!=frozen.ledger_epoch or
                source['workspace_root']!=frozen.body_copy()['_workspace_root']):raise UserError('原来源授权范围已变化，不能重派或移交。',409)
            if action=='accept':
                if source['role']!='source':raise UserError('此授权仅可历史回查。',403)
                self.bindings.mark_submit_intent(*scope)
                path='/api/execution/accept';body=frozen.body_copy()
            else:
                body={'_workspace_root':frozen.body_copy()['_workspace_root']}
                if action=='query':path='/api/execution/status';body['request_id']=frozen.body_copy()['request_id']
                else:
                    if source['role']!='source' or not previous:raise UserError('尚无可取消的原执行或只读授权。',409)
                    path='/api/execution/cancel';body['execution_id']=previous['execution_id']
            client=AIHubExecutionClient({'base_url':source['base_url']},secret)
            with self._condition:
                if self._closed:
                    if action=='accept':self.bindings.mark_unknown(*scope)
                    raise UserError('调用已关闭，请下次核对原请求；本次不会发出派单。',409)
                self._client=client
            try:
                raw=client._request('POST',path,body)
                validate_receipt(raw,frozen,previous=previous,known_secret=secret)
                return self.bindings.observe(*scope,raw,known_secret=secret)
            except (ProviderError,ContractError):
                current=self.bindings.get(*scope)
                if current['local_state'] in ('submitting','unknown'):self.bindings.mark_unknown(*scope)
                raise UserError('原请求结果尚未确认，请明确核对；不会自动重新提交。',502) from None

    def status(self):
        with self._condition:result={'local_busy':self._active,'busy':self._active,'local_queued':0,'closed':self._closed}
        with self.store.connection() as db:
            rows=db.execute('SELECT receipt_json FROM ai_hub_bindings_candidate WHERE local_state<>?',('prepared',))
            pending=0
            for row in rows:
                try:
                    receipt=json.loads(row['receipt_json']) if row['receipt_json'] else None
                    terminal=isinstance(receipt,dict) and receipt.get('provider_state') in ('succeeded','failed','cancelled')
                except (ValueError,TypeError,RecursionError):terminal=False
                # Status/exit only counts, never treats malformed history as proof.
                # Invalid rows remain intact for explicit original-request checking.
                if not terminal:pending+=1
            result['remote_pending']=pending
        return result

    def close(self,timeout=2.5):
        deadline=time.monotonic()+max(0,min(float(timeout),5))
        with self._condition:
            self._closed=True;client=self._client
        if client:client.abort()
        with self._condition:
            while self._active and time.monotonic()<deadline:self._condition.wait(max(0,deadline-time.monotonic()))
        result=self.status();result['worker_stopped']=not result['local_busy'];return result
