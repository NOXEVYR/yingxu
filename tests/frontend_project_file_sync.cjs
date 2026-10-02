'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {test}=require('node:test');
function setup() {
  const calls=[],refresh=[],messages=[],inspector=[];let pending=null;
  const sandbox={URLSearchParams,setTimeout,Date,document:{hidden:false,querySelector:()=>({open:false})},localStorage:{getItem:()=>null},
    fakeApi:async(path,options)=>{calls.push({path,options});if(path==='/api/projects')return {projects:[{id:'p1',name:'Synthetic project',root:'synthetic',counts:{total:3}}]};if(path.endsWith('/sync'))return {job_id:'job'};return pending ? await pending : {state:'done',errors:[]};},
    fakeRefresh:async options=>refresh.push(options),fakeLoad:async()=>refresh.push('items'),fakeToast:message=>messages.push(message)};
  const context=vm.createContext(sandbox);
  const source=fs.readFileSync(require('node:path').join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
  vm.runInContext(source+`\nglobalThis.app={state,syncProjectFiles,refreshProjects};api=fakeApi;refreshProjects=fakeRefresh;loadItems=fakeLoad;toast=fakeToast;documentOnlyActive=()=>false;markdownInputReady=()=>true;`,context);
  Object.assign(context.app.state,{projectId:'p1',section:'assets',bootstrap:{capabilities:{project_file_sync:true}},tabs:[]});
  context.inspector = inspector;
  vm.runInContext(`renderNavigation=()=>{};renderHero=()=>{};renderInspector=()=>inspector.push(projectInspectorHtml());`,context);
  return {...context.app,calls,refresh,messages,inspector,document:sandbox.document,defer:()=>{let done;pending=new Promise(resolve=>done=resolve);return done;}};
}
test('focus sync requests only the selected project and retains workspace location',async()=>{
  const h=setup();await h.syncProjectFiles();assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0])),{path:'/api/project-files/sync',options:{method:'POST',body:{project_id:'p1'}}});
  assert.deepEqual(JSON.parse(JSON.stringify(h.refresh)),[{preserveLocation:true},'items']);assert.equal(h.messages.length,0);
  await h.syncProjectFiles();assert.equal(h.calls.length,2);
});
test('busy edits, hidden windows and old servers do not start background sync',async()=>{
  for(const mode of ['saving','hidden','old','uploading']) {
    const h=setup();if(mode==='saving')h.state.tabs=[{saving:true}];if(mode==='hidden')h.document.hidden=true;if(mode==='old')h.state.bootstrap.capabilities={};if(mode==='uploading')h.state.uploading=true;
    await h.syncProjectFiles();assert.equal(h.calls.length,0);
  }
});
test('late results cannot change a newly selected project or close its draft',async()=>{
  const h=setup(),finish=h.defer(),draft={dirty:true,draft:'unsaved'};h.state.tabs=[draft];
  const first=h.syncProjectFiles();await new Promise(setImmediate);await h.syncProjectFiles();assert.equal(h.calls.length,2);
  h.state.projectId='p2';finish({state:'done',errors:[]});await first;assert.deepEqual(h.refresh,[]);assert.equal(draft.draft,'unsaved');
});
test('scan failures release busy state and preserve existing view',async()=>{
  const h=setup(),finish=h.defer(),run=h.syncProjectFiles();finish({state:'error',message:'offline folder'});
  await assert.rejects(run,/offline folder/);assert.equal(h.state.projectSyncBusy,false);assert.deepEqual(h.refresh,[]);
});
test('refreshed project count updates the rendered project inspector',async()=>{
  const h=setup();h.state.projects=[{id:'p1',name:'Synthetic project',counts:{total:1}}];
  await h.refreshProjects({preserveLocation:true});
  assert.equal(h.inspector.length,1);assert.match(h.inspector[0],/<dt>资源<\/dt><dd>3<\/dd>/);
  assert.equal(h.state.projectId,'p1');
});
test('project refresh keeps the active metadata form and its unsaved draft intact',async()=>{
  const h=setup(),tab={key:'file:a',propertiesDirty:true,propertiesDraft:{name:'unsaved'}};
  h.state.tabs=[tab];h.state.activeKey=tab.key;await h.refreshProjects({preserveLocation:true});
  assert.equal(h.inspector.length,0);assert.equal(tab.propertiesDraft.name,'unsaved');
  assert.equal(h.state.tabs[0],tab);
});
test('project refresh does not rebuild a different workspace inspector',async()=>{
  const h=setup();h.state.section='context';await h.refreshProjects({preserveLocation:true});
  assert.equal(h.inspector.length,0);
});
