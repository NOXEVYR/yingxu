'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function setup(){
  const nodes=new Map(),calls=[],refreshed=[];
  const node=key=>{if(!nodes.has(key))nodes.set(key,{open:true,value:'',textContent:'',innerHTML:'',disabled:false,hidden:false,isConnected:true,events:{},querySelectorAll:()=>[],classList:{toggle(){}},addEventListener(name,fn){this.events[name]=fn;},removeAttribute(name){delete this[name];},focus(){}});return nodes.get(key);};
  const roots=[{id:'one',name:'项目 <一>',root:'C:/旧位置/项目一'},{id:'two',name:'项目二',root:'E:/其他/项目二'}];
  let stored='C:/旧位置',target='D:/新位置';
  const plan={token:'preview-token',projects:roots.map(p=>({id:p.id,name:p.name,source_root:p.root,target_root:'D:/新位置/'+p.name,files:3,bytes:1024,external_references:1})),total_bytes:2048,warnings:['保留 <引用>']};
  let handler=async(url,options={})=>{
    if(url==='/api/project-storage')return {root:stored,existing_roots:roots,available:true};
    if(url.endsWith('/migration/preview'))return plan;
    if(url.endsWith('/migration'))return {job_id:'copy-one'};
    if(url.includes('/migration/jobs/')){stored=target;return {state:'done',completed:6,total:6};}
    throw Error('Unexpected '+url);
  };
  const context=vm.createContext({setTimeout:fn=>setTimeout(fn,0),clearTimeout,window:{},localStorage:{getItem:()=>null,setItem(){}},document:{querySelector:node,querySelectorAll:selector=>selector.includes('#dialogForm')?[node('#settingAppearance'),node('#closeDialog')]:[]},
    fixtureApi:async(url,options={})=>{calls.push({url,options});return handler(url,options);},fixtureRefresh:async ids=>refreshed.push(Array.from(ids))});
  vm.runInContext(source+`\nconst realApi=api,realRefresh=refreshMigratedProjects;api=fixtureApi;refreshMigratedProjects=fixtureRefresh;markDirty=tab=>{tab.dirty=true;};toast=()=>{};globalThis.app={state,projectMigrationHtml,bindProjectMigration,bindProjectStorageSettings,migrationDraftProblem,realApi,realRefresh};`,context);
  context.app.state.bootstrap={capabilities:{project_storage:true,native_picker:true},project_root:stored};context.app.state.projectId='one';
  const ui=context.app.bindProjectMigration(node('#appDialog'),{root:()=>target,complete:async()=>{},update:()=>{}});ui.setSnapshot({existing_roots:roots});
  return {...context.app,context,node,nodes,calls,refreshed,ui,plan,roots,setTarget(value){target=value;},setApi(fn){handler=fn;}};
}
test('changing location saves by previewing ALL projects, never configures root before migration',async()=>{
  const s=setup(),storage=s.bindProjectStorageSettings(s.node('#appDialog'));await storage.ready;
  s.node('#projectStoragePath').value='D:/新位置';await storage.apply();
  const call=s.calls.find(row=>row.url.endsWith('/migration/preview'));assert.deepEqual(Array.from(call.options.body.project_ids),['one','two']);assert.equal(call.options.body.root,'D:/新位置');
  assert.equal(s.calls.some(row=>row.url==='/api/project-storage'&&row.options.method==='POST'),false);assert.equal(s.state.bootstrap.project_root,'C:/旧位置');
  assert.equal(s.node('#saveProjectStorage').textContent,'保存并迁移');assert.match(s.node('#migrationProjects').textContent,/全部 2 个项目/);
});
test('preview escapes untrusted paths and warnings and requires explicit confirmation',async()=>{
  const s=setup();await s.ui.preview();const html=s.node('#projectMigrationPlan').innerHTML;
  assert.match(html,/项目 &lt;一&gt;/);assert.match(html,/保留 &lt;引用&gt;/);assert.doesNotMatch(html,/<一>|<引用>/);assert.match(html,/原目录保留作备份/);
  assert.equal(s.node('#confirmProjectMigration').hidden,false);assert.equal(s.calls.filter(c=>c.url.endsWith('/migration')).length,0);
  assert.doesNotMatch(s.projectMigrationHtml(),/type="checkbox"|选择要迁移/);
});
test('changed destination invalidates confirmation and refuses start without a new preview',async()=>{
  const s=setup();await s.ui.preview();s.setTarget('F:/新目标');await s.ui.start();assert.equal(s.calls.length,1);assert.equal(s.node('#confirmProjectMigration').hidden,true);assert.match(s.node('#projectMigrationNotice').textContent,/重新预览/);
});
test('stale preview cannot replace another dialog and duplicate preview is ignored',async()=>{
  const s=setup(),pending=deferred();s.setApi(()=>pending.promise);const first=s.ui.preview();await s.ui.preview();s.state.modalSequence++;s.node('#projectMigrationPlan').innerHTML='其他弹窗';pending.resolve(s.plan);await first;assert.equal(s.calls.length,1);assert.equal(s.node('#projectMigrationPlan').innerHTML,'其他弹窗');
});
for(const property of ['dirty','propertiesDirty','loading','saving','propertiesSaving','textComposing'])test(`migration preserves and refuses open ${property} work`,async()=>{
  const s=setup();s.state.tabs=[{key:'draft',item:{name:'未保存稿'},draft:'原有正文',[property]:true}];await s.ui.preview();await s.ui.start();
  assert.equal(s.calls.filter(c=>c.url.endsWith('/migration')).length,0);assert.equal(s.state.tabs[0].draft,'原有正文');assert.equal(s.state.tabs[0][property],true);assert.ok(!s.state.migrationBusy);
});
test('canvas latest value is flushed before checking unsaved state without destroying its editor',async()=>{
  const s=setup(),editor={isComposing:()=>false,getValue:()=>'{"elements":[1]}'};s.state.tabs=[{item:{name:'画板'},draft:'{}',canvasEditor:editor}];
  await s.ui.preview();await s.ui.start();assert.equal(s.state.tabs[0].dirty,true);assert.equal(s.state.tabs[0].draft,'{"elements":[1]}');assert.equal(s.state.tabs[0].canvasEditor,editor);assert.equal(s.calls.length,1);
});
test('running copy locks modal and controls, suppresses duplicate starts, then refreshes IDs and unlocks',async()=>{
  const s=setup(),pending=deferred();await s.ui.preview();s.setApi(async(url)=>url.endsWith('/migration')?{job_id:'one'}:pending.promise);
  const work=s.ui.start();assert.equal(s.state.modalBusy,true);assert.equal(s.state.migrationBusy,true);assert.equal(s.node('#closeDialog').disabled,true);await s.ui.start();
  pending.resolve({state:'done',total:6,completed:6});await work;
  assert.deepEqual(s.refreshed,[['one','two']]);assert.equal(s.state.modalBusy,false);assert.equal(s.state.migrationBusy,false);assert.equal(s.node('#closeDialog').disabled,false);assert.match(s.node('#projectMigrationNotice').textContent,/迁移完成/);
});
test('terminal worker failure exposes reason and releases locks without changing user drafts',async()=>{
  const s=setup();await s.ui.preview();s.setApi(async(url)=>url.endsWith('/migration')?{job_id:'one'}:{state:'error',error:'目标磁盘已断开，源文件保留'});await s.ui.start();
  assert.equal(s.state.modalBusy,false);assert.equal(s.refreshed.length,0);assert.equal(s.node('#projectMigrationNotice').textContent,'目标磁盘已断开，源文件保留');
});
test('lost start response stays locked and retries same token idempotently',async()=>{
  const s=setup();await s.ui.preview();let attempts=0;s.setApi(async(url)=>{if(url.endsWith('/migration')){if(++attempts===1)throw Error('连接中断');return {job_id:'same-job'};}return {state:'done'};});
  await s.ui.start();assert.equal(s.state.migrationBusy,true);assert.equal(s.node('#retryProjectMigration').hidden,false);assert.equal(s.node('#retryProjectMigration').disabled,false);
  await s.ui.retry();assert.equal(s.state.migrationBusy,false);assert.deepEqual(s.calls.filter(c=>c.url.endsWith('/migration')).map(c=>c.options.body.token),['preview-token','preview-token']);
});
test('poll interruption keeps editing paused then resumes the SAME job without submitting twice',async()=>{
  const s=setup();await s.ui.preview();let polls=0;s.setApi(async(url)=>{if(url.endsWith('/migration'))return {job_id:'same-job'};if(++polls===1)throw Error('读取超时');return {state:'done'};});
  await s.ui.start();assert.equal(s.state.modalBusy,true);await s.ui.retry();assert.equal(s.calls.filter(c=>c.url.endsWith('/migration')).length,1);assert.equal(s.state.modalBusy,false);
});

