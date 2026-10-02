'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../frontend/mcp-connection.js'),'utf8');
const escapeHtml=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const tick=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
class Node {
 constructor(){this.children=[];this.parent=null;this.connected=false;this.controls=[];this._html='';this.className='';}
 get isConnected(){return this.connected || !!this.parent?.isConnected;}
 append(node){node.parent=this;this.children.push(node);}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);this.parent=null;this.connected=false;}
 set innerHTML(value){this._html=value;this.controls=[];for(const match of value.matchAll(/<([a-z][a-z0-9]*)\b([^>]*)>/gi)){
  const attrs=match[2],data=[...attrs.matchAll(/data-([\w-]+)(?:="([^"]*)")?/g)];if(!data.length)continue;
  const item={dataset:Object.fromEntries(data.map(a=>[a[1].replace(/-([a-z])/g,(_,x)=>x.toUpperCase()),a[2] || ''])),disabled:/\bdisabled\b/.test(attrs),value:'',placeholder:attrs.match(/placeholder="([^"]*)"/)?.[1] || '',_text:'',_html:'',onclick:null,oninput:null};
  Object.defineProperties(item,{textContent:{get(){return this._text;},set(v){this._text=String(v);this._html=escapeHtml(v);}},innerHTML:{get(){return this._html;},set(v){this._html=String(v);this._text=String(v).replace(/<[^>]*>/g,'');}}});this.controls.push(item);
 }}
 get innerHTML(){return this._html+this.controls.map(x=>x._html+escapeHtml(x.value)).join('')+this.children.map(x=>x.innerHTML).join('');}
 querySelector(selector){if(selector==='.mcp-connection')return this.children.find(x=>x.className==='mcp-connection') || null;if(selector==='.handoff-card')return null;const attr=selector.match(/^\[data-([\w-]+)\]$/);if(!attr)return null;const key=attr[1].replace(/-([a-z])/g,(_,x)=>x.toUpperCase());return this.controls.find(x=>Object.hasOwn(x.dataset,key)) || null;}
}
function fixture(){
 const state={projectId:'p1',section:'context',bootstrap:{capabilities:{mcp_project_read:true}}},root=new Node();root.connected=true;
 const calls=[],pending=[],copies=[],toasts=[],errors=[],secret='SYNTHETIC-MCP-BEARER-DO-NOT-RENDER';let clipboardReject=false,sharedCopies=0;
 const clipboard={writeText:async value=>{if(clipboardReject)throw Error(secret);copies.push(value);}};
 const context=vm.createContext({window:{},document:{createElement:()=>new Node()},navigator:{clipboard},URL});vm.runInContext(source,context);
 const env={state,escapeHtml,api:(url,options={})=>{calls.push({url,options});return new Promise((resolve,reject)=>pending.push({url,resolve,reject}));},toast:message=>toasts.push(message),report:error=>errors.push(error),copyText:()=>{sharedCopies++;}};
 const mount=()=>context.window.YingXuMCP.mount(root,env),q=name=>root.querySelector('.mcp-connection').querySelector('[data-mcp-'+name+']');
 const click=name=>{const button=q(name);assert.ok(button);if(!button.disabled)button.onclick();};
 const input=value=>{q('address').value=value;q('address').oninput();};
 const resolve=async(value,url=pending[0]?.url)=>{const index=pending.findIndex(x=>x.url===url);assert.ok(index>=0,'Missing request '+url);pending.splice(index,1)[0].resolve(value);await tick();};
 const reject=async()=>{pending.shift().reject(Error(secret));await tick();};
 const off=(extra={})=>({enabled:false,project_id:null,endpoint:'http://127.0.0.1:8795/mcp',default_endpoint:'http://127.0.0.1:8795/mcp',custom_endpoint:false,address_error:'',read_only:true,...extra});
 const on=(id='p1',extra={})=>off({enabled:true,project_id:id,project_name:'项目 '+id,...extra});
 const connection=(endpoint,id='p1')=>({project_id:id,read_only:true,config:{mcpServers:{yingxu:{url:endpoint,headers:{Authorization:'Bearer '+secret}}}}});
 const checkSecret=()=>{assert.doesNotMatch(root.innerHTML,new RegExp(secret));assert.ok(errors.every(x=>!String(x).includes(secret)));assert.ok(toasts.every(x=>!x.includes(secret)));assert.equal(sharedCopies,0);};
 return {state,root,mount,q,click,input,resolve,reject,off,on,connection,calls,pending,copies,toasts,errors,checkSecret,context,setClipboardReject:value=>{clipboardReject=value;}};
}
test('default address comes from status and mounting never enables service or polls',async()=>{
 const s=fixture();s.mount();assert.equal(s.calls.length,1);assert.equal(s.calls[0].url,'/api/mcp/status');assert.equal(s.q('apply').disabled,true);await s.resolve(s.off());assert.equal(s.q('address').placeholder,s.off().default_endpoint);assert.equal(s.q('address').value,'');assert.equal(s.q('endpoint').textContent,s.off().endpoint);assert.equal(s.q('apply').disabled,true);assert.equal(s.q('enable').disabled,false);await tick();assert.equal(s.calls.length,1);assert.doesNotMatch(source,/setInterval|setTimeout|localStorage|sessionStorage|console\./);
});
test('saving while closed stays closed and restoring default requires explicit apply',async()=>{
 const s=fixture();s.mount();await s.resolve(s.off());s.input('http://localhost:8899/creative');assert.equal(s.q('endpoint').textContent,s.off().endpoint);assert.match(s.q('address-state').textContent,/尚未应用/);s.click('apply');s.click('apply');assert.equal(s.calls.length,2);assert.deepEqual(JSON.parse(JSON.stringify(s.calls.at(-1).options.body)),{enabled:false,endpoint:'http://localhost:8899/creative'});assert.equal(s.q('address').disabled,true);
 await s.resolve(s.off({endpoint:'http://127.0.0.1:8899/creative',custom_endpoint:true}));assert.equal(s.q('status').textContent,'MCP 已关闭');assert.equal(s.q('address').value,'http://127.0.0.1:8899/creative');s.click('default-button');assert.equal(s.calls.length,2);assert.equal(s.q('address').value,'');s.click('apply');assert.deepEqual(JSON.parse(JSON.stringify(s.calls.at(-1).options.body)),{enabled:false,endpoint:''});await s.resolve(s.off());assert.equal(s.q('address').value,'');assert.equal(s.q('endpoint').textContent,s.off().default_endpoint);s.checkSecret();
});
test('enable uses saved address, applying keeps project authorization, copying uses server actual URL',async()=>{
 const s=fixture(),actual='http://127.0.0.1:8899/creative';s.mount();await s.resolve(s.off({endpoint:actual,custom_endpoint:true}));s.click('enable');assert.deepEqual(JSON.parse(JSON.stringify(s.calls.at(-1).options.body)),{enabled:true,project_id:'p1'});await s.resolve(s.on('p1',{endpoint:actual,custom_endpoint:true}));s.input('http://127.0.0.1:8898/next');s.click('apply');assert.deepEqual(JSON.parse(JSON.stringify(s.calls.at(-1).options.body)),{enabled:true,project_id:'p1',endpoint:'http://127.0.0.1:8898/next'});await s.resolve(s.on('p1',{endpoint:'http://127.0.0.1:8898/next',custom_endpoint:true}));s.click('copy');await s.resolve(s.connection('http://127.0.0.1:8898/next'));assert.equal(JSON.parse(s.copies[0]).mcpServers.yingxu.url,'http://127.0.0.1:8898/next');s.checkSecret();s.click('disable');assert.deepEqual(JSON.parse(JSON.stringify(s.calls.at(-1).options.body)),{enabled:false});await s.resolve(s.off({endpoint:'http://127.0.0.1:8898/next',custom_endpoint:true}));
});
test('a failed status reread keeps actions locked and never treats malformed identity as confirmed',async()=>{
 const s=fixture();s.mount();await s.resolve(s.on());s.input('http://127.0.0.1:8800/changed');s.click('apply');await s.reject();assert.equal(s.calls.at(-1).url,'/api/mcp/status');assert.equal(s.q('apply').disabled,true);assert.equal(s.q('copy').disabled,true);await s.resolve(s.on({toString:()=> 'p1'}),'/api/mcp/status'); // malformed identity must not claim a confirmed service
 assert.equal(s.q('enable').disabled,true);s.checkSecret();
});
test('port conflict with an unchanged running service is shown as unapplied, without discarding draft',async()=>{
 const s=fixture();s.mount();await s.resolve(s.on());s.input('http://127.0.0.1:8800/changed');s.click('apply');await s.reject();await s.resolve(s.on());assert.equal(s.q('endpoint').textContent,s.off().endpoint);assert.equal(s.q('address').value,'http://127.0.0.1:8800/changed');assert.match(s.q('address-state').textContent,/未应用/);assert.equal(s.q('copy').disabled,false);assert.equal(s.toasts.length,0);s.click('copy');await s.resolve(s.connection(s.off().endpoint));assert.equal(JSON.parse(s.copies[0]).mcpServers.yingxu.url,s.off().endpoint);s.checkSecret();
});
test('an unknown write response triggers a read instead of presenting applied state',async()=>{
 const s=fixture();s.mount();await s.resolve(s.off());s.input('http://127.0.0.1:8800/custom');s.click('apply');await s.resolve({});assert.equal(s.calls.at(-1).url,'/api/mcp/status');await s.resolve(s.off());assert.equal(s.q('address').value,'http://127.0.0.1:8800/custom');assert.equal(s.toasts.length,0);assert.match(s.q('address-state').textContent,/未应用/);
});
test('another authorized project requires an explicit project switch before address apply',async()=>{
 const s=fixture();s.mount();await s.resolve(s.on('p2',{project_name:'<img src=x onerror=bad()>'}));assert.match(s.q('status').innerHTML,/&lt;img/);s.input('http://127.0.0.1:8800/own');assert.equal(s.q('apply').disabled,true);s.click('apply');assert.equal(s.calls.length,1);s.click('enable');assert.deepEqual(JSON.parse(JSON.stringify(s.calls.at(-1).options.body)),{enabled:true,project_id:'p1'});await s.resolve(s.on());assert.equal(s.q('address').value,'http://127.0.0.1:8800/own');assert.equal(s.q('apply').disabled,false);s.checkSecret();
});
test('project switching or leaving the page suppresses stale writes, reads and credential copies',async()=>{
 const s=fixture();s.mount();await s.resolve(s.on());s.input('http://127.0.0.1:8800/old');s.click('apply');s.state.projectId='p2';s.mount();await s.resolve(s.off(),'/api/mcp/status');const newNode=s.root.querySelector('.mcp-connection');await s.resolve(s.on('p1',{endpoint:'http://127.0.0.1:8800/old',custom_endpoint:true}),'/api/mcp/configure');assert.equal(s.root.querySelector('.mcp-connection'),newNode);assert.equal(s.q('endpoint').textContent,s.off().endpoint);assert.equal(s.toasts.length,0);s.click('enable');s.state.section='skills';await s.reject();assert.equal(s.pending.length,0);assert.equal(s.errors.length,0);s.checkSecret();
});
test('clipboard errors and status address_error never expose server credentials',async()=>{
 const s=fixture();s.mount();await s.resolve(s.on({toString:()=> 'p1'}));assert.equal(s.q('copy').disabled,true);s.state.projectId='p1';s.mount();await s.resolve(s.on('p1',{address_error:'private detail SYNTHETIC-MCP-BEARER-DO-NOT-RENDER',config:{token:'SYNTHETIC-MCP-BEARER-DO-NOT-RENDER'}}));s.setClipboardReject(true);s.click('copy');await s.resolve(s.connection(s.off().endpoint));assert.equal(s.copies.length,0);assert.match(s.errors.at(-1).message,/剪贴板/);s.checkSecret();
});
test('invalid remote, credentials, query and IPv6 inputs never reach configure',async()=>{
 const s=fixture();s.mount();await s.resolve(s.off());for(const endpoint of ['https://127.0.0.1:8795/mcp','http://example.invalid/mcp','http://user:password@127.0.0.1/mcp','http://127.0.0.1/mcp?query=1','http://127.0.0.1/mcp#fragment','http://[::1]:8795/mcp']){s.input(endpoint);s.click('apply');}assert.equal(s.calls.length,1);assert.equal(s.toasts.length,6);
});
test('late credential responses after project or page changes never reach clipboard',async()=>{
 const s=fixture();s.mount();await s.resolve(s.on());s.click('copy');s.state.projectId='p2';await s.resolve(s.connection(s.off().endpoint));assert.equal(s.copies.length,0);s.mount();await s.resolve(s.on('p2'));s.click('copy');s.state.section='skills';await s.resolve(s.connection(s.off().endpoint,'p2'));assert.equal(s.copies.length,0);assert.equal(s.toasts.length,0);s.checkSecret();
});
test('unsupported or absent project makes no requests and a stale initial status cannot overwrite the new mount',async()=>{
 const s=fixture();s.state.projectId='';s.mount();assert.equal(s.calls.length,0);s.state.projectId='p1';s.state.bootstrap.capabilities.mcp_project_read=false;s.mount();assert.equal(s.calls.length,0);s.state.bootstrap.capabilities.mcp_project_read=true;s.mount();s.state.projectId='p2';s.mount();s.pending[1].resolve(s.off({endpoint:'http://127.0.0.1:8801/current',custom_endpoint:true}));s.pending.splice(1,1);await tick();await s.resolve(s.on('p1',{endpoint:'http://127.0.0.1:8800/old',custom_endpoint:true}));assert.equal(s.q('endpoint').textContent,'http://127.0.0.1:8801/current');assert.equal(s.q('address').value,'http://127.0.0.1:8801/current');
});
test('the full URL limit allows a legal 128-character path and only rejects URLs above 256',async()=>{
 const s=fixture();s.mount();await s.resolve(s.off());const endpoint='http://127.0.0.1:8800/'+'p'.repeat(127);assert.equal(new URL(endpoint).pathname.length,128);assert.ok(endpoint.length>128);assert.match(source,/data-mcp-address maxlength="256"/);s.input(endpoint);s.click('apply');assert.equal(s.calls.at(-1).options.body.endpoint,endpoint);await s.resolve(s.off({endpoint,custom_endpoint:true}));assert.equal(s.q('endpoint').textContent,endpoint);const callsBefore=s.calls.length;s.input('http://127.0.0.1:8800/'+'p'.repeat(256));s.click('apply');assert.equal(s.calls.length,callsBefore);assert.match(s.toasts.at(-1),/本机 HTTP 地址/);
});
