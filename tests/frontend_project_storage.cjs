'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
const deferred = () => { let resolve,reject; const promise = new Promise((yes,no) => {resolve=yes;reject=no;}); return {promise,resolve,reject}; };
function setup(picker=true) {
  const nodes=new Map(),calls=[],dialogs=[];
  const node = key => { if(!nodes.has(key))nodes.set(key,{open:true,value:'',textContent:'',disabled:false,hidden:false,classes:new Set(),listeners:{},classList:{toggle(name,value){value?node(key).classes.add(name):node(key).classes.delete(name);}},querySelectorAll(){return [];},addEventListener(name,fn){this.listeners[name]=fn;},focus(){this.focused=true;}});return nodes.get(key); };
  let handler=async(url,options={}) => url==='/api/pick'?{paths:['D:/合成素材']}:options.method==='POST'?{root:options.body.root,project_count:2}:{root:'C:/合成项目',project_count:2};
  const context=vm.createContext({setTimeout,window:{},localStorage:{getItem:()=>null,setItem(){}},document:{querySelector:node,querySelectorAll:()=>[]},
    fixtureApi:async(url,options={})=>{calls.push({url,options});return handler(url,options);},fixtureDialog:options=>{dialogs.push(options);return node('#appDialog');}});
  vm.runInContext(source+`\napi=fixtureApi;showDialog=fixtureDialog;globalThis.app={state,projectStorageSettingsHtml,bindProjectStorageSettings,settingsDialog};`,context);
  context.app.state.bootstrap={capabilities:{project_storage:true,native_picker:picker},project_root:'C:/合成项目'};
  return {...context.app,context,nodes,node,calls,dialogs,setApi(value){handler=value;},bind(){return context.app.bindProjectStorageSettings(node('#appDialog'));}};
}
test('settings section provides separate explicit action and accessible status without form names',()=>{
  const s=setup(),html=s.projectStorageSettingsHtml();
  assert.match(html,/项目存放位置/);assert.match(html,/type="button"[^>]*>保存存放位置/);assert.match(html,/aria-live="polite"/);
  assert.doesNotMatch(html,/\bname=/);assert.match(html,/原目录保留作备份/);
});
test('opening reads current root; picker only previews and explicit save persists once',async()=>{
  const s=setup(),ui=s.bind();await ui.ready;
  assert.equal(s.node('#projectStorageCurrent').textContent,'C:/合成项目');assert.equal(s.node('#saveProjectStorage').disabled,true);
  await ui.pick();assert.equal(s.node('#projectStoragePath').value,'D:/合成素材');assert.equal(s.calls.filter(c=>c.url==='/api/project-storage'&&c.options.method==='POST').length,0);
  await ui.apply();assert.equal(s.state.bootstrap.project_root,'D:/合成素材');assert.equal(s.node('#projectStorageCurrent').textContent,'D:/合成素材');
  assert.match(s.node('#projectStorageNotice').textContent,/以后新建的项目将存放在这里/);assert.equal(s.node('#saveProjectStorage').disabled,true);
  await ui.apply();assert.equal(s.calls.filter(c=>c.options.method==='POST'&&c.url==='/api/project-storage').length,1);
});
test('cancelled picker preserves typed path and sends no storage mutation',async()=>{
  const s=setup(),ui=s.bind();await ui.ready;s.node('#projectStoragePath').value='E:/待保存';s.setApi(async()=>({paths:[]}));
  await ui.pick();assert.equal(s.node('#projectStoragePath').value,'E:/待保存');assert.match(s.node('#projectStorageNotice').textContent,/已取消选择/);
  assert.equal(s.calls.filter(c=>c.url==='/api/project-storage'&&c.options.method==='POST').length,0);
});
test('no native picker still permits typed absolute path and Enter does not submit ordinary settings',async()=>{
  const s=setup(false),ui=s.bind();await ui.ready;assert.equal(s.node('#chooseProjectStorage').disabled,true);assert.match(s.node('#projectStorageNotice').textContent,/完整路径/);
  await ui.pick();assert.equal(s.calls.length,1);s.node('#projectStoragePath').value='/Volumes/素材';s.node('#projectStoragePath').listeners.input();
  assert.equal(s.node('#saveProjectStorage').disabled,false);let prevented=false;s.node('#projectStoragePath').listeners.keydown({key:'Enter',preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(s.node('#saveProjectStorage').focused,true);await ui.apply();assert.equal(s.state.bootstrap.project_root,'/Volumes/素材');
});
test('failed save preserves editable target and ordinary settings fields for retry',async()=>{
  const s=setup(),ui=s.bind();await ui.ready;s.node('#projectStoragePath').value='D:/不能访问';s.node('#settingAppearance').value='paper';
  s.setApi(async()=>{throw Error('文件夹不可写 <合成>');});await ui.apply();
  assert.equal(s.node('#projectStoragePath').value,'D:/不能访问');assert.equal(s.node('#settingAppearance').value,'paper');assert.equal(s.state.bootstrap.project_root,'C:/合成项目');
  assert.equal(s.node('#projectStorageNotice').textContent,'文件夹不可写 <合成>');assert.equal(s.node('#saveProjectStorage').disabled,false);
});
test('read failure disables saving and offers a retry without replacing settings form',async()=>{
  const s=setup();s.setApi(async()=>{throw Error('读取失败');});const ui=s.bind();await ui.ready;
  assert.equal(s.node('#retryProjectStorage').hidden,false);assert.equal(s.node('#projectStoragePath').disabled,true);assert.equal(s.node('#saveProjectStorage').disabled,true);
  s.setApi(async()=>({root:'C:/恢复'}));await ui.load();assert.equal(s.node('#projectStorageCurrent').textContent,'C:/恢复');assert.equal(s.node('#retryProjectStorage').hidden,true);
});
test('unavailable configured disk reports its reason but allows selecting and saving another location',async()=>{
  const s=setup();s.setApi(async(url,options)=>url==='/api/pick'?{paths:['D:/可用项目']}:options.method==='POST'?{root:options.body.root,available:true,error:''}:{root:'E:/离线磁盘',available:false,error:'项目磁盘未连接，请选择其他位置。'});
  const ui=s.bind();await ui.ready;
  assert.equal(s.node('#projectStorageCurrent').textContent,'E:/离线磁盘');assert.equal(s.node('#projectStorageNotice').textContent,'项目磁盘未连接，请选择其他位置。');
  assert.equal(s.node('#projectStorageNotice').classes.has('project-storage-error'),true);assert.equal(s.node('#retryProjectStorage').hidden,false);
  assert.equal(s.node('#chooseProjectStorage').disabled,false);assert.equal(s.node('#projectStoragePath').disabled,false);assert.equal(s.node('#saveProjectStorage').disabled,true);
  await ui.pick();await ui.apply();assert.equal(s.state.bootstrap.project_root,'D:/可用项目');assert.equal(s.node('#projectStorageCurrent').textContent,'D:/可用项目');
  assert.equal(s.node('#retryProjectStorage').hidden,true);assert.equal(s.node('#projectStorageNotice').classes.has('project-storage-error'),false);assert.match(s.node('#projectStorageNotice').textContent,/已保存/);
});
test('pending picker ignores duplicate clicks and stale result after another dialog replaces settings',async()=>{
  const s=setup(),ui=s.bind();await ui.ready;const pending=deferred();s.setApi(()=>pending.promise);
  const first=ui.pick();await ui.pick();assert.equal(s.calls.length,2);s.state.modalSequence++;s.node('#projectStoragePath').value='另一弹窗';
  pending.resolve({paths:['D:/过期']});await first;assert.equal(s.node('#projectStoragePath').value,'另一弹窗');
});
test('save is single-flight and successful late response updates root without touching replacement dialog',async()=>{
  const s=setup(),ui=s.bind();await ui.ready;const pending=deferred();s.setApi(()=>pending.promise);s.node('#projectStoragePath').value='D:/最终';
  const first=ui.apply();await ui.apply();assert.equal(s.calls.filter(c=>c.options.method==='POST').length,1);assert.equal(s.node('#projectStoragePath').disabled,true);
  s.state.modalSequence++;s.node('#projectStorageCurrent').textContent='其他弹窗';pending.resolve({root:'D:/最终'});await first;
  assert.equal(s.state.bootstrap.project_root,'D:/最终');assert.equal(s.node('#projectStorageCurrent').textContent,'其他弹窗');
});
test('reopened settings waits for an active save then reads final persisted location',async()=>{
  const s=setup(),ui=s.bind();await ui.ready;const pending=deferred();let stored='C:/合成项目';
  s.setApi(async(url,options)=>{if(options.method==='POST'){const result=await pending.promise;stored=result.root;return result;}return {root:stored};});
  s.node('#projectStoragePath').value='D:/最终';const saving=ui.apply();s.state.modalSequence++;const reopened=s.bind();
  assert.equal(s.calls.length,2);pending.resolve({root:'D:/最终'});await saving;await reopened.ready;
  assert.equal(s.node('#projectStorageCurrent').textContent,'D:/最终');assert.equal(s.node('#projectStoragePath').value,'D:/最终');
});