test('expired job after backend restart refreshes actual storage and releases locks without claiming success',async()=>{
  const s=setup(),storage=s.bindProjectStorageSettings(s.node('#appDialog'));await storage.ready;
  s.node('#projectStoragePath').value='D:/新位置';await storage.apply();
  s.setApi(async(url)=>{
    if(url.endsWith('/migration'))return {job_id:'lost-job'};
    if(url.includes('/migration/jobs/'))throw Object.assign(Error('任务已过期'),{status:404});
    if(url.endsWith('/migration/status'))return {active_job_id:null};
    if(url==='/api/project-storage')return {root:'C:/旧位置',existing_roots:s.roots,available:true};
    throw Error('Unexpected '+url);
  });
  await s.node('#confirmProjectMigration').events.click();
  assert.deepEqual(s.refreshed,[['one','two']]);assert.equal(s.state.migrationBusy,false);assert.equal(s.state.modalBusy,false);assert.equal(s.node('#closeDialog').disabled,false);
  assert.equal(s.state.bootstrap.project_root,'C:/旧位置');assert.equal(s.node('#projectStoragePath').value,'C:/旧位置');assert.match(s.node('#projectMigrationNotice').textContent,/后台已重启.*实际项目位置.*重新预览/);assert.doesNotMatch(s.node('#projectMigrationNotice').textContent,/迁移完成/);
});

