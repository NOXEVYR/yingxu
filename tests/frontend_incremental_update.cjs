'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const plan={state:'planned',plan_id:'a'.repeat(32),current_version:'0.4.18',latest_version:'0.4.19',total_download_bytes:73400320,download_bytes:0,changed_files:3,reused_files:123,removed_files:2,release_url:'https://github.com/NOXEVYR/yingxu/releases/tag/yingxu-v0.4.19',can_install:false};
function setup({native=true,mac=false}={}) {
  const result={innerHTML:'',textContent:''},check={disabled:false},calls=[],sent=[],toasts=[],listeners=new Set(),timers=new Map();let nextTimer=0;
  const dialog={open:true,querySelector:()=>check,addEventListener(name,fn){if(name==='close')listeners.add(fn);},removeEventListener(name,fn){listeners.delete(fn);},close(){this.open=false;for(const fn of [...listeners])fn();}};
  const nodes={'#appDialog':dialog,'#updateCheckResult':result};let handler=async()=>({state:'idle'});
  const window={yingxuDesktopIncrementalUpdate:native,yingxuMac:mac,chrome:{webview:{postMessage(data){sent.push({data,dialogOpen:dialog.open});}}}};
  const context=vm.createContext({window,localStorage:{getItem:()=>null},document:{querySelector:s=>nodes[s]},setTimeout(fn){const id=++nextTimer;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},
    fixtureApi:async(url,options)=>{calls.push({url,options});return handler(url,options);},fixtureToast:(...args)=>toasts.push(args)});
  vm.runInContext(source+'\napi=fixtureApi;toast=fixtureToast;globalThis.app={state,settingsDialog,updateSettingsHtml,updateSettingsAction,bindIncrementalUpdate,updateReleaseHtml,incrementalUpdateHost,setRecorder(value){captureHotkeyUI=value;}};',context);
  context.app.state.bootstrap={capabilities:{manual_update_check:true}};
  return {...context.app,context,result,check,calls,sent,toasts,dialog,nodes,timers,window,setApi(fn){handler=fn;},bind(){return context.app.bindIncrementalUpdate(dialog);},async tick(){const entry=timers.entries().next().value;assert.ok(entry,'expected a scheduled poll');timers.delete(entry[0]);await entry[1]();await new Promise(resolve=>setImmediate(resolve));}};
}
test('opening reads only local status; plan shows exact bytes and requires a second explicit click to download',async()=>{
  const s=setup();assert.match(s.updateSettingsHtml(),/不会自动下载或安装/);assert.equal(s.calls.length,0);
  const ui=s.bind();await ui.ready;assert.deepEqual(s.calls.map(c=>c.url),['/api/updates/status']);assert.equal(s.timers.size,0);
  s.setApi(async url=>url.endsWith('/plan')?plan:{...plan,state:'downloading'});
  await ui.action('check-update');assert.match(s.result.innerHTML,/70.0 MB/);assert.match(s.result.innerHTML,/73,400,320 字节/);assert.match(s.result.innerHTML,/变化 3.*复用 123.*移除 2/s);
  assert.equal(s.calls.filter(c=>c.url.endsWith('/download')).length,0);assert.match(s.result.innerHTML,/确认下载更新/);
  await ui.action('download-update');assert.equal(s.calls.at(-1).options.body.plan_id,plan.plan_id);assert.equal(s.timers.size,1);
});
test('asynchronous planning and download poll serially and stop on ready',async()=>{
  const s=setup();s.setApi(async()=>({...plan,state:'planning'}));const ui=s.bind();await ui.ready;assert.equal(s.timers.size,1);assert.equal(s.check.disabled,true);
  s.setApi(async()=>plan);await s.tick();assert.equal(s.timers.size,0);assert.equal(s.check.disabled,false);
  s.setApi(async()=>({...plan,state:'downloading',download_bytes:1048576}));await ui.action('download-update');assert.match(s.result.innerHTML,/已下载 1.0 MB/);
  s.setApi(async()=>({...plan,state:'ready',download_bytes:plan.total_download_bytes,can_install:true}));await s.tick();assert.equal(s.timers.size,0);assert.match(s.result.innerHTML,/保存文稿并退出安装/);assert.equal(s.sent.length,0);
});
test('closing cancels pending timer; late request cannot repaint or schedule more polls',async()=>{
  const s=setup();s.setApi(async()=>({...plan,state:'planning'}));const ui=s.bind();await ui.ready;s.dialog.close();assert.equal(s.timers.size,0);assert.equal(ui.current(),false);
  const t=setup(),pending=deferred();t.setApi(()=>pending.promise);const waiting=t.bind();t.dialog.close();t.result.innerHTML='closed';pending.resolve({...plan,state:'downloading'});await waiting.ready;assert.equal(t.result.innerHTML,'closed');assert.equal(t.timers.size,0);
});
test('replacement dialog and reopened settings ignore old success and error responses',async()=>{
  const s=setup(),pending=deferred();s.setApi(()=>pending.promise);const old=s.bind();s.state.modalSequence++;s.nodes['#updateCheckResult']={innerHTML:'replacement'};pending.reject(Error('late failure'));await old.ready;
  assert.equal(s.nodes['#updateCheckResult'].innerHTML,'replacement');assert.equal(s.timers.size,0);
  const t=setup(),first=deferred();t.setApi(()=>first.promise);const a=t.bind();t.setApi(async()=>({...plan,state:'ready',can_install:true}));t.state.modalSequence++;const b=t.bind();await b.ready;const html=t.result.innerHTML;first.resolve({...plan,state:'planning'});await a.ready;assert.equal(t.result.innerHTML,html);assert.equal(t.timers.size,0);
});
test('duplicate download clicks are single flight and disabled install never crosses bridge',async()=>{
  const s=setup();s.setApi(async()=>plan);const ui=s.bind();await ui.ready;await ui.action('install-update');assert.equal(s.sent.length,0);
  const pending=deferred();s.setApi(()=>pending.promise);const first=ui.action('download-update');await ui.action('download-update');await ui.action('check-update');assert.equal(s.calls.filter(c=>c.url.endsWith('/download')).length,1);
  pending.resolve({...plan,state:'ready',can_install:false});await first;await ui.action('install-update');assert.equal(s.sent.length,0);assert.doesNotMatch(s.result.innerHTML,/data-action="install-update"/);
});
test('install closes settings then requests native guard once; cancellation can resume ready state',async()=>{
  const s=setup();s.setApi(async()=>({...plan,state:'ready',can_install:true}));const ui=s.bind();await ui.ready;await ui.action('install-update');await ui.action('install-update');
  assert.equal(s.sent.length,1);assert.equal(s.sent[0].dialogOpen,false);assert.equal(s.sent[0].data.action,'install-update');assert.equal(s.sent[0].data.planId,plan.plan_id);assert.equal(s.timers.size,0);
  assert.doesNotMatch(s.result.innerHTML,/安装成功/);s.dialog.open=true;s.state.modalSequence++;const reopened=s.bind();await reopened.ready;assert.match(s.result.innerHTML,/保存文稿并退出安装/);assert.equal(s.sent.length,1);
});
test('busy settings and hotkey recording block install without closing; bridge errors do not claim success',async()=>{
  const s=setup();s.setApi(async()=>({...plan,state:'ready',can_install:true}));const ui=s.bind();await ui.ready;s.state.modalBusy=true;await ui.action('install-update');assert.equal(s.dialog.open,true);
  s.state.modalBusy=false;s.setRecorder({isRecording:()=>true});await ui.action('install-update');assert.equal(s.sent.length,0);s.setRecorder(null);
  s.window.chrome.webview.postMessage=()=>{throw Error('host unavailable');};await ui.action('install-update');assert.match(s.toasts.at(-1)[0],/未能请求安装/);
});
test('network and Range failures stop polling and offer trusted complete-package fallback',async()=>{
  const s=setup();s.setApi(async()=>({...plan,state:'downloading'}));const ui=s.bind();await ui.ready;s.setApi(async()=>{throw Error('服务器不支持 Range <img onerror=1>');});await s.tick();
  assert.equal(s.timers.size,0);assert.match(s.result.innerHTML,/不支持 Range &lt;img/);assert.match(s.result.innerHTML,/NOXEVYR\/yingxu\/releases\/tag\/yingxu-v0.4.19/);assert.match(s.result.innerHTML,/重新读取更新状态/);assert.doesNotMatch(s.result.innerHTML,/<img onerror/);
  s.setApi(async()=>({...plan,state:'error',message:'Range unavailable'}));await ui.action('refresh-update');assert.equal(s.timers.size,0);assert.doesNotMatch(s.result.innerHTML,/data-action="download-update"/);
});
test('current, malformed state and invalid plan never offer download or install',async()=>{
  for(const info of [{...plan,state:'current'},{...plan,state:'ready',plan_id:'bad',can_install:true},{...plan,state:'planned',plan_id:'bad'},{...plan,state:'unknown'}]){
    const s=setup();s.setApi(async()=>info);const ui=s.bind();await ui.ready;assert.equal(s.timers.size,0);assert.doesNotMatch(s.result.innerHTML,/data-action="(?:download|install)-update"/);await ui.action('install-update');assert.equal(s.sent.length,0);
  }
});
test('old hosts and Mac use manual full-package check without incremental requests',async()=>{
  for(const options of [{native:false},{native:true,mac:true}]){
    const s=setup(options);assert.equal(s.bind(),null);assert.equal(s.calls.length,0);s.setApi(async()=>({...plan,update_available:true,channel:'平台',tag:'yingxu-v0.4.19',url:'https://evil.invalid'}));
    await s.updateSettingsAction('check-update',{dataset:{},disabled:false});assert.equal(s.calls[0].url,'/api/updates/check');assert.match(s.result.innerHTML,/NOXEVYR/);assert.doesNotMatch(s.result.innerHTML,/evil.invalid/);await s.updateSettingsAction('install-update',{dataset:{},disabled:false});assert.equal(s.sent.length,0);
  }
});
test('release metadata cannot inject markup or direct users to arbitrary URLs',()=>{
  const s=setup(),html=s.updateReleaseHtml({release_url:'https://evil.invalid/<script>',latest_version:'\"><img src=x onerror=1>',tag:'javascript:alert(1)'});
  assert.match(html,/https:\/\/github.com\/NOXEVYR\/yingxu\/releases/);assert.doesNotMatch(html,/evil|script|<img|javascript/);
});
test('missing or invalid download sizes never display a misleading zero-byte confirmation',async()=>{
  for(const total of [undefined,-1,'73400320',Infinity]){
    const s=setup();s.setApi(async()=>({...plan,total_download_bytes:total}));const ui=s.bind();await ui.ready;
    assert.match(s.result.innerHTML,/下载量或文件清单不完整/);assert.doesNotMatch(s.result.innerHTML,/data-action="download-update"/);await ui.action('download-update');assert.equal(s.calls.length,1);
  }
});
test('real HTTP status requests carry session authentication even with implicit GET',async()=>{
  const http=require('node:http'),received=[];
  const server=http.createServer((request,response)=>{
    received.push({url:request.url,token:request.headers['x-yingxu-token']});
    const protectedStatus=/^\/api\/updates\/(?:status|install\/status)/.test(request.url);
    response.writeHead(protectedStatus && request.headers['x-yingxu-token']!=='synthetic-session-token'?403:200,{'Content-Type':'application/json'});
    response.end(JSON.stringify({state:'idle'}));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    const base=`http://127.0.0.1:${server.address().port}`;
    const context=vm.createContext({window:{},localStorage:{getItem:()=>null},document:{querySelector:()=>null},fetch:(url,options)=>fetch(base+url,options)});
    vm.runInContext(source+'\nglobalThis.app={state,api};',context);context.app.state.bootstrap={token:'synthetic-session-token'};
    await context.app.api('/api/updates/status');
    await context.app.api('/api/updates/status',{method:'GET'});
    await context.app.api('/api/updates/install/status?ticket='+ 'a'.repeat(32));
    await context.app.api('/api/settings');
    assert.equal(received.length,4);assert.ok(received.slice(0,3).every(row=>row.token==='synthetic-session-token'));assert.equal(received[3].token,undefined);
  } finally {server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});
test('settings distinguish a local build without changing the release version or injecting markup',async()=>{
  const s=setup({native:false});s.dialog.open=false;s.state.bootstrap.version='0.4.18';s.state.bootstrap.build_revision='incremental.1 <test>';
  s.setApi(async()=>({default_view:'grid',default_sort:'updated'}));
  vm.runInContext('showDialog=options=>{globalThis.settingsBody=options.body;};',s.context);
  await s.settingsDialog();assert.match(s.context.settingsBody,/版本 0\.4\.18 · incremental\.1 &lt;test&gt;/);assert.doesNotMatch(s.context.settingsBody,/<test>/);
  delete s.state.bootstrap.build_revision;await s.settingsDialog();assert.match(s.context.settingsBody,/版本 0\.4\.18 · 稳定版/);assert.doesNotMatch(s.context.settingsBody,/incremental\.1/);
});
test('previous installation result distinguishes rollback and failure, escapes messages and survives a new check',async()=>{
  for(const [state,label] of [['installed','已安装'],['rolled_back','未安装成功，已回退旧版'],['recovery_required','未完成，需要恢复'],['failed','安装失败'],['cancelled','已取消']]){
    const s=setup();s.setApi(async()=>({state:'idle',last_install:{state,version:'0.4.19',message:'<img src=x onerror=1>',restarted:false}}));
    const ui=s.bind();await ui.ready;assert.ok(s.result.innerHTML.includes(`上次安装：${label}`));assert.match(s.result.innerHTML,/手动打开映序/);assert.match(s.result.innerHTML,/&lt;img/);assert.doesNotMatch(s.result.innerHTML,/<img/);
    if(state!=='installed')assert.doesNotMatch(s.result.innerHTML,/上次安装：已安装/);
    s.setApi(async()=>plan);await ui.action('check-update');assert.ok(s.result.innerHTML.includes(`上次安装：${label}`));
    s.setApi(async()=>({state:'current',last_install:null}));await ui.action('refresh-update');assert.doesNotMatch(s.result.innerHTML,/上次安装：/);
  }
});
