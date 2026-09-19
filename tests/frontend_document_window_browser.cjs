'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),http=require('node:http'),{spawn}=require('node:child_process');
test('document-only layout preserves editor and exposes a fixed workbench toggle',async t=>{
 const browser=[process.env.YINGXU_TEST_BROWSER,'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(p=>p&&fs.existsSync(p));
 if(!browser){t.skip('Requires an existing Chromium browser');return;}
 const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'yingxu-document-window-browser-'));
 const front=path.join(__dirname,'../frontend');
 let finish;const completed=new Promise(r=>finish=r);
 const runner=`(async()=>{
 const results=[],check=(name,ok)=>{results.push({name,ok});if(!ok)throw Error(name);};
 try{
  state.bootstrap={capabilities:{lazy_markdown:true}};applyAppearance();wireEvents();
  api=async()=>({id:'a'.repeat(32),name:'独立文档示例.md',kind:'markdown',path:'C:/synthetic/example.md',external:true,content:{editable:true,content:'# 独立文档\\n\\n专注阅读与编辑，右上角可以展开完整工作台。\\n\\n'+Array(80).fill('正文内容，用于检查滚动时展开按钮保持可见。\\n\\n').join(''),etag:'fixture'}});
  await openExternal('a'.repeat(32));
  const t=activeTab(),instance=t.markdownEditor,button=$('#documentWindowToggle');
  const visible=s=>{const r=$(s).getBoundingClientRect();return getComputedStyle($(s)).display!=='none'&&r.width>0&&r.height>0;};
  await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
  check('external Markdown starts document-only',t.documentOnly&&visible('#editor')&&!visible('.sidebar')&&!visible('.topbar')&&!visible('#library')&&!visible('#inspector'));
  check('hidden panels have no misleading toolbar switches',!visible('[data-action=toggle-inspector]')&&!visible('[data-action=toggle-reading-library]'));
  check('document fills available window',$('#editor').getBoundingClientRect().width>=innerWidth-2);
  check('editor is live',!!instance&&!!document.querySelector('.cm-editor'));
  const originalDraft=t.draft;instance.insertText('未保存',t.draft.length,t.draft.length);
  check('real editor edit marks dirty',t.dirty&&t.draft.endsWith('未保存'));
  state.projectId='1'.repeat(32);state.items=[{id:'c'.repeat(32),project_id:state.projectId}];state.selectedIds=new Set(['c'.repeat(32)]);
  $('#resourceItems').innerHTML='<article class=resource-card data-item='+('c'.repeat(32))+'></article>';
  let deleted=0;trashItems=async()=>deleted++;button.focus();
  button.dispatchEvent(new KeyboardEvent('keydown',{key:'Delete',bubbles:true,cancelable:true}));
  check('document-only Delete cannot delete hidden selected resources',deleted===0);
  check('document-only paste cannot import into hidden project',!resourcePasteAllowed(button));
  let imported=0;importResourceDrop=async()=>imported++;
  const transfer=new DataTransfer();transfer.items.add(new File(['test'],'test.md'));
  const drop=new DragEvent('drop',{dataTransfer:transfer,bubbles:true,cancelable:true});button.dispatchEvent(drop);
  check('toolbar drop cannot import into hidden project',drop.defaultPrevented&&imported===0);
  check('hidden project capture is not exposed',!visible('[data-action=capture-screen]'));
  check('global screenshot shortcut only copies without a hidden project target',captureTarget().projectId==='');
  const before=button.getBoundingClientRect();
  document.querySelector('.cm-scroller').scrollTop=600;
  await new Promise(r=>requestAnimationFrame(r));
  check('expand button stays fixed when document scrolls',button.getBoundingClientRect().top===before.top&&before.right<=innerWidth&&before.top>=0);
  for(let i=0;i<12;i++)state.tabs.push({key:'dummy:'+i,item:{name:'很长的文档标签标题 '+i,kind:'markdown'}});
  renderTabs();
  check('overflowing tabs cannot displace expand button',button.getBoundingClientRect().right<=innerWidth&&button.getBoundingClientRect().width>=30);
  button.click();
  check('expand restores full workbench',!t.documentOnly&&visible('.sidebar')&&visible('.topbar')&&visible('#library')&&visible('#inspector'));
  check('toggle retains editor and dirty draft',t.markdownEditor===instance&&t.dirty&&t.draft.endsWith('未保存'));
  const toolbar=$('#editorToolbar').firstElementChild;
  for(let i=0;i<20;i++)button.click();
  check('repeated toggles reuse toolbar DOM',$('#editorToolbar').firstElementChild===toolbar);
  instance.format('undo');check('undo survives layout toggles',t.draft===originalDraft);
  instance.format('redo');check('redo survives layout toggles',t.draft.endsWith('未保存'));
  button.click();check('can return to document-only',t.documentOnly&&!visible('.sidebar'));
  let searched=0;openDocumentSearch=()=>searched++;
  document.body.dispatchEvent(new KeyboardEvent('keydown',{key:'f',ctrlKey:true,bubbles:true,cancelable:true}));
  check('Ctrl F routes to document despite hidden search bar',searched===1);
  state.activeKey=null;renderWorkspace();
  check('closing last active document restores navigation',visible('.sidebar')&&visible('.topbar')&&!visible('#editor')&&button.hidden);
  state.tabs=[t];state.activeKey=t.key;renderWorkspace();
  await fetch('/result',{method:'POST',body:JSON.stringify({results})});
 }catch(e){await fetch('/result',{method:'POST',body:JSON.stringify({results,error:String(e),stack:e.stack})});}
})();`;
 const server=http.createServer((req,res)=>{
  if(req.url==='/result') {let body='';req.on('data',c=>body+=c);req.on('end',()=>{res.end('ok');finish(JSON.parse(body));});return;}
  let body;
  if(req.url==='/')body=fs.readFileSync(path.join(front,'index.html'),'utf8').replace('</body>','<script src="/document-runner.js" defer></script></body>');
  else if(req.url==='/document-runner.js')body=runner;
  else if(req.url==='/app.js')body=fs.readFileSync(path.join(front,'app.js'),'utf8').replace(/boot\(\);\s*$/,'');
  else{const file=path.resolve(front,'.'+req.url);if(!file.startsWith(front+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}body=fs.readFileSync(file);}
  res.setHeader('Content-Type',req.url.endsWith('.js')?'text/javascript; charset=utf-8':req.url.endsWith('.css')?'text/css; charset=utf-8':'text/html; charset=utf-8');res.end(body);
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const child=spawn(browser,['--headless','--window-size='+(process.env.YINGXU_TEST_WINDOW_SIZE||'1440,900'),'--remote-debugging-port=0','--disable-gpu','--no-first-run','--disable-background-networking','--user-data-dir='+path.join(temporary,'profile'),'http://127.0.0.1:'+server.address().port],{windowsHide:true,stdio:'ignore'});
 async function screenshot(){
  const port=fs.readFileSync(path.join(temporary,'profile/DevToolsActivePort'),'utf8').split('\n')[0];
  const pages=await fetch('http://127.0.0.1:'+port+'/json').then(r=>r.json());
  const page=pages.find(p=>p.type==='page');const ws=new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  try{const image=await new Promise((resolve,reject)=>{
    const deadline=setTimeout(()=>reject(Error('Screenshot timeout')),5000);
    ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id===1){clearTimeout(deadline);m.error?reject(Error(m.error.message)):resolve(m.result.data);}};
    ws.send(JSON.stringify({id:1,method:'Page.captureScreenshot',params:{format:'png'}}));
  });fs.writeFileSync(process.env.YINGXU_TEST_SCREENSHOT,Buffer.from(image,'base64'));}finally{ws.close();}
 }
 let timer;
 try{const result=await Promise.race([completed,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('Browser document fixture timeout')),30000);child.once('error',reject);child.once('exit',()=>reject(Error('Browser exited before document fixture')));})]);assert.equal(result.error,undefined,JSON.stringify(result));assert.ok(result.results.every(r=>r.ok));console.log(result.results.map(r=>r.name).join('\n'));if(process.env.YINGXU_TEST_SCREENSHOT)await screenshot();}
 finally{clearTimeout(timer);if(child.exitCode===null){const exited=new Promise(r=>child.once('exit',r));child.kill();await exited;}await new Promise(r=>server.close(r));const resolved=path.resolve(temporary);assert.equal(path.dirname(resolved),path.resolve(os.tmpdir()));assert.ok(path.basename(resolved).startsWith('yingxu-document-window-browser-'));fs.rmSync(resolved,{recursive:true,force:true,maxRetries:20,retryDelay:100});}
});