test('expired job follows current active job without submitting another migration',async()=>{
  const s=setup();await s.ui.preview();s.setApi(async(url)=>{
    if(url.endsWith('/migration'))return {job_id:'lost-job'};
    if(url.endsWith('/jobs/lost-job'))throw Object.assign(Error('任务已过期'),{status:404});
    if(url.endsWith('/migration/status'))return {active_job_id:'active-job'};
    if(url.endsWith('/jobs/active-job')){assert.equal(s.state.migrationBusy,true);return {state:'done'};}
    throw Error('Unexpected '+url);
  });
  await s.ui.start();assert.equal(s.calls.filter(c=>c.url.endsWith('/migration')).length,1);assert.equal(s.state.modalBusy,false);assert.deepEqual(s.refreshed,[['one','two']]);
});

test('expired job retains editing lock when active status cannot be confirmed',async()=>{
  const s=setup();await s.ui.preview();s.setApi(async(url)=>{
    if(url.endsWith('/migration'))return {job_id:'lost-job'};
    if(url.includes('/migration/jobs/'))throw Object.assign(Error('任务已过期'),{status:404});
    throw Error('状态连接中断');
  });
  await s.ui.start();assert.equal(s.state.modalBusy,true);assert.equal(s.state.migrationBusy,true);assert.equal(s.node('#retryProjectMigration').hidden,false);assert.equal(s.refreshed.length,0);
});

