'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
const first='a'.repeat(32),second='b'.repeat(32);
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
function setup({fail=false}={}) {
  const waits=[],requests=[],notices=[],reports=[];
  const context=vm.createContext({console,
    setTimeout:(callback,delay)=>{waits.push({callback,delay});return waits.length;},clearTimeout(){},
    localStorage:{getItem:()=>null,setItem(){}},window:{},
    document:{querySelector:()=>({open:false}),querySelectorAll:()=>[]},
    fakeApi:async url=>{requests.push(url);if(fail)throw new Error('fixture read failed');return {id:url.split('/').at(-1),name:'synthetic.md',kind:'markdown',content:{editable:false,content:'# synthetic',etag:'fixture'}};},
    fakeToast:message=>notices.push(message),fakeReport:error=>reports.push(error.message)});
  const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
  vm.runInContext(source+`\napi=fakeApi;toast=fakeToast;report=fakeReport;renderWorkspace=()=>{};renderTabs=()=>{};globalThis.app={state,queueExternalFiles};`,context);
  return {...context.app,waits,requests,notices,reports};
}

for(const mode of ['text','markdown','docx','canvas'])test(`external open waits for ${mode} composition without dropping the queue entry`,async()=>{
  const s=setup();let composing=true;
  const tab={key:'editing',source:'external',item:{kind:mode==='canvas'?'excalidraw':mode},draft:'unfinished',dirty:true};
  if(mode==='text')Object.defineProperty(tab,'textComposing',{get:()=>composing});
  else tab[mode==='canvas'?'canvasEditor':mode+'Editor']={isComposing:()=>composing,getValue:()=>tab.draft};
  s.state.tabs=[tab];s.state.activeKey=tab.key;
  const pending=s.queueExternalFiles([{id:first}]);await flush();
  assert.equal(s.requests.length,0,'do not query or open another document during IME composition');
  assert.equal(s.state.externalQueue.length,1,'keep the requested document in the queue');
  assert.equal(s.state.tabs[0],tab);assert.equal(tab.draft,'unfinished');assert.equal(tab.dirty,true);
  assert.equal(s.waits.length,1,'one bounded wait while a request is pending');
  // A second shell-open request should join the same drain, in order.
  const another=s.queueExternalFiles([{id:second}]);await flush();
  assert.equal(s.state.externalQueue.length,2);assert.equal(s.waits.length,1);
  composing=false;s.waits.shift().callback();await Promise.all([pending,another]);
  assert.deepEqual(s.requests,[`/api/external/${first}`,`/api/external/${second}`]);
  assert.equal(s.state.externalQueue.length,0);assert.equal(s.state.externalDraining,null);
  assert.equal(s.state.tabs.length,3);assert.equal(s.waits.length,0,'no idle polling after draining');
  assert.equal(tab.draft,'unfinished');assert.equal(tab.dirty,true);
});

test('failed external read is reported once without retrying forever',async()=>{
  const s=setup({fail:true});await s.queueExternalFiles([{id:first}]);
  assert.deepEqual(s.requests,[`/api/external/${first}`]);
  assert.deepEqual(s.reports,['fixture read failed']);
  assert.equal(s.state.tabs[0].error,'fixture read failed');
  assert.equal(s.state.externalQueue.length,0);assert.equal(s.state.externalDraining,null);
  assert.equal(s.waits.length,0);
});
