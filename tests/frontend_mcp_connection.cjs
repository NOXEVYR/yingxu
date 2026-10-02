'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {test}=require('node:test'),{execFile}=require('node:child_process'),{promisify}=require('node:util'),{pathToFileURL}=require('node:url');

test('MCP is bundled before application startup and has no polling or credential persistence',()=>{
  const index=fs.readFileSync(path.join(__dirname,'../frontend/index.html'),'utf8');
  assert.ok(index.indexOf('/mcp-connection.js')<index.indexOf('/app.js'));
  const source=fs.readFileSync(path.join(__dirname,'../frontend/mcp-connection.js'),'utf8');
  assert.doesNotMatch(source,/setInterval|setTimeout|localStorage|sessionStorage|console\./);
});

test('real Edge MCP explicit authorization, copy secrecy and stale lifecycle',async t=>{
  const browser=[process.env.YINGXU_TEST_BROWSER,'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(value=>value&&fs.existsSync(value));
  if(!browser){t.skip('Existing Chromium required; no downloads');return;}
  const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'yingxu-mcp-ui-'));
  t.after(()=>{const resolved=path.resolve(temporary);assert.ok(resolved.startsWith(path.resolve(os.tmpdir())+path.sep)&&path.basename(resolved).startsWith('yingxu-mcp-ui-'));fs.rmSync(resolved,{recursive:true,force:true,maxRetries:10,retryDelay:100});});
  for(const name of ['mcp-connection.js','workflow-library.js','workflow-library.css','styles.css','appearance.css'])fs.copyFileSync(path.join(__dirname,'../frontend',name),path.join(temporary,name));
  const sharedCopy=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').match(/^async function copyText\(.*$/m);
  assert.ok(sharedCopy,'Exercise the actual shared clipboard fallback in the browser fixture.');
  const runner=`(async()=>{
    const checks=[],check=(name,ok)=>{checks.push({name,ok:!!ok});if(!ok)throw Error(name);},tick=()=>new Promise(resolve=>setTimeout(resolve,0));
    const root=document.querySelector('#root'),calls=[],pending=[],copies=[],errors=[],toasts=[],secret='synthetic-bearer-must-stay-out-of-DOM';
    const state={projectId:'p1',section:'context',selectedIds:new Set(),tabs:[],bootstrap:{capabilities:{mcp_project_read:true}}};
    let clipboardReject=false,sharedCopyCalls=0;
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{if(clipboardReject)throw Error(secret);copies.push(value);}}});
    const env={state,api:(path,options={})=>{calls.push({path,options});return new Promise((resolve,reject)=>pending.push({path,resolve,reject}));},escapeHtml:s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),copyText:async(...args)=>{sharedCopyCalls++;return legacyCopyText(...args);},toast:message=>toasts.push(message),report:error=>errors.push(String(error))};
    const $=selector=>document.querySelector(selector),escapeHtml=env.escapeHtml,toast=env.toast,showDialog=spec=>{const host=document.createElement('div');host.id='fallbackHost';host.innerHTML=spec.body;document.body.append(host);};
    ${sharedCopy[0].replace('async function copyText','async function legacyCopyText')}
    const off={enabled:false,project_id:null,project_name:'',endpoint:'http://127.0.0.1:8791/mcp',default_endpoint:'http://127.0.0.1:8791/mcp',custom_endpoint:false,address_error:'',read_only:true};
    const on=(id='p1',name='第一集')=>({...off,enabled:true,project_id:id,project_name:name});
    const connection=id=>({project_id:id,read_only:true,config:{mcpServers:{yingxu:{url:off.endpoint,headers:{Authorization:'Bearer '+secret}}}}});
    const q=selector=>root.querySelector(selector),resolve=async value=>{pending.shift().resolve(value);await tick();await tick();},reject=async()=>{pending.shift().reject(Error(secret));await tick();await tick();};
    const mount=()=>{root.innerHTML='<div class="handoff-card"></div>';window.YingXuMCP.mount(root,env);};
    state.bootstrap.capabilities.mcp_project_read=false;mount();check('unsupported server creates no MCP controls or requests',!q('.mcp-connection')&&!calls.length);state.bootstrap.capabilities.mcp_project_read=true;
    state.projectId='';mount();check('missing project performs no request',!calls.length);state.projectId='p1';
    mount();check('mount only reads status and locks actions until resolved',calls.length===1&&calls[0].path==='/api/mcp/status'&&!calls[0].options.method&&q('[data-mcp-enable]').disabled&&q('[data-mcp-copy]').disabled);
    await resolve(off);check('default off does not start service and offers explicit enable',q('[data-mcp-status]').textContent==='MCP 已关闭'&&!q('[data-mcp-enable]').disabled&&q('[data-mcp-disable]').disabled&&q('[data-mcp-copy]').disabled&&calls.length===1);
    check('read scope excludes drafts and explains client connection boundary',root.textContent.includes('文本及绑定 SKILL 正文可读')&&root.textContent.includes('Word 和媒体暂提供信息')&&root.textContent.includes('本机 AI 客户端')&&root.textContent.includes('不包含未保存草稿')&&root.textContent.includes('仅提供只读访问')&&root.textContent.includes('客户端是否已连接请在客户端确认'));
    q('[data-mcp-enable]').click();q('[data-mcp-enable]').click();check('enable is busy before await and sends one project-scoped request',calls.length===2&&calls.at(-1).path==='/api/mcp/configure'&&calls.at(-1).options.method==='POST'&&JSON.stringify(calls.at(-1).options.body)===JSON.stringify({enabled:true,project_id:'p1'})&&q('[data-mcp-copy]').disabled);
    await resolve(on());check('enabled status displays current authorized project',q('[data-mcp-status]').textContent.includes('第一集')&&q('[data-mcp-enable]').disabled&&!q('[data-mcp-disable]').disabled&&!q('[data-mcp-copy]').disabled);
    q('[data-mcp-copy]').click();q('[data-mcp-copy]').click();check('copy makes one explicit connection request',calls.length===3&&calls.at(-1).path==='/api/mcp/connection'&&calls.at(-1).options.method==='POST'&&JSON.stringify(calls.at(-1).options.body)==='{}');
    await resolve(connection('p1'));check('copy writes valid HTTP MCP config directly to system clipboard',copies.length===1&&JSON.parse(copies[0]).mcpServers.yingxu.headers.Authorization==='Bearer '+secret&&sharedCopyCalls===0);
    check('bearer never enters DOM or diagnostic messages',!document.body.innerHTML.includes(secret)&&errors.every(value=>!value.includes(secret))&&toasts.every(value=>!value.includes(secret)));
    await tick();await tick();check('idle creates no additional requests',calls.length===3);
    clipboardReject=true;await legacyCopyText('ordinary noncredential text');check('actual shared helper creates a fallback textarea on clipboard rejection',$('#copyFallback')?.value==='ordinary noncredential text');$('#fallbackHost').remove();
    const toastBefore=toasts.length;q('[data-mcp-copy]').click();await resolve(connection('p1'));
    check('MCP clipboard rejection bypasses shared fallback and cannot expose bearer in DOM',sharedCopyCalls===0&&!$('#copyFallback')&&!document.body.innerHTML.includes(secret)&&[...document.querySelectorAll('input,textarea')].every(node=>!node.value.includes(secret))&&copies.length===1);
    check('clipboard rejection releases busy controls and reports only a fixed safe message',!q('[data-mcp-copy]').disabled&&errors.length===1&&!errors[0].includes(secret)&&errors[0].includes('系统剪贴板权限')&&toasts.length===toastBefore);clipboardReject=false;
    q('[data-mcp-disable]').click();q('[data-mcp-disable]').click();check('close is explicit and carries no project switch',calls.length===5&&calls.at(-1).path==='/api/mcp/configure'&&JSON.stringify(calls.at(-1).options.body)==='{"enabled":false}');await resolve(off);
    mount();await resolve(on('p2','<img src=x onerror=window.injected=true>'));
    check('other authorized project is escaped and never automatically switched',!q('img')&&q('[data-mcp-status]').textContent.includes('<img')&&q('[data-mcp-status]').textContent.includes('当前项目尚未授权')&&!q('[data-mcp-enable]').disabled&&q('[data-mcp-copy]').disabled);
    q('[data-mcp-enable]').click();check('switching authorization requires click with current project identity',calls.at(-1).options.body.project_id==='p1');await resolve(on());
    q('[data-mcp-copy]').click();const copyBefore=copies.length;state.projectId='p2';await resolve(connection('p1'));check('late credential response cannot copy after project switch',copies.length===copyBefore);
    mount();await resolve(on('p2'));q('[data-mcp-copy]').click();state.section='skills';await resolve(connection('p2'));check('leaving collaboration suppresses late credential copy',copies.length===copyBefore);state.section='context';
    mount();const detached=q('.mcp-connection');state.projectId='p3';mount();pending[1].resolve(on('p3','最新项目'));await tick();pending.shift().resolve(on('p2','旧项目'));pending.shift();await tick();
    check('late old status cannot overwrite newly mounted project',!detached.isConnected&&q('[data-mcp-status]').textContent.includes('最新项目')&&!q('[data-mcp-status]').textContent.includes('旧项目'));
    q('[data-mcp-copy]').click();await resolve(connection('different-project'));check('credential project identity is checked again before clipboard write',copies.length===copyBefore);
    q('[data-mcp-copy]').click();await resolve({...connection('p3'),read_only:false});check('non-read-only config is never copied',copies.length===copyBefore);
    q('[data-mcp-copy]').click();await reject();check('credential request failure only reports a sanitized message',errors.length===2&&!errors.at(-1).includes(secret)&&!document.body.innerHTML.includes(secret));
    const callsBeforeMissing=calls.length;Object.defineProperty(navigator,'clipboard',{configurable:true,value:undefined});q('[data-mcp-copy]').click();await tick();
    check('missing system clipboard never fetches credentials or creates a DOM fallback',calls.length===callsBeforeMissing&&sharedCopyCalls===0&&!$('#copyFallback')&&!q('[data-mcp-copy]').disabled&&errors.at(-1).includes('系统剪贴板权限'));
    mount();await resolve(off);q('[data-mcp-enable]').click();const old=q('.mcp-connection');state.projectId='p4';mount();pending[0].resolve(on('p3'));pending.shift();await tick();check('stale configure callback cannot claim current project is enabled',!old.isConnected&&q('[data-mcp-status]').textContent.includes('正在读取'));await resolve(off);
    q('[data-mcp-enable]').click();await reject();check('unknown configure outcome locks actions while rereading status',q('[data-mcp-enable]').disabled&&q('[data-mcp-copy]').disabled&&pending.length===1&&pending[0].path==='/api/mcp/status');
    await resolve(on('p4'));check('status reread restores actual authorization after unknown configure outcome',q('[data-mcp-enable]').disabled&&!q('[data-mcp-disable]').disabled&&!q('[data-mcp-copy]').disabled&&q('[data-mcp-status]').textContent.includes('第一集')&&errors.every(value=>!value.includes(secret)));
    mount();await resolve(off);q('[data-mcp-enable]').click();await reject();await reject();check('failed recovery status leaves authorization locked and sanitized',q('[data-mcp-enable]').disabled&&q('[data-mcp-copy]').disabled&&q('[data-mcp-status]').textContent.includes('无法确认')&&pending.length===0&&errors.every(value=>!value.includes(secret)));
    mount();await reject();check('status failure locks controls and sanitizes diagnostics',q('[data-mcp-enable]').disabled&&q('[data-mcp-copy]').disabled&&errors.every(value=>!value.includes(secret)));
    root.innerHTML='<div class="handoff-card"></div>';const workflow=window.YingXuWorkflow.create({...env,showDialog:()=>{}});workflow.mountHandoff(root);check('workflow handoff mounts MCP while retaining manual handoff',!!q('.mcp-connection')&&!!q('[data-generate]')&&calls.at(-1).path==='/api/mcp/status');await resolve(off);
    document.querySelector('#result').textContent=JSON.stringify({checks,errors});
  })().catch(error=>{document.querySelector('#result').textContent=JSON.stringify({error:String(error),stack:error.stack});});`;
  fs.writeFileSync(path.join(temporary,'runner.js'),runner);
  fs.writeFileSync(path.join(temporary,'fixture.html'),'<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="styles.css"><link rel="stylesheet" href="appearance.css"><link rel="stylesheet" href="workflow-library.css"><div id="root"></div><pre id="result"></pre><script src="mcp-connection.js"></script><script src="workflow-library.js"></script><script src="runner.js"></script>');
  const {stdout,stderr}=await promisify(execFile)(browser,['--headless','--disable-gpu','--no-first-run','--disable-background-networking',`--user-data-dir=${path.join(temporary,'profile')}`,'--window-size=1000,900','--virtual-time-budget=10000','--dump-dom',pathToFileURL(path.join(temporary,'fixture.html')).href],{windowsHide:true,timeout:30000,maxBuffer:3*1024*1024});
  const match=stdout.match(/<pre id="result">([\s\S]*?)<\/pre>/);assert.ok(match&&match[1].trim(),stdout.slice(-2000)+'\n'+stderr.slice(-2000));
  const result=JSON.parse(match[1].replace(/&quot;/g,'"').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>'));assert.equal(result.error,undefined,JSON.stringify(result));
  assert.ok(result.checks.length>=22);for(const row of result.checks)assert.equal(row.ok,true,row.name);t.diagnostic(result.checks.map(row=>row.name).join('; '));
});
