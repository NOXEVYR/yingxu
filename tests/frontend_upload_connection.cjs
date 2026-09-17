'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
function setup({outcome='network',health='online',body='{"id":"item"}',status=201}={}) {
  const nodes=new Map(), requests=[],probes=[],errors=[],notices=[];
  const node=key=>{if(!nodes.has(key))nodes.set(key,{});return nodes.get(key);};
  node('#connectionState').textContent='本地连接正常';
  class XHR {
    constructor(){this.upload={};}
    open(method,url){requests.push(url);}
    setRequestHeader(){}
    abort(){this.onabort();}
    send(){
      if(outcome==='throw')throw new Error('unreadable file');
      queueMicrotask(()=>{
        if(outcome==='abort')this.onabort();
        else if(outcome==='network')this.onerror();
        else if(outcome==='timeout')this.ontimeout();
        else {this.status=status;this.responseText=body;this.onload();}
      });
    }
  }
  const fetch=async(url,options)=>{
    probes.push({url,options});
    if(health==='offline')throw new TypeError('fetch failed');
    if(health==='timeout')return new Promise((_,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted'))));
    return {ok:true,json:async()=>health==='foreign'?{ok:true,app:'other'}:{ok:true,app:'yingxu'}};
  };
  const context=vm.createContext({window:{},document:{querySelector:node},localStorage:{getItem:()=>null},console,URLSearchParams,XMLHttpRequest:XHR,AbortController,fetch,setTimeout:(f)=>setTimeout(f,10),clearTimeout,errors,notices});
  const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
  vm.runInContext(source+'\nrefreshProjects=async()=>{};loadItems=async()=>{};toast=m=>notices.push(m);report=e=>errors.push(e.message);globalThis.app={state,uploadFiles};',context);
  Object.assign(context.app.state,{projectId:'project',category:'characters',bootstrap:{token:'synthetic'},section:'assets'});
  return {app:context.app,node,requests,probes,errors,notices};
}
for(const outcome of ['network','timeout'])test(`${outcome}: live server is not labelled disconnected; no automatic upload retry`,async()=>{
  const s=setup({outcome});await s.app.uploadFiles([{name:'中文 图片.png'},{name:'second.png'}],'characters','nested');
  assert.equal(s.requests.length,1);assert.equal(s.probes.length,1);
  assert.match(s.errors[0],/结果未能确认，但本地服务仍可连接/);assert.match(s.errors[0],/选择文件/);
  assert.equal(s.node('#connectionState').textContent,'本地连接正常');assert.equal(s.notices.length,0);assert.equal(s.app.state.uploading,false);
  const url=new URL(s.requests[0],'http://127.0.0.1');assert.equal(url.searchParams.get('folder_id'),'nested');assert.equal(url.searchParams.get('name'),'中文 图片.png');
});
for(const health of ['offline','foreign','timeout'])test(`health ${health}: clear stale healthy indicator`,async()=>{
  const s=setup({health});await s.app.uploadFiles([{name:'a.png'}],'characters');
  assert.equal(s.node('#connectionState').textContent,'本地连接检查未通过');assert.match(s.errors[0],/恢复后先查看/);assert.equal(s.app.state.uploading,false);
});
test('HTTP rejection retains backend reason and requires no health probe',async()=>{
  const s=setup({outcome:'load',status:409,body:JSON.stringify({error:'目标目录不可写'})});await s.app.uploadFiles([{name:'a.png'}],'characters');
  assert.deepEqual(s.errors,['目标目录不可写']);assert.equal(s.probes.length,0);assert.equal(s.notices.length,0);
});
for(const body of ['bad json','{}','null'])test(`invalid success response ${body} is not counted as an imported file`,async()=>{
  const s=setup({outcome:'load',body});await s.app.uploadFiles([{name:'a.png'}],'characters');
  assert.equal(s.notices.length,0);assert.equal(s.probes.length,1);assert.equal(s.errors.length,1);
});
test('valid import succeeds without extra health requests',async()=>{
  const s=setup({outcome:'load'});const items=await s.app.uploadFiles([{name:'a.png'}],'characters');
  assert.equal(items.length,1);assert.equal(s.probes.length,0);assert.equal(s.errors.length,0);assert.match(s.notices[0],/1 个文件/);
});
test('synchronous source read failure is not labelled disconnected',async()=>{
  const s=setup({outcome:'throw'});await s.app.uploadFiles([{name:'a.png'}],'characters');
  assert.match(s.errors[0],/无法读取或发送/);assert.equal(s.probes.length,0);assert.equal(s.app.state.uploading,false);
});
test('cancelled request does not claim rollback or probe the connection',async()=>{
  const s=setup({outcome:'abort'});await s.app.uploadFiles([{name:'a.png'}],'characters');
  assert.match(s.errors[0],/结果请在列表中核对/);assert.equal(s.probes.length,0);assert.equal(s.notices.length,0);
});
