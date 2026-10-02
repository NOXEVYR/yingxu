'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function setup(handler){
  const nodes=[],calls=[],timers=new Map();let n=0,settings=0;
  const document={body:{appendChild(node){nodes.push(node);}},createElement(){return {children:[],setAttribute(){},replaceChildren(){this.children=[];},append(...parts){this.children.push(...parts);},remove(){const i=nodes.indexOf(this);if(i>=0)nodes.splice(i,1);}};}};
  const window={document};vm.runInNewContext(fs.readFileSync('frontend/automatic-updates.js','utf8'),{window,setTimeout,clearTimeout});
  const ui=window.YingXuAutomaticUpdates.start({document,api:async(path,options)=>{calls.push({path,options});return handler(path);},openSettings(){settings++;},setTimer(fn,delay){timers.set(++n,{fn,delay});return n;},clearTimer(id){timers.delete(id);}});
  return {ui,nodes,calls,timers,get settings(){return settings;},async tick(){const [id,timer]=timers.entries().next().value;timers.delete(id);await timer.fn();await new Promise(r=>setImmediate(r));}};
}
test('starts asynchronously, ready notice opens settings without installing, dismissal does not repeat',async()=>{
  const s=setup(()=>({state:'ready',latest_version:'0.4.99',update_available:true}));await s.ui.ready;
  assert.equal(s.calls[0].path,'/api/updates/automatic/start');assert.equal(s.calls[0].options.method,'POST');assert.equal(s.nodes.length,0);
  await s.tick();assert.equal(s.nodes.length,1);assert.match(s.nodes[0].children[0].textContent,/下载并验证/);
  s.nodes[0].children[1].onclick();assert.equal(s.settings,1);assert.ok(s.calls.every(c=>!c.path.includes('install')));
  s.nodes[0].children[2].onclick();await s.tick();assert.equal(s.nodes.length,0);s.ui.stop();assert.equal(s.timers.size,0);
});
test('network errors stay quiet and retry is bounded; closing prevents late repaint',async()=>{
  const s=setup(()=>{throw Error('offline');});await s.ui.ready;assert.equal(s.nodes.length,0);assert.equal([...s.timers.values()][0].delay,300000);s.ui.stop();assert.equal(s.timers.size,0);
  let resolve;const t=setup(path=>path.endsWith('start')?{}:new Promise(r=>{resolve=r;}));await t.ui.ready;const pending=t.ui.poll();t.ui.stop();resolve({state:'ready'});await pending;assert.equal(t.nodes.length,0);assert.equal(t.timers.size,0);
});
test('checks escape remote version data through textContent and never automatically confirm installation',async()=>{
  const s=setup(()=>({state:'available',update_available:true,latest_version:'<img onerror=bad>'}));await s.ui.ready;await s.tick();assert.equal(s.nodes.length,1);assert.equal(s.nodes[0].children[0].innerHTML,undefined);assert.equal(s.nodes[0].children[1].textContent,'查看更新');s.ui.stop();
});
test('dismissal is scoped to a build and a newer repair build can notify again',async()=>{
  let state={state:'available',update_available:true,latest_version:'0.4.22',latest_build:'workflow.3',update_kind:'build'};
  const s=setup(()=>state);await s.ui.ready;await s.tick();assert.match(s.nodes[0].children[0].textContent,/修补构建/);
  s.nodes[0].children[2].onclick();await s.tick();assert.equal(s.nodes.length,0);
  state={...state,latest_build:'workflow.4'};await s.tick();assert.equal(s.nodes.length,1);s.ui.stop();
});
test('uncertain build opens manual details without triggering a download or installation',async()=>{
  const s=setup(()=>({state:'manual_required',update_available:false,latest_version:'0.4.22',latest_build:'other.3',update_kind:'manual'}));
  await s.ui.ready;await s.tick();assert.equal(s.nodes.length,1);assert.match(s.nodes[0].children[0].textContent,/手动核对/);
  s.nodes[0].children[1].onclick();assert.equal(s.settings,1);assert.ok(s.calls.every(c=>!/(?:download|install)/.test(c.path)));s.ui.stop();
});
test('stale ready notice disappears on checking, current, disabled and failure states',async()=>{
  const ready={state:'ready',update_available:true,latest_version:'0.4.23',latest_build:'workflow.4'};
  let state=ready;const s=setup(()=>state);await s.ui.ready;
  for (const phase of ['checking','current','disabled','error']) {
    state=ready;await s.tick();assert.equal(s.nodes.length,1);
    state={...ready,state:phase};await s.tick();assert.equal(s.nodes.length,0);
  }
  state=ready;await s.tick();assert.equal(s.nodes.length,1);s.ui.stop();
});
test('updates over the background limit still offer manual download details',async()=>{
  for (const kind of ['version','build']) {
    const s=setup(()=>({state:'manual_required',update_available:true,update_kind:kind,latest_version:'0.4.23',latest_build:'workflow.4',download_bytes:52428801}));
    await s.ui.ready;await s.tick();assert.equal(s.nodes.length,1);assert.equal(s.nodes[0].children[1].textContent,'查看更新');
    s.nodes[0].children[1].onclick();assert.equal(s.settings,1);assert.ok(s.calls.every(c=>!/(?:download|install)/.test(c.path)));s.ui.stop();
  }
});
test('a failed start request is retried before polling status and starts only one scheduler',async()=>{
  let starts=0;
  const s=setup(path=>{if(path.endsWith('start') && ++starts===1)throw Error('transient start failure');return {state:'current'};});
  await s.ui.ready;assert.equal(starts,1);assert.equal(s.timers.size,1);
  await s.tick();assert.equal(starts,2);assert.equal(s.calls.at(-1).path,'/api/updates/automatic/status');
  await s.ui.poll();assert.equal(starts,2);assert.equal(s.timers.size,1);
  s.ui.stop();assert.equal(s.timers.size,0);
});
