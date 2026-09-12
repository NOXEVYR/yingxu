'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
const key='yingxu:sidebar-project-order';
function setup(saved=new Map(),unavailable=false){
  const calls=[],writes=[],nodes=new Map();
  const context=vm.createContext({setTimeout,clearTimeout,URLSearchParams,console,window:{},document:{querySelector:id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);}},
    localStorage:{getItem:k=>{if(unavailable)throw Error('storage unavailable');return saved.get(k)??null;},setItem:(k,value)=>{if(unavailable)throw Error('storage unavailable');saved.set(k,String(value));writes.push([k,String(value)]);}},calls});
  vm.runInContext(source+`\nconst renderSidebar=renderNavigation;renderNavigation=()=>{sidebarProjects();calls.push('render');};renderHero=()=>{};renderInspector=()=>{};loadSection=async()=>calls.push('load');guardProperties=async()=>{calls.push('guard');return true;};api=async(url,options)=>{calls.push({url,options});return {};};globalThis.app={state,sidebarProjects,sidebarProjectFolder,selectSidebarProject,renderSidebar,projectLibraryDialog};`,context);
  Object.assign(context.app.state,{projects:'abcdef'.split('').map(id=>({id,name:id})),projectId:'a',projectLibrary:{recent_ids:['a','b','c','d','e','f']},bootstrap:{capabilities:{project_library:true}}});
  return {...context.app,context,calls,writes,saved,nodes,ids:()=>Array.from(context.app.sidebarProjects(),p=>p.id)};
}
function location(s){return {project:s.state.projectId,folder:s.state.folderId,page:s.state.folderPage,offset:s.state.offset,selected:Array.from(s.state.selectedIds),active:s.state.activeKey};}
function setLocation(s){Object.assign(s.state,{folderId:'nested',folderPage:2,offset:48,activeKey:'file:editing',tabs:[{key:'file:editing',dirty:true,draft:'保留正文'}]});s.state.selectedIds.add('selected');}

test('all projects are available and visits never shuffle their positions',async()=>{
  const s=setup();assert.deepEqual(s.ids(),['a','b','c','d','e','f']);const before=s.ids();
  await s.selectSidebarProject('c');assert.deepEqual(s.ids(),before);assert.equal(s.state.projectLibrary.recent_ids[0],'c');
});

test('classification includes exact members and does not force the active project into another folder',()=>{
  const s=setup();setLocation(s);const before=location(s);
  s.state.projectLibrary={folders:[{id:'one',name:'长篇'},{id:'child',name:'子分类',parent_id:'one'}],projects:[{id:'b',folder_id:'one'},{id:'c',folder_id:'child'}]};
  s.state.projectLibraryFolder='one';assert.deepEqual(s.ids(),['b']);assert.deepEqual(location(s),before);assert.equal(s.state.tabs[0].draft,'保留正文');
  s.state.projectLibraryFolder='';assert.deepEqual(s.ids(),['a','d','e','f']);
  s.state.projectLibraryFolder='*';assert.equal(s.ids().length,6);
});

test('category title and empty state follow the selection without creating or opening a project',()=>{
  const s=setup();s.state.projectLibrary={folders:[{id:'empty',name:'空分类'}],projects:[]};s.state.projectLibraryFolder='empty';s.renderSidebar();
  assert.equal(s.nodes.get('#sidebarProjectTitle').textContent,'空分类');assert.match(s.nodes.get('#projectList').innerHTML,/此分类暂无项目/);assert.equal(s.state.projectId,'a');
  s.state.projectLibraryFolder='';s.renderSidebar();assert.equal(s.nodes.get('#sidebarProjectTitle').textContent,'未分类');
  s.state.projectLibraryFolder='*';s.renderSidebar();assert.equal(s.nodes.get('#sidebarProjectTitle').textContent,'全部项目');
});

test('classification refresh immediately removes moved members while keeping open drafts',()=>{
  const s=setup();setLocation(s);s.state.projectLibrary={folders:[{id:'one',name:'长篇'}],projects:[{id:'b',folder_id:'one'}]};s.state.projectLibraryFolder='one';assert.deepEqual(s.ids(),['b']);
  s.state.projectLibrary.projects[0].folder_id=null;assert.deepEqual(s.ids(),[]);assert.equal(s.state.activeKey,'file:editing');assert.equal(s.state.tabs[0].dirty,true);
});

