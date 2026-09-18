const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/macos.js','utf8');
function load(search) {
  const sent=[], events={}, styles=[];
  const window={addEventListener:(name,fn)=>events[name]=fn};
  const context={window,location:{search},URLSearchParams,console,state:{bootstrap:{token:'local'}} ,
    api:(path,options)=>{sent.push({path,message:options.body});return Promise.resolve({ok:true});},report:()=>{},
    document:{createElement:()=>({}),head:{appendChild:style=>styles.push(style)},addEventListener:(name,fn)=>events[name]=fn,querySelector:()=>({setAttribute:(key,value)=>sent.push({key,value})})}};
  vm.createContext(context);vm.runInContext(source,context);
  return {context,window,sent,events,styles};
}
const win=load('');assert.equal(win.window.chrome,undefined);assert.equal(win.styles.length,0);
const mac=load('?desktop=macos');assert.equal(mac.window.yingxuMac,true);
mac.window.chrome.webview.postMessage({action:'desktop-ready'});
assert.equal(mac.sent.length,1);assert.equal(mac.sent[0].path,'/api/macos/desktop');
mac.window.chrome.webview.postMessage({action:'drag-files',ids:['arbitrary']});assert.equal(mac.sent.length,1);
mac.window.chrome.webview.postMessage({action:'exit-response',requestId:'x',allow:false});assert.equal(mac.sent[1].message.allow,false);
let received;mac.window.chrome.webview.addEventListener('message',event=>received=event.data);
mac.window.yingxuMacReceive({action:'prepare-exit'});assert.equal(received.action,'prepare-exit');
assert.match(mac.styles[0].textContent,/capture-screen/);
assert.match(mac.styles[0].textContent,/data-drag-file/);
mac.events.DOMContentLoaded();assert.equal(mac.sent.at(-1).value,'/?desktop=macos');
console.log('macOS bridge: Windows isolation, queued readiness, unsupported action rejection, close routing and hidden controls passed');

const {test}=require('node:test');
const appSource=fs.readFileSync('frontend/app.js','utf8').replace(/boot\(\);\s*$/,'');
function appFixture(isMac=true){
  const dialogs=[],calls=[],nodes=new Map();
  const node=key=>{if(!nodes.has(key))nodes.set(key,{open:false,value:'',innerHTML:'',addEventListener(){},focus(){this.focused=true;},select(){this.selected=true;}});return nodes.get(key);};
  const context=vm.createContext({setTimeout,localStorage:{getItem:()=>null,setItem(){}},window:{yingxuMac:isMac,chrome:{webview:{postMessage(){}}}},
    document:{querySelector:node},
    FormData:class{constructor(form){this.form=form;}has(key){return !!this.form[key];}get(key){return this.form[key];}},
    fixtureApi:async(url,options)=>{calls.push({url,options});return options?.body || {confirm_delete:true,confirm_trash_delete:true,close_to_tray:true,capture_enabled:true,capture_hotkey:'Ctrl+Alt+Shift+S',capture_mode:'annotate',default_view:'grid',default_sort:'updated'};},
    fixtureDialog:options=>dialogs.push(options)});
  vm.runInContext(appSource+`\napi=fixtureApi;showDialog=fixtureDialog;toast=()=>{};configureSection=()=>{};loadItems=async()=>{};hideMenu=()=>{};globalSearchDialog=()=>globalThis.globalOpened=true;openDocumentSearch=()=>globalThis.documentOpened=true;documentSearchable=()=>true;globalThis.app={state,settingsDialog,searchShortcut};`,context);
  context.app.state.bootstrap={version:'0.4.12',capabilities:{maintenance:true,manual_update_check:true},settings:{}};
  return {context,...context.app,dialogs,calls,nodes,node};
}
test('macOS uses shared version and maintenance settings without unsupported controls',async()=>{
  const s=appFixture();await s.settingsDialog();const body=s.dialogs[0].body;
  assert.match(body,/0\.4\.12-mac\.1/);assert.match(body,/界面与后台版本一致/);assert.match(body,/maintenance-preview/);assert.match(body,/手动更新/);assert.match(body,/data-action="check-update"/);assert.match(body,/maintenanceKeep/);assert.match(body,/macOS 废纸篓/);assert.match(body,/⌘F/);
  assert.doesNotMatch(body,/name="(?:close_to_tray|capture_enabled|capture_hotkey|capture_mode)"|data-action="(?:un)?register-open-with"/);
  await s.dialogs[0].onSubmit({confirm_delete:true,confirm_trash_delete:true,autoplay_media:true,default_view:'list',default_sort:'name'});
  assert.deepEqual(Object.keys(s.calls[1].options.body).sort(),['appearance_theme','autoplay_media','confirm_delete','confirm_trash_delete','default_sort','default_view']);
  assert.equal(s.calls[1].options.body.appearance_theme,'swiss');
  assert.equal(s.state.view,'list');assert.equal(s.state.sort,'name');
});
test('Windows retains tray capture and open-with settings',async()=>{
  const s=appFixture(false);await s.settingsDialog();const body=s.dialogs[0].body;
  assert.match(body,/name="close_to_tray"/);assert.match(body,/name="capture_mode"/);assert.match(body,/data-action="register-open-with"/);assert.match(body,/Windows 回收站/);assert.doesNotMatch(body,/macOS 试用版/);
});
test('Command+F searches document when focused there, resources elsewhere; Command+K remains global',()=>{
  const s=appFixture();const event=(key,editor)=>({key,metaKey:true,ctrlKey:false,altKey:false,preventDefault(){this.prevented=true;},target:{closest:selector=>editor&&selector==='#editor'?{}:null}});
  const documentEvent=event('f',true);assert.equal(s.searchShortcut(documentEvent),true);assert.equal(documentEvent.prevented,true);assert.equal(s.context.documentOpened,true);
  s.context.documentOpened=false;const resourceEvent=event('f',false);s.searchShortcut(resourceEvent);assert.equal(s.context.documentOpened,false);assert.equal(s.node('#searchInput').focused,true);
  s.searchShortcut(event('k',true));assert.equal(s.context.globalOpened,true);
});
