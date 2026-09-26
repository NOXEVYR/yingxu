'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const root=path.join(__dirname,'..'),script=fs.readFileSync(path.join(root,'frontend/startup.js'),'utf8');
function fixture(native=false){
  const timers=new Map(),events={},classes=new Set();let removed=0,id=0;
  const elements={startupScreen:{remove(){removed++;}},startupMessage:{textContent:''},startupDismiss:{hidden:true,addEventListener(name,fn){this[name]=fn;}}};
  const window={yingxuNativeStartup:native,addEventListener(name,fn){events[name]=fn;}};
  const document={documentElement:{classList:{add(x){classes.add(x);}}},getElementById:k=>elements[k],addEventListener(n,f){events[n]=f;},removeEventListener(n){delete events[n];}};
  vm.runInNewContext(script,{window,document,setTimeout(fn){timers.set(++id,fn);return id;},clearTimeout(n){timers.delete(n);}});
  return {window,document,events,elements,timers,classes,get removed(){return removed;}};
}
test('readiness removes startup immediately and cancels the only fallback timer',()=>{
  const f=fixture();f.events.DOMContentLoaded();assert.equal(f.timers.size,1);
  f.window.YingXuStartup.finish();assert.equal(f.removed,1);assert.equal(f.timers.size,0);
});
test('readiness before DOMContentLoaded cannot recreate the overlay or timer',()=>{
  const f=fixture();f.window.YingXuStartup.finish();assert.equal(f.events.DOMContentLoaded,undefined);assert.equal(f.timers.size,0);
});
test('native reveal avoids a second web animation',()=>{
  const f=fixture(true);assert.ok(f.classes.has('native-startup'));f.events.DOMContentLoaded();assert.equal(f.removed,1);assert.equal(f.timers.size,0);
});
test('slow or failed app script leaves a working way into the workspace',()=>{
  const f=fixture();f.events.DOMContentLoaded();[...f.timers.values()][0]();assert.equal(f.elements.startupDismiss.hidden,false);
  assert.match(f.elements.startupMessage.textContent,/连接提示/);f.elements.startupDismiss.click();assert.equal(f.removed,1);
});
test('leaving the page releases pending startup work',()=>{
  const f=fixture();f.events.DOMContentLoaded();f.events.pagehide();assert.equal(f.timers.size,0);assert.equal(f.removed,1);
});
test('boot success, API failure and early initialization failure all dismiss startup',async()=>{
  const app=fs.readFileSync(path.join(root,'frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
  for(const failure of ['','api','events']){
    const nodes={};let finished=0;
    const context=vm.createContext({window:{YingXuStartup:{finish(){finished++;}}},console,setTimeout,clearTimeout,localStorage:{getItem(){return null;}},document:{querySelector(key){return nodes[key]||=( {value:'',textContent:'',innerHTML:''});}},failure});
    vm.runInContext(app+`\nwireEvents=()=>{if(failure==='events')throw Error('events');};readDrafts=()=>{};api=async()=>{if(failure==='api')throw Error('api');return {settings:{}};};applyAppearance=()=>{};refreshProjects=async()=>{};configureSection=()=>{};loadItems=async()=>{};renderInspector=()=>{};syncProjectFiles=async()=>{};globalThis.run=boot;`,context);
    await context.run();assert.equal(finished,1,failure||'success');
  }
});
