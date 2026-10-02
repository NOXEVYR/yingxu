'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../frontend/ai-collaboration.js'),'utf8');
const escapeHtml=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
const tick=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
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
  querySelectorAll(selector){const keys=selector.split(',').map(s=>s.trim().slice(6,-1).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()));return this.controls.filter(c=>keys.some(k=>c.dataset[k]!==undefined)).concat(...this.children.map(x=>x.querySelectorAll(selector)));}
  querySelector(selector){if(selector==='.ai-collaboration')return this.children.find(x=>x.className==='ai-collaboration')||null;if(selector==='[data-ai-tool-calls]' && this._html.includes('data-ai-tool-calls'))return this;const matches=[...selector.matchAll(/\[([^=\]]+)(?:="([^"]*)")?\]/g)];return this.controls.find(c=>matches.every(m=>m[1].startsWith('data-')?c.dataset[m[1].slice(5).replace(/-([a-z])/g,(_,x)=>x.toUpperCase())]!==undefined && (m[2]===undefined || c.dataset[m[1].slice(5).replace(/-([a-z])/g,(_,x)=>x.toUpperCase())]===m[2]):false))||this.children.map(x=>x.querySelector(selector)).find(Boolean)||null;}
}
function task(id='11111111111111111111111111111111',project='p1',extra={}){return {id,project_id:project,title:'片段制作',kind:'video',goal:'测试目标',acceptance:['逐项验收'],revision:1,status:'active',runs:[{id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',task_id:id,run_number:1,status:'frozen',client_id:'codex',conversation_id:'原会话',input_snapshot:{goal:'测试目标',input_item_ids:['i1'],skill_pins:[{collection_id:'c1',version:'v1'}]},directories:{generated:{folder_id:'fg'},references:{folder_id:'fr'}}}],artifacts:[{id:'cccccccccccccccccccccccccccccccc',run_id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',item_id:'i1',name:'片段.mp4',role:'result',verification_status:'verified',review_status:'pending',revision:1}],receipts:[{id:'33333333333333333333333333333333',run_id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',state:'completed'}],...extra};}
function fixture(tools=false){
 const calls=[],pending=[],errors=[],toasts=[],opened=[],folders=[],copies=[],toolMounts=[],state={projectId:'p1',section:'context',tabs:[],bootstrap:{capabilities:{ai_tool_calls:tools}}},root=new Node('root');root.connected=true;
 let uuid=0;const context=vm.createContext({window:{crypto:{randomUUID:()=> 'request-uuid-'+(++uuid)},YingXuToolCalls:tools?{mount:(_root,env)=>{toolMounts.push(env);return {dispose(){}};}}:undefined},document:{createElement:tag=>new Node(tag)}});vm.runInContext(source,context);
 const env={state,api:(url,options={})=>{calls.push({url,options});return new Promise((resolve,reject)=>pending.push({url,resolve,reject}));},escapeHtml,report:e=>errors.push(e),toast:(...x)=>toasts.push(x),copyText:async x=>copies.push(x),openItem:async id=>opened.push(id),openFolder:async p=>folders.push(p),hasPendingEdits:()=>state.tabs.some(t=>t.dirty),render:()=>{throw Error('Must not remount the workspace editor');}};
 const mount=()=>context.window.YingXuCollaboration.mount(root,env),node=()=>root.querySelector('.ai-collaboration');
 const click=(action,attrs='')=>{const target=node().querySelector(`[data-ai="${action}"]${attrs}`);assert.ok(target,'Missing '+action);node().onclick({target,stopPropagation(){}});};
 const input=(name,value)=>{const target=node().querySelector(`[data-ai-field="${name}"]`);assert.ok(target,'Missing field '+name);target.value=value;node().oninput({target});};
 const resolve=async(url,value)=>{
   // Ordinary fixtures model the full production write responses; empty malformed responses stay empty.
   const runMatch=url.match(/^\/api\/ai-tasks\/([^/]+)\/runs\/([^/]+)\/(handoff|receive)$/);
   if(/\/runs$/.test(url) && value?.id)value={...task().runs[0],task_id:url.split('/')[3],...value};
   if(runMatch && value?.snapshot_id)value={id:value.snapshot_id,task_id:runMatch[1],run_id:runMatch[2],mode:'full',...value};
   if(runMatch && value?.id && runMatch[3]==='receive')value={task_id:runMatch[1],run_id:runMatch[2],...value};
   const index=pending.findIndex(x=>x.url===url);assert.ok(index>=0,'Missing request '+url);pending.splice(index,1)[0].resolve(value);await tick();
 };
 const initialize=async(t=task())=>{await resolve('/api/ai-tasks?project_id='+state.projectId,{tasks:[t]});await resolve('/api/skill-collections?project='+state.projectId,{collections:[{id:'c1',name:'技能',version:'v1',bound_version:'v1',versions:[{version:'v1'},{version:'v2'}]}]});await resolve(`/api/items?project=${state.projectId}&limit=48&offset=0&sort=updated`,{items:[{id:'i1',name:'片段.mp4'}],total:1});await resolve('/api/ai-tasks/'+t.id,t);};
 return {root,state,env,mount,node,click,input,resolve,initialize,calls,pending,errors,toasts,opened,folders,copies,toolMounts,collaboration:context.window.YingXuCollaboration};
}

test('contextual navigation reuses task actions and keeps busy, project and disposal guards',async()=>{
 const s=fixture();let updates=0;s.env.onNavigationChange=()=>updates++;const view=s.mount();await s.initialize();
 const nav=s.collaboration.navigation(s.state);assert.equal(nav.tasks[0].id,task().id);assert.equal(nav.selected,task().id);assert.ok(updates>0);
 const before=s.calls.length;s.state.aiToolCallBusy=true;
 await s.collaboration.navigate(s.state,'create');assert.equal(s.collaboration.navigation(s.state).creating,false);assert.equal(s.calls.length,before);
 s.state.aiToolCallBusy=false;await s.collaboration.navigate(s.state,'select','unknown-task');await s.collaboration.navigate(s.state,'freeze',task().id);
 assert.equal(s.calls.length,before,'The navigation cannot invoke unrelated writes or unknown task IDs');
 await s.collaboration.navigate(s.state,'create');assert.equal(s.collaboration.navigation(s.state).creating,true);assert.equal(s.calls.length,before);
 s.input('title','未保存新任务');view.dispose();await s.collaboration.navigate(s.state,'reload');assert.equal(s.calls.length,before);
 s.state.projectId='p2';assert.equal(s.collaboration.navigation(s.state),null);await s.collaboration.navigate(s.state,'create');assert.equal(s.calls.length,before);
 s.state.projectId='p1';s.state.section='assets';await s.collaboration.navigate(s.state,'reload');assert.equal(s.calls.length,before);
});

test('completed tool receipt refreshes only original task and exposes its existing artifacts',async()=>{
 const s=fixture(true);s.mount();await s.initialize();const original=s.toolMounts.at(-1),before=s.calls.length;
 s.state.aiToolCallBusy=true;original.onBusyChanged();assert.equal(s.node().querySelector('[data-ai="preview"]').disabled,true);
 s.input('conversation_id','忙碌时不允许更改');const refresh=original.onReceipt();assert.equal(s.calls.length,before+1);assert.equal(s.calls.at(-1).url,'/api/ai-tasks/'+original.task.id);
 const updated=task();updated.artifacts[0].name='原轮新收配音.mp3';await s.resolve(s.calls.at(-1).url,updated);await refresh;
 assert.match(s.node().innerHTML,/原轮新收配音.mp3/);assert.equal(s.state.aiToolCallBusy,true);assert.equal(s.calls.length,before+1);
 assert.equal(s.node().querySelector('[data-ai="preview"]').disabled,true);
 s.state.aiToolCallBusy=false;original.onBusyChanged();assert.equal(s.node().querySelector('[data-ai="preview"]').disabled,false);
 assert.match(s.node().innerHTML,/value="原会话"/);assert.equal(s.calls.length,before+1);
});

test('original busy release unlocks a fully remounted parent without another request',async()=>{
 const s=fixture(true);s.mount();await s.initialize();const original=s.toolMounts.at(-1);
 s.state.aiToolCallBusy=true;original.onBusyChanged();s.mount();await s.initialize();
 const before=s.calls.length;assert.equal(s.node().querySelector('[data-ai="next"]').disabled,true);
 assert.equal(s.node().querySelector('[data-ai-field="conversation_id"]').disabled,true);
 s.state.aiToolCallBusy=false;original.onBusyChanged();
 assert.equal(s.node().querySelector('[data-ai="next"]').disabled,false);
 assert.equal(s.node().querySelector('[data-ai-field="conversation_id"]').disabled,false);
 assert.equal(s.calls.length,before);assert.equal(s.calls.filter(c=>c.options.method==='POST').length,0);
});

test('previous tool receipt callback cannot refresh a different project or disposed parent',async()=>{
 const s=fixture(true);const view=s.mount();await s.initialize();const original=s.toolMounts.at(-1),before=s.calls.length;
 s.state.projectId='p2';await original.onReceipt();assert.equal(s.calls.length,before);
 s.node().innerHTML='其他项目页面';original.onBusyChanged();assert.equal(s.node().innerHTML,'其他项目页面');
 s.state.projectId='p1';view.dispose();await original.onReceipt();assert.equal(s.calls.length,before);
 original.onBusyChanged();assert.equal(s.node().innerHTML,'其他项目页面');
});

test('late parent refresh after receipt does not paint a different project',async()=>{
 const s=fixture(true);s.mount();await s.initialize();const original=s.toolMounts.at(-1);const refresh=original.onReceipt(),url=s.calls.at(-1).url;
 s.state.projectId='p2';s.node().innerHTML='另一个项目的内容';const updated=task();updated.artifacts[0].name='旧任务配音';await s.resolve(url,updated);await refresh;
 assert.equal(s.node().innerHTML,'另一个项目的内容');
});
test('actual module escapes all task/artifact/receipt content and previews only through existing host',async()=>{
 const s=fixture();s.mount();const t=task('11111111111111111111111111111111','p1',{title:'<img src=x onerror=evil()>',artifacts:[{id:'cccccccccccccccccccccccccccccccc',run_id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',item_id:'i1',name:'<script>evil()</script>',verification_status:'verified'}]});await s.initialize(t);
 assert.match(s.node().innerHTML,/&lt;img/);assert.match(s.node().innerHTML,/&lt;script/);assert.doesNotMatch(s.node().innerHTML,/<(?:video|audio|iframe|script|img)\b/);
 s.click('preview');await tick();assert.deepEqual(s.opened,['i1']);s.click('folder');await tick();assert.deepEqual(JSON.parse(JSON.stringify(s.folders)),[{project_id:'p1',category:'generated',folder_id:'fg'}]);assert.equal(s.calls.length,4);
});
test('new session uses full handoff and acknowledgement is explicit after copying',async()=>{
 const s=fixture();s.mount();await s.initialize();s.click('new-session');s.input('conversation_id','新聊天');s.click('handoff');s.click('handoff');assert.equal(s.calls.length,5);assert.equal(s.state.aiCollaborationBusy,true);assert.deepEqual(JSON.parse(JSON.stringify(s.calls.at(-1).options.body)),{client_id:'codex',conversation_id:'新聊天',force_full:true});
 await s.resolve('/api/ai-tasks/11111111111111111111111111111111/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/handoff',{snapshot_id:'66666666666666666666666666666666',mode:'full',prompt:'<script>交接</script>'});assert.equal(s.state.aiCollaborationBusy,false);s.click('copy');await tick();assert.deepEqual(s.copies,['<script>交接</script>']);assert.equal(s.calls.length,5);
 s.click('ack');assert.equal(s.calls.at(-1).url,'/api/ai-tasks/11111111111111111111111111111111/handoffs/66666666666666666666666666666666/ack');await s.resolve('/api/ai-tasks/11111111111111111111111111111111/handoffs/66666666666666666666666666666666/ack',{acknowledged:true});await s.resolve('/api/ai-tasks/11111111111111111111111111111111',task());assert.equal(s.node().querySelector('[data-ai="ack"]').disabled,true);
});
test('late project read and write cannot contaminate newly mounted project',async()=>{
 const s=fixture();s.mount();await s.initialize();s.click('handoff');s.state.projectId='p2';s.mount();await s.initialize(task('22222222222222222222222222222222','p2',{title:'新项目任务'}));await s.resolve('/api/ai-tasks/11111111111111111111111111111111/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/handoff',{snapshot_id:'old',mode:'full',prompt:'旧项目内容'});
 assert.match(s.node().innerHTML,/新项目任务/);assert.doesNotMatch(s.node().innerHTML,/旧项目内容/);assert.equal(s.state.aiCollaborationBusy,false);assert.equal(s.copies.length,0);assert.equal(s.errors.length,0);
});
test('busy protection persists across remount and does not discard draft',async()=>{
 const s=fixture();s.mount();await s.initialize();s.input('conversation_id','草稿会话');s.click('handoff');s.mount();await s.initialize();assert.equal(s.state.aiCollaborationBusy,true);s.click('handoff');assert.equal(s.calls.filter(x=>x.options.method==='POST').length,1);
 await s.resolve('/api/ai-tasks/11111111111111111111111111111111/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/handoff',{snapshot_id:'s',prompt:'迟到结果'});assert.equal(s.state.aiCollaborationBusy,false);assert.equal(s.node().querySelector('[data-ai="reload"]').disabled,false);s.click('reload');await s.initialize();assert.match(s.node().innerHTML,/value="草稿会话"/);
});
test('freeze refuses unsaved editor and submits fixed selected versions once',async()=>{
 const s=fixture();s.mount();await s.initialize(task('11111111111111111111111111111111','p1',{runs:[],artifacts:[]}));s.input('conversation_id','会话');s.state.tabs.push({dirty:true});s.click('freeze');assert.equal(s.calls.length,4);assert.match(s.toasts[0][0],/保存文稿/);s.state.tabs[0].dirty=false;s.click('freeze');s.click('freeze');assert.equal(s.calls.length,5);const body=JSON.parse(JSON.stringify(s.calls.at(-1).options.body));assert.deepEqual(body.skill_pins,[{collection_id:'c1',version:'v1'}]);assert.equal(body.expected_revision,1);assert.equal(s.state.aiCollaborationBusy,true);
 await s.resolve('/api/ai-tasks/11111111111111111111111111111111/runs',{id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'});await s.resolve('/api/ai-tasks/11111111111111111111111111111111',task());assert.equal(s.state.aiCollaborationBusy,false);
});
test('receipts remain readable after fresh module session and reviews bind artifact revision',async()=>{
 const s=fixture();s.mount();await s.initialize();s.click('receipt');await s.resolve('/api/ai-tasks/11111111111111111111111111111111/receipts/33333333333333333333333333333333',{id:'33333333333333333333333333333333',run_id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',state:'partial',results:[{status:'error',error:'<img src=x>',item_id:'i1'}]});assert.match(s.node().innerHTML,/&lt;img/);assert.match(s.node().innerHTML,/部分成功/);
 const notes=s.node().querySelector('[data-ai-review-notes="cccccccccccccccccccccccccccccccc"]');notes.value='需要修正节奏';s.node().oninput({target:notes});s.click('review','[data-decision="needs_revision"]');s.click('review','[data-decision="needs_revision"]');const call=s.calls.at(-1);assert.equal(call.options.method,'PATCH');assert.deepEqual(JSON.parse(JSON.stringify(call.options.body)),{decision:'needs_revision',notes:'需要修正节奏',expected_revision:1});await s.resolve(call.url,{});await s.resolve('/api/ai-tasks/11111111111111111111111111111111',task());assert.equal(s.state.aiCollaborationBusy,false);
});
test('late run candidates after switching round never appear in the new round',async()=>{
 const s=fixture();s.mount();const t=task();t.runs.push({...t.runs[0],id:'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',run_number:2});await s.initialize(t);s.click('candidates');s.input('run_id','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');await s.resolve('/api/ai-tasks/11111111111111111111111111111111/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',t.runs[0]);await s.resolve('/api/ai-tasks/11111111111111111111111111111111/runs/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/candidates',{files:[{relative_path:'wrong-round.mp4',size:2}],errors:[]});assert.doesNotMatch(s.node().innerHTML,/wrong-round/);assert.equal(s.pending.length,0);
});
test('empty project provides creation and a failed receipt retry reuses its key',async()=>{
 const s=fixture();s.mount();await s.resolve('/api/ai-tasks?project_id=p1',{tasks:[]});await s.resolve('/api/skill-collections?project=p1',{collections:[]});await s.resolve('/api/items?project=p1&limit=48&offset=0&sort=updated',{items:[],total:0});assert.match(s.node().innerHTML,/创建第一个任务/);s.click('create');s.input('title','新任务');s.input('goal','完成片段');s.click('save-create');s.click('save-create');assert.equal(s.calls.filter(x=>x.url==='/api/ai-tasks').length,1);await s.resolve('/api/ai-tasks',task());await s.resolve('/api/ai-tasks/11111111111111111111111111111111',task());
 const control=s.node().querySelector('[data-ai-receive="i1"]'); // No indexed items in this fixture: use directory candidates.
 assert.equal(control,null);s.click('candidates');await s.resolve('/api/ai-tasks/11111111111111111111111111111111/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/candidates',{files:[{relative_path:'片段.mp4',size:1}],errors:[]});const f=s.node().querySelector('[data-ai-receive-path="片段.mp4"]');f.checked=true;s.node().onchange({target:f});s.click('receive');const first=s.calls.at(-1);s.pending.shift().reject(Error('response lost'));await tick();s.click('receive');assert.equal(s.calls.at(-1).options.body.idempotency_key,first.options.body.idempotency_key);await s.resolve(first.url,{id:'77777777777777777777777777777777',state:'completed',results:[{status:'verified',item_id:'i1',sha256:'a'.repeat(64)}]});await s.resolve('/api/ai-tasks/11111111111111111111111111111111',task());assert.match(s.node().innerHTML,/已核验/);assert.equal(s.state.aiCollaborationBusy,false);
});
test('descending backend runs select latest and a late handoff cannot bind an edited session',async()=>{
 const s=fixture();s.mount();const t=task();t.runs.unshift({...t.runs[0],id:'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',run_number:2,input_snapshot:{goal:'最新目标',inputs:[{item_id:'i1',name:'参考.mp4',verification:'metadata_only'}],skill_pins:[{collection_id:'c1',version:'v2',name:'新技能'}]}});await s.initialize(t);assert.match(s.node().innerHTML,/最新目标/);assert.match(s.node().innerHTML,/未核验内容/);s.click('handoff');assert.equal(s.calls.at(-1).url,'/api/ai-tasks/11111111111111111111111111111111/runs/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/handoff');s.input('conversation_id','另一个会话');await s.resolve(s.calls.at(-1).url,{snapshot_id:'h',prompt:'不能交给新会话'});assert.doesNotMatch(s.node().innerHTML,/不能交给新会话/);assert.equal(s.node().querySelector('[data-ai="ack"]'),null);
});
test('unselected favorite version choice is retained when selecting and freezing',async()=>{
 const s=fixture();s.mount();await s.initialize(task('11111111111111111111111111111111','p1',{runs:[],artifacts:[]}));const pin=s.node().querySelector('[data-ai-pin="c1"]');pin.checked=false;s.node().onchange({target:pin});const version=s.node().querySelector('[data-ai-version="c1"]');version.value='v2';s.node().onchange({target:version});pin.checked=true;s.node().onchange({target:pin});s.input('conversation_id','测试收藏');s.click('freeze');assert.equal(s.calls.at(-1).options.body.skill_pins[0].version,'v2');await s.resolve('/api/ai-tasks/11111111111111111111111111111111/runs',{id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'});await s.resolve('/api/ai-tasks/11111111111111111111111111111111',task());
});
test('a known partial receipt retries only failed files with a new operation key',async()=>{
 const s=fixture();s.mount();await s.initialize();s.click('candidates');await s.resolve('/api/ai-tasks/11111111111111111111111111111111/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/candidates',{files:[{relative_path:'ok.mp4',size:1},{relative_path:'failed.mp4',size:1}],errors:[]});for(const name of ['ok.mp4','failed.mp4']){const f=s.node().querySelector(`[data-ai-receive-path="${name}"]`);f.checked=true;s.node().onchange({target:f});}s.click('receive');const first=s.calls.at(-1);await s.resolve(first.url,{id:'88888888888888888888888888888888',state:'partial',results:[{status:'verified',relative_path:'ok.mp4',item_id:'i1'},{status:'error',relative_path:'failed.mp4',error:'锁定'}]});await s.resolve('/api/ai-tasks/11111111111111111111111111111111',task());s.click('receive');const retry=s.calls.at(-1);assert.notEqual(retry.options.body.idempotency_key,first.options.body.idempotency_key);assert.equal(retry.options.body.files.length,1);assert.equal(retry.options.body.files[0].relative_path,'failed.mp4');await s.resolve(retry.url,{id:'99999999999999999999999999999999',state:'completed',results:[]});await s.resolve('/api/ai-tasks/11111111111111111111111111111111',task());
});
test('backend summaries load only the selected run, then render its fixed input and artifacts',async()=>{
 const s=fixture();s.mount();const full=task(),run=full.runs[0];full.runs=[{id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',task_id:'11111111111111111111111111111111',run_number:1,status:'已冻结',client_id:'codex',conversation_id:'固定会话'}];full.artifacts=[];await s.initialize(full);assert.equal(s.calls.at(-1).url,'/api/ai-tasks/11111111111111111111111111111111/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');assert.match(s.node().innerHTML,/正在读取本轮固定记录/);
 await s.resolve('/api/ai-tasks/11111111111111111111111111111111/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',{...run,input_snapshot:{goal:'固定轮次目标',acceptance:['可单独预览'],inputs:[{item_id:'i1',name:'轮次参考',verification:'sha256'}],skill_pins:[{collection_id:'c1',version:'v2',name:'固定版本技能'}]},artifacts:task().artifacts});assert.match(s.node().innerHTML,/固定轮次目标/);assert.match(s.node().innerHTML,/固定版本技能/);assert.match(s.node().innerHTML,/已记录内容摘要/);s.click('preview');await tick();assert.deepEqual(s.opened,['i1']);assert.equal(s.pending.length,0);
});
test('unknown create responses preserve draft and reuse request key across remount and retry',async()=>{
 const s=fixture();s.mount();await s.initialize();s.click('create');s.input('title','待确认的新任务');s.input('goal','保持草稿');s.click('save-create');const first=s.calls.at(-1);assert.ok(first.options.body.idempotency_key);await s.resolve('/api/ai-tasks',{});assert.equal(s.errors.at(-1).unknownResult,true);assert.match(s.errors.at(-1).message,/结果未确认.*相同请求/);assert.equal(s.node().querySelector('[data-ai-field="title"]').value,'待确认的新任务');assert.equal(s.state.aiCollaborationBusy,false);
 s.mount();await s.initialize();s.click('save-create');assert.equal(s.calls.at(-1).options.body.idempotency_key,first.options.body.idempotency_key);await s.resolve('/api/ai-tasks',task('22222222222222222222222222222222','p2'));assert.equal(s.node().querySelector('[data-ai-field="title"]').value,'待确认的新任务');
 s.click('save-create');assert.equal(s.calls.at(-1).options.body.idempotency_key,first.options.body.idempotency_key);await s.resolve('/api/ai-tasks',task());await s.resolve('/api/ai-tasks/11111111111111111111111111111111',task());assert.equal(s.node().querySelector('[data-ai="save-create"]'),null);assert.equal(s.pending.length,0);
});
test('create payload changes receive a new key and a transport-unknown result retains that key',async()=>{
 const s=fixture();s.mount();await s.initialize();s.click('create');s.input('title','第一次内容');s.input('goal','目标');s.click('save-create');const first=s.calls.at(-1).options.body.idempotency_key;await s.resolve('/api/ai-tasks',{});s.input('goal','新的目标');s.click('save-create');const second=s.calls.at(-1).options.body.idempotency_key;assert.notEqual(second,first);
 const error=Error('response truncated');error.unknownResult=true;s.pending.shift().reject(error);await tick();assert.match(s.errors.at(-1).message,/相同请求/);s.click('save-create');assert.equal(s.calls.at(-1).options.body.idempotency_key,second);await s.resolve('/api/ai-tasks',{});assert.equal(s.node().querySelector('[data-ai-field="goal"]').value,'新的目标');
});
test('an invalid freeze, handoff or receipt response cannot clear selections or claim success',async()=>{
 const freeze=fixture();freeze.mount();await freeze.initialize(task('11111111111111111111111111111111','p1',{runs:[],artifacts:[]}));freeze.input('conversation_id','冻结会话');freeze.click('freeze');await freeze.resolve('/api/ai-tasks/11111111111111111111111111111111/runs',{});assert.ok(freeze.node().querySelector('[data-ai="freeze"]'));assert.equal(freeze.node().querySelector('[data-ai-field="goal"]').value,'测试目标');assert.equal(freeze.errors.at(-1).unknownResult,true);
 const s=fixture();s.mount();await s.initialize();s.click('handoff');await s.resolve('/api/ai-tasks/11111111111111111111111111111111/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/handoff',{});assert.equal(s.node().querySelector('[data-ai="ack"]'),null);assert.equal(s.errors.at(-1).unknownResult,true);s.click('candidates');await s.resolve('/api/ai-tasks/11111111111111111111111111111111/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/candidates',{files:[{relative_path:'保留.mp4',size:1}],errors:[]});const input=s.node().querySelector('[data-ai-receive-path="保留.mp4"]');input.checked=true;s.node().onchange({target:input});s.click('receive');const first=s.calls.at(-1);await s.resolve(first.url,{});assert.equal(s.node().querySelector('[data-ai-receive-path="保留.mp4"]').checked,true);s.click('receive');assert.equal(s.calls.at(-1).options.body.idempotency_key,first.options.body.idempotency_key);await s.resolve(first.url,{});assert.equal(s.state.aiCollaborationBusy,false);
});
test('old round receipts remain reachable when the task summary no longer includes them',async()=>{
 const s=fixture();s.mount();const t=task('11111111111111111111111111111111','p1',{receipts:[]});t.runs[0].receipts=[{id:'33333333333333333333333333333333',run_id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',state:'completed'}];await s.initialize(t);assert.match(s.node().innerHTML,/持久收件记录 · 1 次/);s.click('receipt');assert.equal(s.calls.at(-1).url,'/api/ai-tasks/11111111111111111111111111111111/receipts/33333333333333333333333333333333');await s.resolve(s.calls.at(-1).url,{id:'33333333333333333333333333333333',run_id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',state:'completed',results:[{status:'verified',item_id:'i1'}]});assert.match(s.node().innerHTML,/收件：已完成/);
});
