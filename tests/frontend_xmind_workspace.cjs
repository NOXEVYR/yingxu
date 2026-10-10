'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm');
const {test}=require('node:test'),{execFile}=require('node:child_process'),{promisify}=require('node:util'),{pathToFileURL}=require('node:url');
test('rebuilt XMind workspace direct manipulation, clipboard, camera, history and bounds in Chromium',async t=>{
  const browser=[process.env.YINGXU_TEST_BROWSER,'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(p=>p&&fs.existsSync(p));
  if(!browser){t.skip('Existing Chromium required');return;}
  const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'yingxu-xmind-workspace-'));
  t.after(()=>{const resolved=path.resolve(tmp);assert.ok(resolved.startsWith(path.resolve(os.tmpdir())+path.sep)&&path.basename(resolved).startsWith('yingxu-xmind-workspace-'));fs.rmSync(resolved,{recursive:true,force:true,maxRetries:10,retryDelay:100});});
  for(const file of ['xmind-editor.js','xmind-editor.css','styles.css','appearance.css'])fs.copyFileSync(path.join(__dirname,'../frontend',file),path.join(tmp,file));
  fs.copyFileSync(path.join(__dirname,'fixtures/xmind-workspace-runner.js'),path.join(tmp,'runner.js'));
  fs.writeFileSync(path.join(tmp,'fixture.html'),'<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="styles.css"><link rel="stylesheet" href="appearance.css"><link rel="stylesheet" href="xmind-editor.css"><style>body{display:block}#mount{width:1100px;height:780px}</style><div id="mount"></div><pre id="result"></pre><script src="xmind-editor.js"></script><script src="runner.js"></script>');
  const {stdout}=await promisify(execFile)(browser,['--headless','--disable-gpu','--no-first-run','--disable-background-networking',`--user-data-dir=${path.join(tmp,'profile')}`,'--virtual-time-budget=10000','--dump-dom',pathToFileURL(path.join(tmp,'fixture.html')).href],{windowsHide:true,timeout:45000,maxBuffer:4*1024*1024});
  const match=stdout.match(/<pre id="result">([^<]+)<\/pre>/);assert.ok(match,stdout.slice(-3000));
  const rows=JSON.parse(match[1].replace(/&quot;/g,'"').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>'));
  assert.ok(Array.isArray(rows),JSON.stringify(rows));assert.ok(rows.length>=45,rows.length);assert.deepEqual(rows.filter(row=>!row.ok),[],JSON.stringify(rows.filter(row=>!row.ok)));
});
test('XMind tab disables native interface zoom and releases routing on return to text',()=>{
  const messages=[],nodes=new Map();const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{classList:{toggle(){}},replaceChildren(){},hidden:false});return nodes.get(selector);};
  const context=vm.createContext({console,setTimeout,clearTimeout,window:{chrome:{webview:{postMessage:message=>messages.push(message)}}},document:{querySelector:node},localStorage:{getItem:()=>null,setItem(){}}});
  const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
  vm.runInContext(source+'\nglobalThis.workspace={state,renderWorkspace};',context);
  vm.runInContext('applyReadingLayout=applyWorkbenchLayout=discardUnusedMarkdownEditors=renderTabs=renderInspector=renderEditorToolbar=renderEditorBody=renderEditorStatus=()=>{};',context);
  for(const kind of ['xmind','image','text']){context.workspace.state.tabs=[{key:'test',item:{kind}}];context.workspace.state.activeKey='test';context.workspace.state.section='none';context.workspace.renderWorkspace();}
  assert.deepEqual(messages.map(m=>m.active),[true,true,false]);assert.ok(messages.every(m=>m.action==='image-preview'));
  let disposed=0;context.dispose=()=>disposed++;vm.runInContext('globalThis.disposable={xmindEditor:{destroy:dispose}};activeXmindEditorTab=disposable;unmountDocxPreview=unmountDocxEditor=stopImageZoomTracking=stopPreviewMedia=()=>{};',context);context.workspace.state.tabs=[];context.workspace.state.activeKey=null;context.workspace.renderWorkspace();assert.equal(disposed,1);assert.equal(context.disposable.xmindEditor,undefined);assert.equal(messages.at(-1).active,false);
});