test('confirmed idle backend releases lock even if actual project refresh fails',async()=>{
  const s=setup();await s.ui.preview();vm.runInContext('refreshMigratedProjects=async()=>{throw Error("刷新失败");};',s.context);
  s.setApi(async(url)=>{if(url.endsWith('/migration'))return {job_id:'lost-job'};if(url.endsWith('/migration/status'))return {active_job_id:null};throw Object.assign(Error('任务已过期'),{status:404});});
  await s.ui.start();assert.equal(s.state.modalBusy,false);assert.equal(s.state.migrationBusy,false);assert.match(s.node('#projectMigrationNotice').textContent,/实际位置刷新未完成.*刷新失败/);assert.doesNotMatch(s.node('#projectMigrationNotice').textContent,/迁移完成/);
});
test('mutation gate prevents unrelated writes while allowing idempotent start retry',async()=>{
  const s=setup();s.state.migrationBusy=true;
  await assert.rejects(s.realApi('/api/settings',{method:'PATCH',body:{}}),/正在迁移/);
  await assert.rejects(s.realApi('/api/items',{method:'POST',body:{}}),/正在迁移/);
  s.context.fetch=async()=>({ok:true,json:async()=>({job_id:'same'})});const result=await s.realApi('/api/project-storage/migration',{method:'POST',body:{token:'same'}});assert.equal(result.job_id,'same');
});
test('successful refresh updates paths and etags without replacing clean editor drafts or selected IDs',async()=>{
  const s=setup(),editor={};const tab={key:'file:note',id:'note',source:'file',item:{project_id:'one',path:'C:/旧/note.md'},content:{etag:'old',editable:true},draft:'原正文',markdownEditor:editor};
  s.state.tabs=[tab];s.state.activeKey=tab.key;s.state.selectedIds.add('note');s.state.folderId='nested';s.state.offset=48;
  s.setApi(async(url)=>url.startsWith('/api/items/')?{id:'note',project_id:'one',path:'D:/新/note.md'}:{etag:'new',content:'原正文',editable:true});
  vm.runInContext('refreshProjects=async()=>{};loadItems=async()=>state.selectedIds.clear();updateSelection=()=>{};renderWorkspace=()=>{};',s.context);
  await s.realRefresh(['one']);assert.equal(tab.item.path,'D:/新/note.md');assert.equal(tab.content.etag,'new');assert.equal(tab.draft,'原正文');assert.equal(tab.markdownEditor,editor);assert.equal(s.state.activeKey,tab.key);assert.equal(s.state.folderId,'nested');assert.equal(s.state.offset,48);assert.deepEqual(Array.from(s.state.selectedIds),['note']);
});
test('refresh failure still preserves selection and migration is not incorrectly reported as rolled back',async()=>{
  const s=setup();s.state.selectedIds.add('note');vm.runInContext('refreshProjects=async()=>{};loadItems=async()=>{state.selectedIds.clear();throw Error("刷新失败");};updateSelection=()=>{};',s.context);
  await assert.rejects(s.realRefresh(['one']),/刷新失败/);assert.deepEqual(Array.from(s.state.selectedIds),['note']);
});

test('migration refresh loads rewritten relative links into a clean existing editor',async()=>{
  const s=setup(),values=[],editor={setValue:value=>values.push(value)};
  const tab={key:'file:note',id:'note',source:'file',item:{project_id:'one'},content:{etag:'old',content:'[角色](old.md)'},draft:'[角色](old.md)',markdownEditor:editor};
  s.state.tabs=[tab];s.setApi(async(url)=>url.startsWith('/api/items/')?{id:'note',project_id:'one'}:{etag:'new',content:'[角色](../角色/new.md)'});
  vm.runInContext('refreshProjects=async()=>{};loadItems=async()=>{};updateSelection=()=>{};renderWorkspace=()=>{};',s.context);
  await s.realRefresh(['one']);assert.equal(tab.draft,'[角色](../角色/new.md)');assert.deepEqual(values,[tab.draft]);assert.equal(tab.markdownEditor,editor);assert.equal(tab.content.etag,'new');assert.equal(Boolean(tab.dirty),false);
});
test('a late dirty draft keeps its original etag and is marked conflicting after migration',async()=>{
  const s=setup(),tab={id:'note',source:'file',item:{project_id:'one'},content:{etag:'old'},draft:'未保存正文',dirty:true};
  s.state.tabs=[tab];s.setApi(async(url)=>url.startsWith('/api/items/')?{id:'note',project_id:'one'}:{etag:'new',content:'磁盘正文'});
  vm.runInContext('refreshProjects=async()=>{};loadItems=async()=>{};updateSelection=()=>{};renderWorkspace=()=>{};',s.context);
  await s.realRefresh(['one']);assert.equal(tab.draft,'未保存正文');assert.equal(tab.content.etag,'old');assert.equal(tab.conflict,true);
});
