'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {test}=require('node:test'),{spawn,execFile}=require('node:child_process'),http=require('node:http'),{promisify}=require('node:util');
test('real browser routes synthetic image paste and menu actions without accessing the system clipboard',async t=>{
  const browser=[process.env.YINGXU_TEST_BROWSER,'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(p=>p&&fs.existsSync(p));
  if(!browser){t.skip('Requires existing Chromium');return;}
  const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'yingxu-clipboard-browser-'));
  t.after(()=>{const resolved=path.resolve(temporary);assert.equal(path.dirname(resolved),path.resolve(os.tmpdir()));assert.ok(path.basename(resolved).startsWith('yingxu-clipboard-browser-'));fs.rmSync(resolved,{recursive:true,force:true,maxRetries:10,retryDelay:100});});
  const front=path.join(__dirname,'../frontend');
  fs.writeFileSync(path.join(temporary,'app.js'),fs.readFileSync(path.join(front,'app.js'),'utf8').replace(/boot\(\);\s*$/,''));
  fs.writeFileSync(path.join(temporary,'runner.js'),`
  (async()=>{
    const results=[],check=(name,ok)=>{results.push({name,ok});if(!ok)throw Error(name);},sent=[],native=[];
    const tick=()=>new Promise(r=>setTimeout(r,20));
    let releaseRefresh=null;
    const png=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='),c=>c.charCodeAt(0));
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{read:async()=>{throw Error('synthetic denied');}}});
    window.XMLHttpRequest=class{constructor(){this.upload={};}open(method,url){this.url=url;}setRequestHeader(){}send(file){sent.push({file,url:this.url});this.status=201;this.responseText=JSON.stringify({id:'synthetic'});queueMicrotask(()=>this.onload());}};
    toast=()=>{};report=error=>{throw error;};loadItems=async()=>{};guardProperties=async()=>true;
    refreshProjects=()=>new Promise(resolve=>releaseRefresh=resolve);
    api=async(url,options)=>{native.push({url,options});return {items:[{id:'native-image'}]};};
    Object.assign(state,{projectId:'fixture-project',category:'characters',folderId:'nested',section:'assets',bootstrap:{token:'synthetic'}});wireEvents();
    const blank=document.querySelector('#resourceViewport');blank.tabIndex=0;blank.focus();
    const transfer=new DataTransfer();transfer.items.add(new File([png],'image.png',{type:'image/png'}));
    const paste=new ClipboardEvent('paste',{clipboardData:transfer,bubbles:true,cancelable:true});blank.dispatchEvent(paste);await tick();
    check('real ClipboardEvent uploads exactly one image',paste.defaultPrevented&&sent.length===1&&native.length===0);
    const params=new URL(sent[0].url,'http://127.0.0.1').searchParams;
    check('image destination includes project category and folder',params.get('project')==='fixture-project'&&params.get('category')==='characters'&&params.get('folder_id')==='nested');
    check('original pasted PNG bytes are preserved',Array.from(new Uint8Array(await sent[0].file.arrayBuffer())).join()===Array.from(png).join());
    check('busy remains held while refreshing after upload',state.uploading&&!!releaseRefresh);
    blank.dispatchEvent(new ClipboardEvent('paste',{clipboardData:transfer,bubbles:true,cancelable:true}));await tick();
    check('second paste while refresh waits does not duplicate upload',sent.length===1);
    releaseRefresh();await tick();check('refresh completion releases busy',!state.uploading);
    refreshProjects=async()=>{};
    const input=document.querySelector('#searchInput');input.focus();const textPaste=new ClipboardEvent('paste',{clipboardData:transfer,bubbles:true,cancelable:true});input.dispatchEvent(textPaste);await tick();
    check('input retains native paste behavior',!textPaste.defaultPrevented&&sent.length===1);
    blank.focus();showMenu(blank,'location',{project_id:'menu-project',category:'scenes',folder_id:'menu-folder'});
    document.querySelector('[data-menu-command="paste-files"]').click();await tick();
    check('actual right-click menu falls back to native image reader',native.length===1&&native[0].url==='/api/clipboard/paste'&&native[0].options.body.project_id==='menu-project'&&native[0].options.body.folder_id==='menu-folder');
    navigator.clipboard.read=async()=>[{types:['image/png'],getType:async()=>new Blob([png],{type:'image/png'})}];
    blank.focus();await pasteResourceFiles({project_id:'browser-image',category:'props',folder_id:null});
    check('browser clipboard image uses upload with PNG filename',sent.length===2&&sent[1].file.name.endsWith('.png')&&native.length===1);
    await fetch('/result',{method:'POST',body:JSON.stringify(results)});
  })().catch(error=>fetch('/result',{method:'POST',body:JSON.stringify({error:String(error),stack:error.stack})}));`);
  const html=fs.readFileSync(path.join(front,'index.html'),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'').replace('</body>','<pre id="result"></pre><script src="app.js"></script><script src="runner.js"></script></body>');
  fs.writeFileSync(path.join(temporary,'fixture.html'),html);
  // Virtual-time --dump-dom can exhaust its timer budget while File.arrayBuffer()
  // is still waiting on real browser I/O. Await an explicit fixture result instead.
  let deliver;const completed=new Promise(resolve=>{deliver=resolve;});
  const server=http.createServer((req,res)=>{
    if(req.method==='POST'&&req.url==='/result') {
      let body='';req.on('data',chunk=>{body+=chunk;if(body.length>65536)req.destroy();});
      req.on('end',()=>{res.end('ok');try{deliver(JSON.parse(body));}catch(error){deliver({error:String(error)});}});return;
    }
    const name={'/fixture.html':'fixture.html','/app.js':'app.js','/runner.js':'runner.js'}[req.url];
    if(!name){res.writeHead(404);res.end();return;}
    res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript; charset=utf-8':'text/html; charset=utf-8');
    res.end(fs.readFileSync(path.join(temporary,name)));
  });
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  const child=spawn(browser,['--headless','--disable-gpu','--no-first-run','--disable-background-networking',`--user-data-dir=${path.join(temporary,'profile')}`,`http://127.0.0.1:${server.address().port}/fixture.html`],{windowsHide:true,stdio:['ignore','ignore','pipe']});
  let stderr='',timer;child.stderr.on('data',chunk=>{stderr=(stderr+chunk).slice(-4000);});
  const failed=new Promise((_,reject)=>{child.once('error',reject);child.once('exit',(code,signal)=>reject(Error(`Fixture browser exited before result (${code}/${signal}): ${stderr}`)));});
  try {
    const results=await Promise.race([completed,failed,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('Browser fixture did not finish within 30 seconds: '+stderr)),30000);})]);
    assert.ok(Array.isArray(results),JSON.stringify(results));assert.equal(results.length,9);for(const row of results)assert.equal(row.ok,true,row.name);
  } finally {
    clearTimeout(timer);
    if(child.exitCode===null&&child.pid) {
      if(process.platform==='win32') await promisify(execFile)('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true}).catch(()=>{});
      else child.kill('SIGTERM');
      if(child.exitCode===null) await new Promise(resolve=>{const limit=setTimeout(resolve,3000);child.once('exit',()=>{clearTimeout(limit);resolve();});});
    }
    server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
  }
});
