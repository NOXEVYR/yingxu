'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../frontend/ai-tool-calls.js'),'utf8');
const escapeHtml=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('\"','&quot;');
const tick=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
class Node {
  constructor(tag='section'){this.tag=tag;this.children=[];this.parent=null;this.connected=false;this._html='';this.controls=[];this.className='';}
  get isConnected(){return this.connected || !!this.parent?.isConnected;}
  append(node){node.parent=this;this.children.push(node);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);this.parent=null;this.connected=false;}
  set innerHTML(value){this.children.forEach(x=>x.parent=null);this.children=[];this._html=value;this.controls=[];for(const match of value.matchAll(/<(button|input|select|textarea)\b([^>]*)(?:>([\s\S]*?)<\/\1>)?/g)){
    const attributes=match[2],dataset={};for(const a of attributes.matchAll(/data-([\w-]+)="([^"]*)"/g))dataset[a[1].replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=a[2];
    const control={dataset,disabled:/\bdisabled\b/.test(attributes),checked:/\bchecked\b/.test(attributes),type:attributes.match(/type="([^"]*)"/)?.[1]||match[1],value:attributes.match(/value="([^"]*)"/)?.[1] || match[3] || '',attributes,owner:this,focus(){this.focused=true;},closest(){return this;}};
    this.controls.push(control);
  }}
  get innerHTML(){return this._html+this.children.map(x=>x.innerHTML).join('');}
  contains(target){return target.owner===this || this.children.some(x=>x.contains(target));}
  querySelector(selector){if(selector==='.ai-collaboration')return this.children.find(x=>x.className==='ai-collaboration')||null;const matches=[...selector.matchAll(/\[([^=\]]+)(?:="([^"]*)")?\]/g)];return this.controls.find(c=>matches.every(m=>m[1].startsWith('data-')?c.dataset[m[1].slice(5).replace(/-([a-z])/g,(_,x)=>x.toUpperCase())]!==undefined && (m[2]===undefined || c.dataset[m[1].slice(5).replace(/-([a-z])/g,(_,x)=>x.toUpperCase())]===m[2]):false))||this.children.map(x=>x.querySelector(selector)).find(Boolean)||null;}
}

const task={id:'1'.repeat(32),project_id:'p1'},run={id:'a'.repeat(32)},connectionId='2'.repeat(32),attemptId='3'.repeat(32);
const base='/api/ai-tasks/'+task.id+'/runs/'+run.id+'/calls';
function connection(extra={}){return {id:connectionId,name:'本地合成接口',enabled:true,revision:1,checked:true,operations:[{id:'mock',name:'合成文本',input_schema:{type:'object',properties:{prompt:{type:'string',title:'描述'},count:{type:'integer',default:1}},required:['prompt']},supports:{query:true,lookup:true,cancel:false}}],...extra};}
function attempt(extra={}){return {id:attemptId,task_id:task.id,run_id:run.id,request_id:'original-request',state:'queued',supports:{query:true,lookup:true,cancel:false},results:[],...extra};}
function fixture(){
  const calls=[],pending=[],errors=[],toasts=[],state={projectId:'p1',section:'context'},root=new Node();root.connected=true;let uuid=0,current=true;
  const context=vm.createContext({window:{crypto:{randomUUID:()=> 'request-'+(++uuid)}}});vm.runInContext(source,context);
  const env={task,run,state,api:(url,options={})=>{calls.push({url,options});return new Promise((resolve,reject)=>pending.push({url,resolve,reject}));},escapeHtml,report:error=>errors.push(error),toast:(...args)=>toasts.push(args),isCurrent:()=>current,hasPendingEdits:()=>!!state.dirty};
  let view;const mount=()=>{view?.dispose();view=context.window.YingXuToolCalls.mount(root,env);return view;};
  const click=(name,extra='')=>{const target=root.querySelector(`[data-call="${name}"]${extra}`);assert.ok(target,'Missing '+name);root.onclick({target,stopPropagation(){}});};
  const input=(name,value,parameter=false)=>{const target=root.querySelector(`[data-call-${parameter?'parameter':'field'}="${name}"]`);assert.ok(target,'Missing field '+name);target.value=value;root.oninput({target});};
  const receiptInput=(name,value)=>{const target=root.querySelector(`[data-call-receipt-field="${name}"][data-attempt="${attemptId}"]`);assert.ok(target,'Missing receipt field '+name);if(name==='confirmed')target.checked=value;else target.value=value;root.oninput({target});};
  const resolve=async(url,value)=>{const index=pending.findIndex(x=>x.url===url);assert.ok(index>=0,'Missing request '+url);pending.splice(index,1)[0].resolve(value);await tick();};
  const reject=async(url,error)=>{const index=pending.findIndex(x=>x.url===url);assert.ok(index>=0,'Missing request '+url);pending.splice(index,1)[0].reject(error);await tick();};
  const expand=async(c=connection(),items=[])=>{click('toggle');await resolve('/api/ai-connections',{items:[c]});await resolve(base,{items});input('connection_id',c.id);input('operation_id','mock');};
  return{root,state,env,mount,dispose:()=>view?.dispose(),click,input,receiptInput,resolve,reject,expand,calls,pending,errors,toasts,setCurrent:value=>{current=value;}};
}

test('collapsed tool view performs no read, and expansion loads only connections and this run',async()=>{
 const f=fixture();f.mount();assert.equal(f.calls.length,0);assert.doesNotMatch(f.root.innerHTML,/工具连接/);await f.expand();assert.deepEqual(f.calls.map(c=>c.url),['/api/ai-connections',base]);assert.match(f.root.innerHTML,/素材传输与自动收录尚未接入/);
});
test('connection and result descriptions are escaped; unsupported input prevents submission',async()=>{
 const f=fixture();f.mount();await f.expand(connection({name:'<img src=x>',operations:[{id:'mock',name:'<script>',input_schema:{properties:{source:{type:'object',title:'复杂输入'}}},supports:{}}]}),[attempt({results:[{name:'<video onerror=x>'}]})]);assert.match(f.root.innerHTML,/&lt;img/);assert.match(f.root.innerHTML,/&lt;video/);assert.equal(f.root.querySelector('[data-call="submit"]').disabled,true);assert.equal(f.root.querySelector('[data-call="cancel"]'),null);
});
test('explicit submit saves scalar parameters once and protects local writes until acceptance',async()=>{
 const f=fixture();f.mount();await f.expand();f.input('prompt','合成测试',true);f.input('count','3',true);f.click('submit');f.click('submit');const writes=f.calls.filter(c=>c.options.method==='POST');assert.equal(writes.length,1);assert.equal(f.state.aiToolCallBusy,true);assert.equal(writes[0].url,base);assert.deepEqual(JSON.parse(JSON.stringify(writes[0].options.body.parameters)),{prompt:'合成测试',count:3});assert.ok(writes[0].options.body.idempotency_key);await f.resolve(base,attempt());assert.equal(f.state.aiToolCallBusy,false);assert.match(f.root.innerHTML,/工具排队中/);assert.doesNotMatch(f.root.innerHTML,/工具已确认取消/);
});
test('unknown submit preserves original request and parameters across remount without regeneration',async()=>{
 const f=fixture();f.mount();await f.expand();f.input('prompt','第一次参数',true);f.click('submit');const first=f.calls.at(-1).options.body;f.pending.shift().reject(Error('connection lost'));await tick();assert.equal(f.errors.at(-1).unknownResult,true);f.mount();assert.equal(f.calls.length,3);f.input('prompt','等待期间改了参数',true);f.click('reconcile');assert.equal(f.calls.at(-1).options.body.idempotency_key,first.idempotency_key);assert.equal(f.calls.at(-1).options.body.parameters.prompt,'第一次参数');await f.resolve(base,attempt());assert.equal(f.state.aiToolCallBusy,false);
});
test('malformed acceptance does not claim success and can be reconciled with the same key',async()=>{
 const f=fixture();f.mount();await f.expand();f.input('prompt','保留',true);f.click('submit');const key=f.calls.at(-1).options.body.idempotency_key;await f.resolve(base,{});assert.equal(f.errors.at(-1).unknownResult,true);assert.match(f.root.innerHTML,/核对本次提交/);f.click('reconcile');assert.equal(f.calls.at(-1).options.body.idempotency_key,key);await f.resolve(base,attempt({task_id:'f'.repeat(32)}));assert.equal(f.errors.at(-1).unknownResult,true);assert.doesNotMatch(f.root.innerHTML,/本次请求已记录/);
});
test('a new attempt is explicit and gets a new request key after known acceptance',async()=>{
 const f=fixture();f.mount();await f.expand();f.input('prompt','相同参数',true);f.click('submit');const first=f.calls.at(-1).options.body.idempotency_key;await f.resolve(base,attempt({state:'succeeded'}));f.click('new-request');f.click('submit');assert.notEqual(f.calls.at(-1).options.body.idempotency_key,first);await f.resolve(base,attempt({id:'4'.repeat(32)}));
});
test('late records after changing run or project do not paint the new view',async()=>{
 const f=fixture();f.mount();f.click('toggle');f.setCurrent(false);f.root.innerHTML='新任务内容';await f.resolve('/api/ai-connections',{items:[connection()]});await f.resolve(base,{items:[attempt({notice:'旧轮结果'})]});assert.equal(f.root.innerHTML,'新任务内容');assert.equal(f.calls.length,2);
});
test('a late write remains in its original run and clears only its own local busy count',async()=>{
 const f=fixture();f.mount();await f.expand();f.input('prompt','旧轮调用',true);f.click('submit');f.state.projectId='p2';f.root.innerHTML='另一项目';await f.resolve(base,attempt({notice:'旧项目结果'}));assert.equal(f.root.innerHTML,'另一项目');assert.equal(f.state.aiToolCallBusy,false);
});
test('state reconciliation never submits again and cancel-request is not confirmed cancellation',async()=>{
 const f=fixture();f.mount();await f.expand(connection(),[attempt({state:'running',supports:{query:true,lookup:true,cancel:true}})]);f.click('query');assert.equal(f.calls.at(-1).url,base+'/'+attemptId+'/query');await f.resolve(base+'/'+attemptId+'/query',attempt({state:'running',supports:{query:true,cancel:true}}));f.click('cancel');assert.equal(f.calls.at(-1).url,base+'/'+attemptId+'/cancel');await f.resolve(base+'/'+attemptId+'/cancel',attempt({state:'cancel_requested',supports:{query:true,cancel:true}}));assert.match(f.root.innerHTML,/已申请取消/);assert.doesNotMatch(f.root.innerHTML,/工具已确认取消/);assert.equal(f.calls.filter(c=>c.url===base&&c.options.method==='POST').length,0);
});
test('unsaved editor blocks tool submission and leaves the input intact',async()=>{
 const f=fixture();f.mount();await f.expand();f.input('prompt','保留文字',true);f.state.dirty=true;f.click('submit');await tick();assert.equal(f.calls.length,2);assert.match(f.toasts.at(-1)[0],/先保存文稿/);assert.equal(f.root.querySelector('[data-call-parameter="prompt"]').value,'保留文字');
});
test('connection settings save only a credential variable name, with no generation request',async()=>{
 const f=fixture();f.mount();await f.expand();f.click('new-connection');f.input('connection.name','合成工具');f.input('connection.base_url','http://127.0.0.1:12345');f.input('connection.credential_env','SYNTHETIC_KEY');f.click('save-connection');const call=f.calls.at(-1);assert.equal(call.url,'/api/ai-connections');assert.equal(call.options.body.credential_env,'SYNTHETIC_KEY');assert.equal(Object.hasOwn(call.options.body,'token'),false);await f.resolve('/api/ai-connections',connection({checked:false}));await f.resolve('/api/ai-connections',{items:[connection({checked:false})]});await f.resolve(base,{items:[]});assert.equal(f.calls.filter(c=>c.url===base&&c.options.method==='POST').length,0);assert.equal(f.state.aiToolCallBusy,false);
});
test('long attempt histories have an explicit bounded next page and no idle requests',async()=>{
 const f=fixture();f.mount();f.click('toggle');await f.resolve('/api/ai-connections',{items:[connection()]});await f.resolve(base,{items:[attempt()],total:3});
 assert.match(f.root.innerHTML,/已显示 1 \/ 3 条/);f.click('more');assert.equal(f.calls.at(-1).url,base+'?limit=48&offset=1');f.click('more');assert.equal(f.calls.length,3);
 await f.resolve(base+'?limit=48&offset=1',{items:[attempt({id:'4'.repeat(32)}),attempt({id:'5'.repeat(32)})],total:3});
 assert.equal(f.root.querySelector('[data-call="more"]'),null);assert.equal(f.calls.length,3);assert.equal(f.state.aiToolCallBusy,undefined);
});
test('untouched required boolean is submitted as false and completed attempts cannot be queried',async()=>{
 const c=connection();c.operations[0].input_schema={type:'object',properties:{approve:{type:'boolean'}},required:['approve']};
 const f=fixture();f.mount();await f.expand(c,[attempt({state:'succeeded'})]);assert.equal(f.root.querySelector('[data-call="query"]').disabled,true);
 f.click('submit');assert.equal(f.calls.at(-1).options.body.parameters.approve,false);await f.resolve(base,attempt({id:'6'.repeat(32)}));
});
test('an in-flight remount observes acceptance and releases its disabled controls',async()=>{
 const f=fixture();f.mount();await f.expand();f.input('prompt','原参数',true);f.click('submit');f.mount();
 assert.equal(f.root.querySelector('[data-call="submit"]').disabled,true);await f.resolve(base,attempt());
 assert.equal(f.root.querySelector('[data-call="submit"]').disabled,false);assert.equal(f.state.aiToolCallBusy,false);assert.match(f.root.innerHTML,/工具排队中/);
});
test('an uncertain original submit can be reconciled even if the connection is now disabled',async()=>{
 const f=fixture();f.mount();await f.expand();f.input('prompt','原参数',true);f.click('submit');const original=f.calls.at(-1).options.body;
 f.pending.shift().reject(Error('connection lost'));await tick();f.click('refresh');await f.resolve('/api/ai-connections',{items:[connection({enabled:false,checked:false,operations:[]})]});await f.resolve(base,{items:[]});
 assert.equal(f.root.querySelector('[data-call="reconcile"]').disabled,false);f.click('reconcile');assert.equal(f.calls.at(-1).options.body,original);await f.resolve(base,attempt({state:'unknown'}));
 assert.equal(f.state.aiToolCallBusy,false);assert.match(f.root.innerHTML,/结果待核对/);
});
test('queued local acceptance is refreshed without replacing its submission with a lookup',async()=>{
 const f=fixture();f.mount();await f.expand(connection(),[attempt({state:'accepted'})]);
 assert.equal(f.root.querySelector('[data-call="query"]').disabled,true);f.click('query');assert.equal(f.calls.length,2);
 f.click('get');await f.resolve(base+'/'+attemptId,attempt({state:'running'}));
 assert.equal(f.root.querySelector('[data-call="query"]').disabled,false);assert.equal(f.state.aiToolCallBusy,false);
});

const selectionBase=base.replace(/\/calls$/, '/capability-selections');
const selectionId='7'.repeat(32);
const hubConnection=()=>connection({provider:'aihub-interop/1',read_only:true,operations:[],connection_revision:'d'.repeat(64)});
const selection=(extra={})=>({id:selectionId,task_id:task.id,run_id:run.id,name:'合成视频选型',key:'fixture.video',connection_name:'曜核',declaration_sha256:'e'.repeat(64),execution_allowed:false,...extra});
async function expandHub(f){f.mount();f.click('toggle');await f.resolve('/api/ai-connections',{items:[hubConnection()]});await f.resolve(base,{items:[]});f.input('connection_id',connectionId);}

test('hub metadata remains opt-in with no dispatch controls or automatic catalogue scan',async()=>{
 const f=fixture();await expandHub(f);assert.equal(f.calls.length,2);assert.match(f.root.innerHTML,/真正派单需要单独的来源授权/);assert.equal(f.root.querySelector('[data-call="submit"]'),null);assert.equal(f.root.querySelector('[data-call="operation_id"]'),null);
 const input=f.root.querySelector('[data-call-field="capability_id"]');f.input('capability_id','c'.repeat(32));assert.equal(f.root.querySelector('[data-call-field="capability_id"]'),input);assert.equal(f.root.querySelector('[data-call="save-selection"]').disabled,false);assert.equal(f.calls.length,2);
});
test('saving a hub selection binds the checked revision and clears local busy without executing',async()=>{
 const f=fixture();await expandHub(f);f.input('capability_id','c'.repeat(32));f.click('save-selection');const call=f.calls.at(-1);
 assert.equal(call.url,selectionBase);assert.equal(call.options.body.connection_revision,1);assert.equal(call.options.body.source_connection_revision,'d'.repeat(64));assert.equal(f.state.aiToolCallBusy,true);
 await f.resolve(selectionBase,selection());assert.equal(f.state.aiToolCallBusy,false);assert.match(f.root.innerHTML,/未执行生成/);assert.equal(f.calls.filter(c=>c.url===base&&c.options.method==='POST').length,0);
});
test('lost selection response uses the same request even after remount and parameter change',async()=>{
 const f=fixture();await expandHub(f);f.input('capability_id','c'.repeat(32));f.click('save-selection');const body=f.calls.at(-1).options.body;
 f.pending.shift().reject(Error('lost response'));await tick();f.mount();f.input('capability_id','different');f.click('reconcile-selection');assert.equal(f.calls.at(-1).options.body,body);
 await f.resolve(selectionBase,selection());assert.equal(f.state.aiToolCallBusy,false);assert.equal(f.root.querySelector('[data-call="reconcile-selection"]'),null);
});
test('explicit selection reads and verifies do not replace the frozen record with new evidence',async()=>{
 const f=fixture();await expandHub(f);f.click('load-selections');await f.resolve(selectionBase,{items:[selection()]});f.click('verify-selection');
 assert.equal(f.calls.at(-1).url,selectionBase+'/'+selectionId+'/verify');await f.resolve(selectionBase+'/'+selectionId+'/verify',{id:selectionId,task_id:task.id,run_id:run.id,declaration_matches:true,execution_allowed:false});
 assert.match(f.root.innerHTML,/不表示执行版本已锁定/);assert.match(f.root.innerHTML,/合成视频选型/);assert.equal(f.calls.length,4);
});
test('late hub selection writes never paint another project and release the original busy guard',async()=>{
 const f=fixture();await expandHub(f);f.input('capability_id','c'.repeat(32));f.click('save-selection');f.state.projectId='other';f.root.innerHTML='其他项目';
 await f.resolve(selectionBase,selection());assert.equal(f.root.innerHTML,'其他项目');assert.equal(f.state.aiToolCallBusy,false);
});
test('connection type is saved explicitly without assuming an installed service address',async()=>{
 const f=fixture();f.mount();await f.expand();f.click('new-connection');f.input('connection.provider','aihub-interop/1');f.input('connection.name','曜核');f.input('connection.base_url','http://127.0.0.1:18765');f.click('save-connection');
 assert.equal(f.calls.at(-1).options.body.provider,'aihub-interop/1');await f.resolve('/api/ai-connections',hubConnection());await f.resolve('/api/ai-connections',{items:[hubConnection()]});await f.resolve(base,{items:[]});assert.equal(f.calls.length,5);
});
test('a known selection is reused until the user explicitly prepares a new one',async()=>{
 const f=fixture();await expandHub(f);f.input('capability_id','c'.repeat(32));f.click('save-selection');const first=f.calls.at(-1).options.body.idempotency_key;await f.resolve(selectionBase,selection());
 f.click('save-selection');assert.equal(f.calls.at(-1).options.body.idempotency_key,first);await f.resolve(selectionBase,selection());
 f.click('new-selection');f.click('save-selection');assert.notEqual(f.calls.at(-1).options.body.idempotency_key,first);await f.resolve(selectionBase,selection({id:'8'.repeat(32)}));assert.match(f.root.innerHTML,/本轮选型 · 2/);
});

const receiptBase=base+'/'+attemptId+'/receipts',candidateBase=base.replace(/\/calls$/, '/candidates');
const resultId='d'.repeat(64),otherResultId='e'.repeat(64),deliveryId='4'.repeat(32);
const toolResult=(extra={})=>({result_id:resultId,name:'合成成果',sha256:'c'.repeat(64),...extra});
const delivery=(extra={})=>({id:deliveryId,task_id:task.id,run_id:run.id,attempt_id:attemptId,result_id:resultId,state:'completed',relative_path:'candidate.mp4',receipt_id:'5'.repeat(32),artifact_id:'6'.repeat(32),observed_sha256:'c'.repeat(64),source_verification:'sha256',...extra});
async function openReceipt(f,results=[toolResult()]){
 f.mount();await f.expand(connection(),[attempt({state:'succeeded',results})]);f.click('receipt-open');
}
async function loadReceipt(f,links=[]){
 f.click('receipt-load');await f.resolve(receiptBase,{items:links});await f.resolve(candidateBase,{files:[{relative_path:'candidate.mp4'},{relative_path:'different.mp4'}]});
}
function chooseReceipt(f){f.receiptInput('result',resultId);f.receiptInput('path','candidate.mp4');}
function noGeneration(f){assert.equal(f.calls.filter(c=>c.url===base&&c.options.method==='POST').length,0);}

test('receipt expansion is local; only explicit refresh reads original run candidates and links',async()=>{
 const f=fixture();await openReceipt(f);assert.equal(f.calls.length,2);assert.match(f.root.innerHTML,/点击刷新读取本轮文件与历史关联/);
 f.click('receipt-open');f.click('receipt-open');assert.equal(f.calls.length,2);f.click('receipt-load');
 assert.deepEqual(f.calls.slice(2).map(c=>c.url),[receiptBase,candidateBase]);assert.equal(f.state.aiToolCallBusy,true);f.click('receipt-load');assert.equal(f.calls.length,4);
 await f.resolve(receiptBase,{items:[delivery(),delivery({id:'8'.repeat(32),run_id:'b'.repeat(32),relative_path:'wrong-run.mp4'}),delivery({id:'9'.repeat(32),task_id:'f'.repeat(32),relative_path:'wrong-task.mp4'})]});
 await f.resolve(candidateBase,{files:[{relative_path:'candidate.mp4'}]});assert.match(f.root.innerHTML,/已关联/);assert.doesNotMatch(f.root.innerHTML,/wrong-run|wrong-task/);assert.equal(f.state.aiToolCallBusy,false);await tick();assert.equal(f.calls.length,4);noGeneration(f);
});

test('a result without a digest needs manual confirmation and retains unverified provenance',async()=>{
 const f=fixture();await openReceipt(f,[toolResult({sha256:undefined})]);await loadReceipt(f);chooseReceipt(f);
 assert.equal(f.root.querySelector('[data-call="receipt-submit"]').disabled,true);f.click('receipt-submit');assert.equal(f.calls.length,4);
 f.receiptInput('confirmed',true);f.click('receipt-submit');assert.equal(f.calls.at(-1).options.body.confirmed_unverified_source,true);
 await f.resolve(receiptBase,delivery({source_verification:'manual_source_unverified'}));assert.match(f.root.innerHTML,/手动关联，来源未核验/);assert.match(f.root.innerHTML,/审核采用仍需确认/);noGeneration(f);
});

test('unknown receipt preserves its original body and key across remount and changed selection',async()=>{
 const f=fixture();await openReceipt(f,[toolResult(),toolResult({result_id:otherResultId})]);await loadReceipt(f);chooseReceipt(f);f.click('receipt-submit');const original=f.calls.at(-1).options.body;
 await f.reject(receiptBase,Error('response lost'));assert.equal(f.errors.at(-1).unknownResult,true);f.mount();assert.equal(f.calls.length,5);
 f.receiptInput('result',otherResultId);f.receiptInput('path','different.mp4');assert.equal(f.root.querySelector('[data-call="receipt-new"]').disabled,true);f.click('receipt-new');assert.equal(f.calls.length,5);
 f.click('receipt-reconcile');assert.equal(f.calls.at(-1).options.body,original);assert.equal(f.calls.at(-1).options.body.result_id,resultId);assert.equal(f.calls.at(-1).options.body.relative_path,'candidate.mp4');assert.ok(original.idempotency_key);
 await f.resolve(receiptBase,delivery());assert.equal(f.state.aiToolCallBusy,false);assert.equal(f.root.querySelector('[data-call="receipt-reconcile"]'),null);noGeneration(f);
});

test('wrong scope and malformed receipt responses remain uncertain without claiming success',async()=>{
 const invalid=[{},delivery({id:'bad'}),delivery({task_id:'f'.repeat(32)}),delivery({run_id:'b'.repeat(32)}),delivery({attempt_id:'9'.repeat(32)}),delivery({result_id:otherResultId}),delivery({state:'succeeded'}),delivery({receipt_id:'bad'}),delivery({artifact_id:'bad'}),delivery({observed_sha256:'bad'})];
 for(const value of invalid){
  const f=fixture();await openReceipt(f);await loadReceipt(f);chooseReceipt(f);f.click('receipt-submit');const original=f.calls.at(-1).options.body;await f.resolve(receiptBase,value);
  assert.equal(f.errors.at(-1)?.unknownResult,true,JSON.stringify(value));assert.doesNotMatch(f.root.innerHTML,/已关联原成果|<strong>已关联<\/strong>/);assert.equal(f.root.querySelector('[data-call="receipt-new"]').disabled,true);
  f.click('receipt-reconcile');assert.equal(f.calls.at(-1).options.body,original);await f.resolve(receiptBase,delivery());assert.equal(f.state.aiToolCallBusy,false);noGeneration(f);
 }
});

test('late receipt writes do not paint another project and release the original write guard',async()=>{
 const f=fixture();await openReceipt(f);await loadReceipt(f);chooseReceipt(f);f.click('receipt-submit');f.click('receipt-submit');assert.equal(f.calls.length,5);
 f.state.projectId='p2';f.root.innerHTML='另一项目内容';await f.resolve(receiptBase,delivery());assert.equal(f.root.innerHTML,'另一项目内容');assert.equal(f.state.aiToolCallBusy,false);assert.equal(f.errors.length,0);noGeneration(f);
});

test('pending receipts resume their original association without creating a receipt or generation',async()=>{
 const f=fixture();await openReceipt(f);await loadReceipt(f,[delivery({state:'pending'})]);f.click('receipt-resume');const url=receiptBase+'/'+deliveryId+'/resume';
 assert.equal(f.calls.at(-1).url,url);assert.equal(f.calls.at(-1).options.method,'POST');assert.deepEqual(JSON.parse(JSON.stringify(f.calls.at(-1).options.body)),{});f.click('receipt-resume');assert.equal(f.calls.length,5);
 await f.resolve(url,delivery());assert.match(f.root.innerHTML,/<strong>已关联<\/strong>/);assert.equal(f.root.querySelector('[data-call="receipt-resume"]'),null);assert.equal(f.calls.filter(c=>c.url===receiptBase&&c.options.method==='POST').length,0);assert.equal(f.state.aiToolCallBusy,false);noGeneration(f);
});

test('unsaved edits block initial and uncertain receipt submissions with original request intact',async()=>{
 const f=fixture();await openReceipt(f);await loadReceipt(f);chooseReceipt(f);f.state.dirty=true;f.click('receipt-submit');await tick();assert.equal(f.calls.length,4);assert.match(f.toasts.at(-1)[0],/先保存文稿/);
 f.state.dirty=false;f.click('receipt-submit');const original=f.calls.at(-1).options.body;await f.reject(receiptBase,Error('response lost'));
 f.state.dirty=true;f.click('receipt-reconcile');await tick();assert.equal(f.calls.length,5);assert.match(f.toasts.at(-1)[0],/先保存文稿/);f.state.dirty=false;f.click('receipt-reconcile');assert.equal(f.calls.at(-1).options.body,original);await f.resolve(receiptBase,delivery());noGeneration(f);
});

test('unsaved edits also block resuming a pending receipt',async()=>{
 const f=fixture();await openReceipt(f);await loadReceipt(f,[delivery({state:'pending'})]);f.state.dirty=true;f.click('receipt-resume');await tick();
 assert.equal(f.calls.length,4);assert.match(f.toasts.at(-1)[0],/先保存文稿/);assert.equal(f.state.aiToolCallBusy,false);noGeneration(f);
});

test('a resumed response must identify the original pending association',async()=>{
 const f=fixture();await openReceipt(f);await loadReceipt(f,[delivery({state:'pending'})]);f.click('receipt-resume');await f.resolve(receiptBase+'/'+deliveryId+'/resume',delivery({id:'9'.repeat(32)}));
 assert.equal(f.errors.at(-1)?.unknownResult,true);assert.doesNotMatch(f.root.innerHTML,/已关联原成果|<strong>已关联<\/strong>/);assert.equal(f.state.aiToolCallBusy,false);noGeneration(f);
});

test('known receipt errors allow an explicit new request; uncertain errors keep the original key',async()=>{
 const f=fixture();await openReceipt(f);await loadReceipt(f);chooseReceipt(f);f.click('receipt-submit');const first=f.calls.at(-1).options.body.idempotency_key;
 await f.reject(receiptBase,Object.assign(Error('digest mismatch'),{status:409}));assert.equal(f.root.querySelector('[data-call="receipt-new"]').disabled,false);f.click('receipt-submit');assert.equal(f.calls.at(-1).options.body.idempotency_key,first);
 await f.reject(receiptBase,Object.assign(Error('digest mismatch'),{status:409}));f.click('receipt-new');f.click('receipt-submit');const second=f.calls.at(-1).options.body.idempotency_key;assert.notEqual(second,first);
 await f.reject(receiptBase,Object.assign(Error('uncertain'),{status:503,unknownResult:true}));assert.equal(f.root.querySelector('[data-call="receipt-new"]').disabled,true);f.click('receipt-new');f.mount();f.click('receipt-reconcile');assert.equal(f.calls.at(-1).options.body.idempotency_key,second);await f.resolve(receiptBase,delivery());noGeneration(f);
});

test('completed receipt refreshes the original parent once and keeps busy until refresh settles',async()=>{
 const f=fixture(),received=[];let finish;
 f.env.onReceipt=value=>{received.push(value);return new Promise(resolve=>{finish=resolve;});};
 await openReceipt(f);await loadReceipt(f);chooseReceipt(f);f.click('receipt-submit');const value=delivery();await f.resolve(receiptBase,value);
 assert.equal(received.length,1);assert.equal(received[0],value);assert.equal(f.state.aiToolCallBusy,true);f.click('receipt-submit');assert.equal(f.calls.length,5);
 finish();await tick();assert.equal(f.state.aiToolCallBusy,false);assert.match(f.root.innerHTML,/<strong>已关联<\/strong>/);assert.equal(f.errors.length,0);noGeneration(f);
});

test('parent refresh failure reports separately and preserves completed receipt across remount',async()=>{
 const f=fixture(),failure=Error('parent refresh failed');let refreshed=0;
 f.env.onReceipt=async()=>{refreshed++;throw failure;};await openReceipt(f);await loadReceipt(f);chooseReceipt(f);f.click('receipt-submit');await f.resolve(receiptBase,delivery());
 assert.equal(refreshed,1);assert.equal(f.errors.length,1);assert.equal(f.errors[0],failure);assert.equal(f.errors[0].unknownResult,undefined);assert.match(f.root.innerHTML,/成果已关联；列表刷新失败/);assert.match(f.root.innerHTML,/<strong>已关联<\/strong>/);
 assert.equal(f.root.querySelector('[data-call="receipt-reconcile"]'),null);assert.equal(f.state.aiToolCallBusy,false);f.mount();assert.equal(f.calls.length,5);assert.match(f.root.innerHTML,/<strong>已关联<\/strong>/);assert.equal(f.root.querySelector('[data-call="receipt-reconcile"]'),null);noGeneration(f);
});

test('pending receipt does not refresh parent until recovery confirms completed association',async()=>{
 const f=fixture(),received=[];f.env.onReceipt=async value=>{received.push(value);};await openReceipt(f);await loadReceipt(f);chooseReceipt(f);f.click('receipt-submit');await f.resolve(receiptBase,delivery({state:'pending'}));
 assert.equal(received.length,0);assert.match(f.root.innerHTML,/<strong>待核对<\/strong>/);f.click('receipt-resume');const value=delivery();await f.resolve(receiptBase+'/'+deliveryId+'/resume',value);assert.equal(received.length,1);assert.equal(received[0],value);assert.equal(f.state.aiToolCallBusy,false);noGeneration(f);
});

test('late completed receipt never refreshes a different project, run view, or disposed page',async()=>{
 for(const leave of [f=>{f.state.projectId='p2';},f=>{f.setCurrent(false);},f=>{f.dispose();}]){
  const f=fixture(),received=[];f.env.onReceipt=async value=>{received.push(value);};await openReceipt(f);await loadReceipt(f);chooseReceipt(f);f.click('receipt-submit');
  leave(f);f.root.innerHTML='新页面内容';await f.resolve(receiptBase,delivery());assert.equal(received.length,0);assert.equal(f.root.innerHTML,'新页面内容');assert.equal(f.state.aiToolCallBusy,false);noGeneration(f);
 }
});
