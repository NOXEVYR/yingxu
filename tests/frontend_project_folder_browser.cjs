'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),http=require('node:http'),{spawn}=require('node:child_process');
test('real browser library drop is exclusive, shows project hint, and opens copied project',async t=>{
 const browser=[process.env.YINGXU_TEST_BROWSER,'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(p=>p&&fs.existsSync(p));
 if(!browser){t.skip('Requires an existing Chromium browser');return;}
 const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'yingxu-project-folder-browser-'));
 const front=path.join(__dirname,'../frontend'),requests=[];
 let finish;const completed=new Promise(r=>finish=r);
 const runner=`(async()=>{
  const results=[],check=(name,ok)=>{results.push({name,ok});if(!ok)throw Error(name);};
  const sent=[],selected=[];let guard=0;
  try{
   Object.assign(state,{projectId:'original',projectLibraryFolder:'category',projectLibrary:{folders:[{id:'category'}],projects:[{id:'copied',folder_id:'category'}]}});
   guardProperties=async()=>{guard++;return true;};toast=()=>{};report=e=>{throw e;};
   refreshProjects=async()=>{};loadItems=async()=>{};selectProject=async id=>selected.push(id);renderWorkspace=()=>{};configureSection=()=>{};
   resolveDroppedPaths=async()=>['C:/synthetic/folder'];
   api=async(url,opt)=>{sent.push({url,opt});if(url==='/api/project-library/open-folder')return {job_id:'fixture-job'};if(url==='/api/jobs/fixture-job')return {state:'done',done:2,project_id:'copied'};throw Error('Unexpected '+url);};
   wireDragAndDrop();
   const button=document.querySelector('#projectLibraryButton'),viewport=document.querySelector('#resourceViewport');
   const transfer=new DataTransfer();transfer.items.add(new File([],'synthetic-folder'));
   button.dispatchEvent(new DragEvent('dragenter',{bubbles:true,cancelable:true,dataTransfer:transfer}));
   const hover=new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:transfer});button.dispatchEvent(hover);
   check('project target cancels browser navigation',hover.defaultPrevented);
   check('project copy hint replaces current-resource hint',getComputedStyle(button,'::after').content.includes('复制文件夹')&&getComputedStyle(viewport,'::after').display==='none');
   const drop=new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer});button.dispatchEvent(drop);
   for(let i=0;i<100&&state.uploading;i++)await new Promise(r=>setTimeout(r,10));
   check('drop is consumed exactly once',drop.defaultPrevented&&sent.filter(r=>r.url==='/api/project-library/open-folder').length===1&&!sent.some(r=>r.url==='/api/import'));
   check('selected library category reaches backend',sent[0].opt.body.folder_id==='category');
   check('copied project opens and busy releases',selected[0]==='copied'&&!state.uploading&&guard>=1);
   check('drag overlay cleared',!document.body.classList.contains('external-drag')&&!document.body.classList.contains('project-library-file-drag'));
   projectFolderImportDialog();
   check('fallback offers ordinary folder path dialog',document.querySelector('#appDialog').open&&!!document.querySelector('#pickProjectFolder'));
   document.querySelector('#projectFolderPath').value='C:/synthetic/second';
   document.querySelector('#dialogForm').requestSubmit();
   for(let i=0;i<100&&document.querySelector('#appDialog').open;i++)await new Promise(r=>setTimeout(r,10));
   check('picker submission copies once and closes dialog',sent.filter(r=>r.url==='/api/project-library/open-folder').length===2&&!document.querySelector('#appDialog').open);
   Object.assign(state,{projectId:'original',section:'assets',category:'characters',folderId:'target',bootstrap:{token:'synthetic'}});
   window.yingxuDesktopDropPaths=false;
   const folders=[];
   api=async(url,opt)=>{if(url!=='/api/folders')throw Error(url);folders.push(opt.body);return {id:'created-'+folders.length};};
   const fileEntry={name:'note.md',isFile:true,file:ok=>ok(new File(['# synthetic'],'note.md'))};
   const entry={name:'resource-folder',isDirectory:true,createReader(){let read=false;return {readEntries:ok=>{ok(read?[]:[fileEntry]);read=true;}};}};
   const resourceTransfer=new DataTransfer();resourceTransfer.items.add(new File([],'resource-folder'));
   // Synthetic directory entry exercises the real DOM drop route and byte-upload XHR.
   const itemPrototype=Object.getPrototypeOf(resourceTransfer.items[0]),originalEntry=itemPrototype.webkitGetAsEntry;
   itemPrototype.webkitGetAsEntry=function(){return this.getAsFile()?.name==='resource-folder'?entry:originalEntry.call(this);};
   viewport.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:resourceTransfer}));
   itemPrototype.webkitGetAsEntry=originalEntry;
   for(let i=0;i<100&&state.uploading;i++)await new Promise(r=>setTimeout(r,10));
   check('resource drop creates folder at captured destination '+JSON.stringify(folders),folders.length===1&&folders[0].name==='resource-folder'&&folders[0].parent_id==='target');
   const received=await fetch('/uploads').then(r=>r.json());
   check('nested file uploads as bytes into new folder',received.length===1&&received[0].folder==='created-1'&&received[0].body==='# synthetic');
   check('resource folder import releases busy state',!state.uploading);
   await fetch('/result',{method:'POST',body:JSON.stringify({results})});
  }catch(e){await fetch('/result',{method:'POST',body:JSON.stringify({results,error:String(e)})});}
 })();`;
 const server=http.createServer((req,res)=>{
  if(req.url.startsWith('/api/upload?')){let body='';req.on('data',c=>body+=c);req.on('end',()=>{requests.push({folder:new URL(req.url,'http://localhost').searchParams.get('folder_id'),body});res.setHeader('Content-Type','application/json');res.end(JSON.stringify({id:'uploaded'}));});return;}
  if(req.url==='/uploads'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify(requests));return;}
  if(req.url==='/result') {let body='';req.on('data',c=>body+=c);req.on('end',()=>{res.end('ok');finish(JSON.parse(body));});return;}
  let body;
  if(req.url==='/')body=fs.readFileSync(path.join(front,'index.html'),'utf8').replace('</body>','<script src="/folder-runner.js" defer></script></body>');
  else if(req.url==='/folder-runner.js')body=runner;
  else if(req.url==='/app.js')body=fs.readFileSync(path.join(front,'app.js'),'utf8').replace(/boot\(\);\s*$/,'');
  else{const file=path.resolve(front,'.'+req.url);if(!file.startsWith(front+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}body=fs.readFileSync(file);}
  res.setHeader('Content-Type',req.url.endsWith('.js')?'text/javascript; charset=utf-8':req.url.endsWith('.css')?'text/css; charset=utf-8':'text/html; charset=utf-8');res.end(body);
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const child=spawn(browser,['--headless','--disable-gpu','--no-first-run','--disable-background-networking','--user-data-dir='+path.join(temporary,'profile'),'http://127.0.0.1:'+server.address().port],{windowsHide:true,stdio:'ignore'});
 let timer;
 try{const result=await Promise.race([completed,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('Browser folder fixture timeout')),30000);child.once('error',reject);child.once('exit',()=>reject(Error('Browser exited before folder fixture')));})]);assert.equal(result.error,undefined,JSON.stringify(result));assert.ok(result.results.every(r=>r.ok));console.log(result.results.map(r=>r.name).join('\n'));}
 finally{clearTimeout(timer);if(child.exitCode===null){const exited=new Promise(r=>child.once('exit',r));child.kill();await exited;}await new Promise(r=>server.close(r));const resolved=path.resolve(temporary);assert.equal(path.dirname(resolved),path.resolve(os.tmpdir()));assert.ok(path.basename(resolved).startsWith('yingxu-project-folder-browser-'));fs.rmSync(resolved,{recursive:true,force:true,maxRetries:20,retryDelay:100});}
});
