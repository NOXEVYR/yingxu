'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm');
const {test}=require('node:test'),{spawn,execFile}=require('node:child_process'),http=require('node:http'),{randomUUID}=require('node:crypto');
const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');

function cleanupFixture(temporary) {
  const resolved=path.resolve(temporary);
  assert.ok(resolved.startsWith(path.resolve(os.tmpdir())+path.sep)&&path.basename(resolved).startsWith('yingxu-media-lifecycle-'));
  fs.rmSync(resolved,{recursive:true,force:true,maxRetries:10,retryDelay:100});
}
test('release pauses both media kinds and removes direct/nested sources even if one pause or load fails',()=>{
  const events=[];
  const media=[0,1].map(index=>({
    pause(){events.push(`pause${index}`);if(index===0)throw Error('synthetic');},
    removeAttribute(name){events.push(`remove${index}:${name}`);},
    querySelectorAll(){return [{removeAttribute:name=>events.push(`source${index}:${name}`)}];},
    load(){events.push(`load${index}`);if(index===0)throw Error('synthetic');}
  }));
  const context=vm.createContext({window:{},document:{querySelectorAll:()=>media},localStorage:{getItem:()=>null},setTimeout,clearTimeout});
  vm.runInContext(source+';stopPreviewMedia(true);',context);
  assert.deepEqual(events,['pause0','remove0:autoplay','remove0:src','source0:src','load0','pause1','remove1:autoplay','remove1:src','source1:src','load1']);
});
test('hide-only pause does not reload or clear sources and unknown native messages do not affect playback',async()=>{
  let pauses=0;
  const context=vm.createContext({window:{},document:{querySelectorAll:()=>[{pause(){pauses++;}}]},localStorage:{getItem:()=>null},setTimeout,clearTimeout});
  vm.runInContext(source+';globalThis.message=handleDesktopMessage;',context);
  await context.message({action:'unrelated'});assert.equal(pauses,0);
  await context.message({action:'pause-media'});assert.equal(pauses,1);
});

