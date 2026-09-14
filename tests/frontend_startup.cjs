'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
function fixture(enabled=true){
  const requests=[],rendered=[];
  const context=vm.createContext({window:{},console,setTimeout,clearTimeout,URLSearchParams,
    localStorage:{getItem:()=>null,setItem:()=>{}},requests,rendered});
  vm.runInContext(source+`\napi=url=>new Promise((resolve,reject)=>requests.push({url,resolve,reject}));
    renderNavigation=()=>rendered.push({projects:state.projects,library:state.projectLibrary});renderHero=()=>{};
    globalThis.app={state,refreshProjects};`,context);
  const {app}=context;
  Object.assign(app.state,{projectId:'kept',projects:[{id:'kept'}],projectLibrary:{folders:[{id:'old'}]},bootstrap:{capabilities:{project_library:enabled}},activeKey:'draft',tabs:[{key:'draft',dirty:true,draft:'未保存正文'}]});
  return {...app,requests,rendered};
}
test('startup requests project data together and renders only a coherent pair',async()=>{
  const f=fixture(),oldProjects=f.state.projects,oldLibrary=f.state.projectLibrary;
  const refresh=f.refreshProjects();
  assert.deepEqual(f.requests.map(r=>r.url),['/api/projects','/api/project-library']);
  f.requests[1].resolve({folders:[{id:'current'}],projects:[{id:'kept',folder_id:'current'}]});
  await Promise.resolve();
  assert.equal(f.state.projects,oldProjects);assert.equal(f.state.projectLibrary,oldLibrary);assert.equal(f.rendered.length,0);
  f.requests[0].resolve({projects:[{id:'kept',name:'保留项目'}]});await refresh;
  assert.equal(f.rendered.length,1);assert.equal(f.state.projectLibrary.folders[0].id,'current');
  assert.equal(f.state.activeKey,'draft');assert.equal(f.state.tabs[0].draft,'未保存正文');assert.equal(f.state.projectId,'kept');
});
test('failed startup request does not publish a partial project classification',async()=>{
  const f=fixture(),projects=f.state.projects,library=f.state.projectLibrary;
  const refresh=f.refreshProjects();
  f.requests[0].resolve({projects:[{id:'new'}]});f.requests[1].reject(Error('temporary failure'));
  await assert.rejects(refresh,/temporary failure/);
  assert.equal(f.state.projects,projects);assert.equal(f.state.projectLibrary,library);assert.equal(f.rendered.length,0);
});
test('older backends without project library retain the one-request startup path',async()=>{
  const f=fixture(false),refresh=f.refreshProjects();assert.equal(f.requests.length,1);
  f.requests[0].resolve({projects:[{id:'kept'}]});await refresh;assert.equal(f.rendered.length,1);
});