test('saved classification restores on restart and deleted classifications fall back to all',()=>{
  const s=setup(new Map([['yingxu:project-library-folder','one']]));s.state.projectLibrary={folders:[{id:'one',name:'长篇'}],projects:[{id:'b',folder_id:'one'}]};assert.deepEqual(s.ids(),['b']);
  s.state.projectLibrary.folders=[];assert.equal(s.sidebarProjectFolder(),'*');assert.equal(s.ids().length,6);
});

test('project-library selection callback only changes sidebar scope and keeps editing location',async()=>{
  const s=setup();setLocation(s);const before=location(s);let callbacks;
  s.context.window.YingXuProjectLibrary={install:options=>{callbacks=options;return {open:async()=>{}};}};
  await s.projectLibraryDialog();callbacks.onFolderChange('one',{folders:[{id:'one',name:'长篇'}],projects:[{id:'b',folder_id:'one'}]});
  assert.deepEqual(s.ids(),['b']);assert.equal(s.saved.get('yingxu:project-library-folder'),'one');assert.deepEqual(location(s),before);assert.equal(s.state.tabs[0].draft,'保留正文');assert.equal(s.calls.filter(x=>x?.url).length,0);
});

test('stored ordering survives recent visits and ignores removed duplicates',async()=>{
  const s=setup(new Map([[key,JSON.stringify(['gone','c','c','b','a'])]]));assert.deepEqual(s.ids(),['c','b','a','d','e','f']);
  await s.selectSidebarProject('d');assert.deepEqual(s.ids(),['c','b','a','d','e','f']);s.state.projects=s.state.projects.filter(p=>p.id!=='b');assert.deepEqual(s.ids(),['c','a','d','e','f']);
});

test('malformed saved order or unavailable storage does not hide projects',()=>{
  for(const raw of ['{broken','null','42','"c"','{}']){assert.equal(setup(new Map([[key,raw]])).ids().length,6);}
  const s=setup(new Map(),true);assert.equal(s.ids().length,6);
});

test('repeated current-project click preserves folder, page, selection, draft and makes no requests',async()=>{
  const s=setup();s.ids();setLocation(s);const before=location(s);s.calls.length=0;s.writes.length=0;assert.equal(await s.selectSidebarProject('a'),false);assert.deepEqual(location(s),before);assert.equal(s.state.tabs[0].draft,'保留正文');assert.equal(s.state.tabs[0].dirty,true);assert.deepEqual(s.calls,[]);assert.deepEqual(s.writes,[]);
});
test('same project numeric and string ids are equivalent for the no-op check',async()=>{
  const s=setup();s.state.projectId=7;setLocation(s);const before=location(s);assert.equal(await s.selectSidebarProject('7'),false);assert.deepEqual(location(s),before);assert.deepEqual(s.calls,[]);
});
test('cancelling unsaved-property confirmation preserves location, sidebar and draft',async()=>{
  const s=setup();const ids=s.ids();setLocation(s);const before=location(s);vm.runInContext('guardProperties=async()=>{calls.push("guard-cancel");return false;}',s.context);s.calls.length=0;await s.selectSidebarProject('b');assert.deepEqual(location(s),before);assert.deepEqual(s.ids(),ids);assert.equal(s.state.tabs[0].draft,'保留正文');assert.deepEqual(s.calls,['guard-cancel']);
});
test('accepted project navigation retains existing reset semantics and preserves an open dirty tab',async()=>{
  const s=setup();s.ids();setLocation(s);await s.selectSidebarProject('b');assert.equal(s.state.projectId,'b');assert.equal(s.state.folderId,null);assert.equal(s.state.folderPage,0);assert.equal(s.state.offset,0);assert.equal(s.state.selectedIds.size,0);assert.equal(s.state.activeKey,'file:editing');assert.equal(s.state.tabs[0].draft,'保留正文');assert.equal(s.state.tabs[0].dirty,true);
});
test('visit failure leaves location unchanged and does not load another project',async()=>{
  const s=setup();const ids=s.ids();setLocation(s);const before=location(s);vm.runInContext('api=async()=>{throw Error("visit unavailable");}',s.context);s.calls.length=0;await assert.rejects(s.selectSidebarProject('b'),/visit unavailable/);assert.deepEqual(location(s),before);assert.deepEqual(s.ids(),ids);assert.ok(!s.calls.includes('load'));
});
