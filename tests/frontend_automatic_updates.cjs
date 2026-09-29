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