test('real Chromium releases closed/switched media and pauses native/page visibility transitions',async t=>{
  const browser=[process.env.YINGXU_TEST_BROWSER,'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(p=>p&&fs.existsSync(p));
  if(!browser){t.skip('Requires an existing Chromium browser; does not download');return;}
  const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'yingxu-media-lifecycle-'));
  const token=randomUUID();
  let browserProcess,server,timer,settled=false,lastStage='browser startup',browserLog='';
  let resolveResult,rejectResult;
  const completed=new Promise((resolve,reject)=>{resolveResult=resolve;rejectResult=reject;});
  // Attach a handler before starting asynchronous fixtures, so startup failures
  // cannot produce an unhandled rejection while cleanup is being registered.
  completed.catch(()=>{});
  const finish=(error,result)=>{if(settled)return;settled=true;clearTimeout(timer);error?rejectResult(error):resolveResult(result);};
  t.after(async()=>{
    clearTimeout(timer);
    if(browserProcess?.pid&&browserProcess.exitCode===null&&browserProcess.signalCode===null){
      // The unique profile creates our own browser instance. Never terminate a
      // desktop browser by name; close only this spawned process and its tree.
      if(process.platform==='win32')await new Promise(resolve=>execFile('taskkill',['/PID',String(browserProcess.pid),'/T','/F'],{windowsHide:true,timeout:10000},()=>resolve()));
      else {try{process.kill(-browserProcess.pid,'SIGTERM');}catch(error){if(error.code!=='ESRCH')throw error;}}
      if(browserProcess.exitCode===null&&browserProcess.signalCode===null)await new Promise(resolve=>{
        const deadline=setTimeout(()=>{browserProcess.kill('SIGKILL');resolve();},5000);
        browserProcess.once('exit',()=>{clearTimeout(deadline);resolve();});
      });
    }
    if(server)await new Promise(resolve=>{server.close(resolve);server.closeAllConnections();});
    cleanupFixture(temporary);
  });
  // A 30-second silent PCM fixture exercises actual media decode/play/pause.
  // Muting and silence ensure the test never makes sound on the user's desktop.
  const samples=8000*30,wav=Buffer.alloc(44+samples*2);
  wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(8000,24);wav.writeUInt32LE(16000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(samples*2,40);
  fs.writeFileSync(path.join(temporary,'silent.wav'),wav);
  fs.writeFileSync(path.join(temporary,'app.js'),source);
  const runner=`
  (()=>{
  let stage='runner startup';
  const report=(kind,value)=>fetch('/'+kind+'/${token}',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
  const advance=name=>{stage=name;void report('stage',{stage}).catch(()=>{});};
  const bounded=async(name,operation)=>{
    advance(name);let timer;
    try{return await Promise.race([operation(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Timed out: '+name)),10000);})]);}
    finally{clearTimeout(timer);}
  };
  addEventListener('error',event=>void report('result',{error:event.message,stage}).catch(()=>{}));
  addEventListener('unhandledrejection',event=>void report('result',{error:String(event.reason),stage}).catch(()=>{}));
  (async()=>{
    const results=[],check=(name,ok)=>{advance(name);results.push({name,ok});if(!ok)throw new Error(name);};
    const tick=()=>new Promise(resolve=>setTimeout(resolve,50));
    const makeTab=(id,kind='audio')=>({key:'external:'+id,id,source:'external',item:{id,name:'合成媒体',kind,size:480044,media_url:'silent.wav'},mode:'preview',dirty:false,detailReady:true});
    let playback=0;
    const play=async node=>{node.muted=true;await bounded('playback '+(++playback),()=>node.play());check('synthetic media actually starts',!node.paused&&node.readyState>=2);};
    let persisted=0;persistDrafts=()=>{persisted++;};wireEvents();
    const tab=makeTab('first');state.tabs=[tab];state.activeKey=tab.key;renderWorkspace();
    let media=$('#editorContent audio');await play(media);
    await bounded('seek synthetic media',()=>new Promise(resolve=>{media.addEventListener('seeked',resolve,{once:true});media.currentTime=2;}));
    await bounded('native pause',()=>handleDesktopMessage({action:'pause-media'}));
    check('native tray-hide message pauses but retains source and time',media.paused&&media.getAttribute('src')==='silent.wav'&&media.currentTime>=2);
    await play(media);window.dispatchEvent(new Event('pagehide'));
    check('pagehide pauses and persists drafts without releasing source',media.paused&&media.hasAttribute('src')&&persisted>0);
    await play(media);Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));
    check('hidden visibility pauses current media',media.paused&&media.hasAttribute('src'));
    Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));
    check('becoming visible does not automatically resume playback',media.paused);
    await play(media);document.dispatchEvent(new Event('visibilitychange'));
    check('visible visibility event does not interrupt active playback',!media.paused);
    await bounded('close final tab',()=>closeTab(tab.key));await tick();
    check('closing final tab pauses and releases detached media',media.paused&&!media.hasAttribute('src')&&media.readyState===0&&media.networkState===0);
    check('closing final tab clears editor and hides workspace',!$('#editorContent').childElementCount&&$('#editor').hidden&&state.tabs.length===0);
    const videoTab=makeTab('video','video');state.tabs=[videoTab];state.activeKey=videoTab.key;renderWorkspace();
    const oldVideo=$('#editorContent video');let videoPauses=0;
    const nativeVideoPause=oldVideo.pause.bind(oldVideo);oldVideo.pause=()=>{videoPauses++;nativeVideoPause();};
    check('video fixture uses actual media element and source',oldVideo instanceof HTMLVideoElement&&oldVideo.getAttribute('src')==='silent.wav');
    const next=makeTab('next');state.tabs.push(next);state.activeKey=next.key;renderWorkspace();await tick();
    check('switching tab pauses and releases old video element',videoPauses===1&&oldVideo.paused&&!oldVideo.hasAttribute('src')&&oldVideo.readyState===0&&oldVideo.networkState===0);
    media=$('#editorContent audio');await play(media);next.loading=true;renderEditorBody(next);await tick();
    check('loading a replacement clears old audio',media.paused&&!media.hasAttribute('src')&&media.readyState===0&&!!$('#editorContent .editor-loading'));
    $('#editorContent').innerHTML='<audio autoplay><source src="silent.wav" type="audio/wav"></audio>';
    media=$('#editorContent audio');stopPreviewMedia(true);await tick();
    check('nested source and autoplay are released',media.paused&&!media.hasAttribute('autoplay')&&!media.querySelector('source').hasAttribute('src')&&media.readyState===0);
    stopPreviewMedia(true);check('repeated cleanup remains safe',media.paused);
    await report('result',results);
  })().catch(error=>report('result',{error:String(error),stack:error.stack,stage}));
  })();`;
  fs.writeFileSync(path.join(temporary,'runner.js'),runner);
  const html=fs.readFileSync(path.join(__dirname,'../frontend/index.html'),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'').replace('</body>','<pre id="result"></pre><script src="app.js"></script><script src="runner.js"></script></body>');
  fs.writeFileSync(path.join(temporary,'fixture.html'),html);
  const fixtures=new Map([['/fixture.html',['text/html; charset=utf-8',Buffer.from(html)]],['/app.js',['text/javascript; charset=utf-8',Buffer.from(source)]],['/runner.js',['text/javascript; charset=utf-8',Buffer.from(runner)]],['/silent.wav',['audio/wav',wav]]]);
  server=http.createServer((request,response)=>{
    if(request.method==='POST'&&['/stage/'+token,'/result/'+token].includes(request.url)){
      let body='';request.setEncoding('utf8');
      request.on('data',chunk=>{body+=chunk;if(body.length>65536)request.destroy();});
      request.on('end',()=>{
        try{const value=JSON.parse(body);response.writeHead(204);response.end();if(request.url.startsWith('/stage/'))lastStage=value.stage;else finish(null,value);}
        catch(error){response.writeHead(400);response.end();finish(error);}
      });return;
    }
    const fixture=fixtures.get(request.url);
    if(request.method!=='GET'||!fixture){response.writeHead(404);response.end();return;}
    const [type,data]=fixture;
    const headers={'Content-Type':type,'Cache-Control':'no-store','Accept-Ranges':'bytes'};
    if(request.headers.range){
      const range=/^bytes=(\d+)-(\d*)$/.exec(request.headers.range);
      const start=range?Number(range[1]):NaN,end=range&&range[2]?Math.min(Number(range[2]),data.length-1):data.length-1;
      if(!Number.isSafeInteger(start)||start>end){response.writeHead(416,{'Content-Range':`bytes */${data.length}`});response.end();return;}
      response.writeHead(206,{...headers,'Content-Length':end-start+1,'Content-Range':`bytes ${start}-${end}/${data.length}`});response.end(data.subarray(start,end+1));return;
    }
    response.writeHead(200,{...headers,'Content-Length':data.length});response.end(data);
  });
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  // Media decoding runs on real time. Virtual-time dump-dom can finish before
  // play() settles, yielding an empty marker despite no application failure.
  timer=setTimeout(()=>finish(new Error('Browser result timeout; last stage: '+lastStage+'\n'+browserLog)),45000);
  browserProcess=spawn(browser,['--headless','--disable-gpu','--no-first-run','--disable-background-networking','--autoplay-policy=no-user-gesture-required','--mute-audio',`--user-data-dir=${path.join(temporary,'profile')}`,`http://127.0.0.1:${server.address().port}/fixture.html`],{windowsHide:true,detached:process.platform!=='win32',stdio:['ignore','pipe','pipe']});
  for(const stream of [browserProcess.stdout,browserProcess.stderr])stream.on('data',chunk=>{browserLog=(browserLog+chunk).slice(-8000);});
  browserProcess.once('error',error=>finish(error));
  browserProcess.once('exit',(code,signal)=>finish(new Error('Browser exited before result: '+code+'/'+signal+'; stage: '+lastStage+'\n'+browserLog)));
  const result=await completed;
  assert.ok(Array.isArray(result),JSON.stringify(result));assert.equal(result.length,17);for(const row of result)assert.equal(row.ok,true,row.name);
});
