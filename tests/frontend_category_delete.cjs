'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {test}=require('node:test');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const plan={token:'preview',name:'项目<script>',folder_count:2,project_count:1,item_count:3,projects:[{id:'p',name:'作品<script>',root:'C:/synthetic/project'}]};
function setup({guard=true,failure=null,refreshFailure=false}={}) {
  const calls=[],dialogs=[],messages=[],prepared=[],removed=[],disk=[];
  const nodes=new Map();
  const context=vm.createContext({URLSearchParams,localStorage:{getItem:()=>null,setItem(){}},document:{querySelector:key=>{if(!nodes.has(key))nodes.set(key,{});return nodes.get(key);}},
    fakeApi:async(path,options)=>{calls.push({path,options});if(path.endsWith('/preview'))return structuredClone(plan);if(failure)throw failure;return {project_count:1,entries:[{id:'batch',kind:'project'}]};},
    fakePrepare:async tabs=>{prepared.push(tabs);return guard;},fakeRemove:tabs=>removed.push(tabs),fakeDisk:async(...args)=>disk.push(args),
    fakeDialog:options=>{const listeners=[];const dialog={options,addEventListener:(_,fn)=>listeners.push(fn),close:()=>listeners.forEach(fn=>fn())};dialogs.push(dialog);return dialog;},
    fakeReport:error=>messages.push(error.message),fakeRefresh:async()=>{if(refreshFailure)throw new Error('refresh failed');}});
  const source=fs.readFileSync(require('node:path').join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
  vm.runInContext(source+`\nglobalThis.app={state,trashProjectCategory};api=fakeApi;prepareTabs=fakePrepare;removeOpenTabs=fakeRemove;deleteTrash=fakeDisk;
    showDialog=fakeDialog;report=fakeReport;toast=()=>{};refreshProjects=fakeRefresh;loadSection=async()=>{};renderWorkspace=()=>{};renderInspector=()=>{};markdownInputReady=()=>true;`,context);
  context.app.state.tabs=[{source:'file',item:{project_id:'p'}},{source:'external',item:{path:'C:\\synthetic\\project\\note.md'}},{source:'file',item:{project_id:'other'}}];
  return {...context.app,calls,dialogs,messages,prepared,removed,disk};
}
test('draft cancellation stops before confirmation or deletion',async()=>{
  const h=setup({guard:false});assert.equal(await h.trashProjectCategory('folder'),false);
  assert.equal(h.dialogs.length,0);assert.equal(h.calls.length,1);assert.equal(h.prepared[0].length,2);
});
test('confirmation cancellation keeps all data and does not call disk deletion',async()=>{
  const h=setup();const pending=h.trashProjectCategory('folder');await tick();
  assert.match(h.dialogs[0].options.body,/作品&lt;script&gt;/);h.dialogs[0].close();
  assert.equal(await pending,false);assert.equal(h.calls.length,2);assert.equal(h.removed.length,0);assert.equal(h.disk.length,0);
});
test('whole-category confirmation closes affected tabs and limits physical deletion to returned batches',async()=>{
  const h=setup();const pending=h.trashProjectCategory('folder');await tick();
  await h.dialogs[0].options.onSubmit({elements:{recycle_disk:{checked:true}}});h.dialogs[0].close();
  assert.equal(await pending,true);assert.equal(h.state.section,'trash');assert.equal(h.removed[0].length,2);
  assert.deepEqual(JSON.parse(JSON.stringify(h.disk[0])),[null,null,false,[{id:'batch',kind:'project'}]]);
  assert.equal(h.calls[2].options.body.token,'preview');assert.equal(h.state.categoryDeleteBusy,false);
});
test('unchecked disk option leaves recoverable project batches',async()=>{
  const h=setup();const pending=h.trashProjectCategory('folder');await tick();
  await h.dialogs[0].options.onSubmit({elements:{recycle_disk:{checked:false}}});h.dialogs[0].close();
  assert.equal(await pending,true);assert.equal(h.disk.length,0);
});
test('stale confirmation or uncertain write never removes tabs or replays deletion',async()=>{
  const h=setup({failure:new Error('内容已变化')});const pending=h.trashProjectCategory('folder');await tick();
  await h.dialogs[0].options.onSubmit({elements:{recycle_disk:{checked:true}}});h.dialogs[0].close();
  assert.equal(await pending,false);assert.equal(h.calls.length,3);assert.equal(h.removed.length,0);assert.equal(h.disk.length,0);
});
test('refresh failure after commit reports the committed outcome',async()=>{
  const h=setup({refreshFailure:true});const pending=h.trashProjectCategory('folder');await tick();
  await h.dialogs[0].options.onSubmit({elements:{recycle_disk:{checked:true}}});h.dialogs[0].close();
  assert.equal(await pending,true);assert.match(h.messages[0],/已移入回收站/);assert.equal(h.calls.length,3);
});
