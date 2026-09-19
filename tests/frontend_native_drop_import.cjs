'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function setup({native=true,supported=true}={}){
 const nodes=new Map(),listeners=new Set(),messages=[],requests=[],uploads=[],jobs=[],notices=[];
 const node=k=>{if(!nodes.has(k))nodes.set(k,{});return nodes.get(k);};
 const bridge={addEventListener:(t,f)=>listeners.add(f),removeEventListener:(t,f)=>listeners.delete(f)};
 if(supported)bridge.postMessageWithAdditionalObjects=(message,objects)=>messages.push({message,objects});
 const c=vm.createContext({window:{yingxuDesktopDropPaths:native,chrome:{webview:bridge}},document:{querySelector:node},localStorage:{getItem:()=>null},console,crypto:{randomUUID:()=> '11111111-1111-1111-1111-111111111111'},setTimeout:f=>setTimeout(f,30),clearTimeout,requests,uploads,jobs,notices});
 const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
 vm.runInContext(source+`\napi=async(...args)=>{requests.push(args);return {job_id:'job'};};monitorJob=async(...args)=>{jobs.push(args);return {state:'done'};};uploadFiles=async(...args)=>uploads.push(args);toast=m=>notices.push(m);globalThis.app={state,importResourceDrop};`,c);
 Object.assign(c.app.state,{projectId:'source-project'});
 const reply=(paths,error,requestId=messages[0]?.message.requestId)=>{for(const f of [...listeners])f({data:{action:'resolved-drop-files',requestId,paths,error}});};
 return {app:c.app,c,bridge,node,listeners,messages,requests,uploads,jobs,notices,reply};
}
test('desktop external drop resolves genuine File objects and copies to captured project/category/folder',async()=>{
 const s=setup(),files=[{name:'中文 图.png'},{name:'b.jpg'}];const task=s.app.importResourceDrop(files,'characters','nested');
 assert.equal(s.app.state.uploading,true);assert.equal(s.messages[0].objects,files);assert.equal(s.requests.length,0);
 s.app.state.projectId='other';s.reply(['C:\\images\\中文 图.png','C:\\images\\b.jpg']);await task;
 const [url,options]=s.requests[0];assert.equal(url,'/api/import');assert.deepEqual(JSON.parse(JSON.stringify(options.body)),{project_id:'source-project',category:'characters',folder_id:'nested',paths:['C:\\images\\中文 图.png','C:\\images\\b.jpg'],mode:'copy'});
 assert.equal(s.uploads.length,0);assert.equal(s.jobs[0][0],'job');assert.equal(s.app.state.uploading,false);assert.equal(s.listeners.size,0);
});
test('resolution keeps import busy and blocks a second drop',async()=>{
 const s=setup(),task=s.app.importResourceDrop([{name:'a.png'}],'props');await s.app.importResourceDrop([{name:'b.png'}],'scenes');
 assert.equal(s.messages.length,1);s.reply(['C:\\a.png']);await task;assert.equal(s.requests.length,1);
});
test('desktop directory uses original root path without browser traversal or flattening',async()=>{
 const s=setup(),entry={isDirectory:true,createReader(){throw new Error('native drop must not enumerate in browser');}};
 const task=s.app.importResourceDrop([{name:'素材'}],'characters','selected',[entry]);
 s.reply(['C:\\synthetic\\素材']);await task;
 assert.equal(s.requests.length,1);assert.equal(s.requests[0][1].body.paths[0],'C:\\synthetic\\素材');assert.equal(s.requests[0][1].body.folder_id,'selected');assert.equal(s.uploads.length,0);
});
for(const options of [{native:false},{supported:false}])test(`browser/old host retains byte upload ${JSON.stringify(options)}`,async()=>{
 const s=setup(options);await s.app.importResourceDrop([{name:'a.png'}],'scenes','folder');assert.equal(s.uploads.length,1);assert.equal(s.requests.length,0);
});
test('pathless synthetic files fall back as a whole batch without creating an import job',async()=>{
 const s=setup(),task=s.app.importResourceDrop([{name:'paste.png'}],'props');s.reply(null);await task;assert.equal(s.uploads.length,1);assert.equal(s.requests.length,0);
});
test('unrelated native response cannot redirect import; timeout never writes',async()=>{
 const s=setup(),task=s.app.importResourceDrop([{name:'a.png'}],'props');s.reply(['C:\\bad.png'],null,'wrong');await assert.rejects(task,/超时/);assert.equal(s.requests.length,0);assert.equal(s.uploads.length,0);assert.equal(s.app.state.uploading,false);assert.equal(s.listeners.size,0);
});
for(const paths of [[],[''],['C:\\a.png','C:\\b.png']])test('partial or invalid native path batch is rejected',async()=>{
 const s=setup(),task=s.app.importResourceDrop([{name:'a.png'}],'props');s.reply(paths);await assert.rejects(task,/不完整/);assert.equal(s.requests.length,0);assert.equal(s.app.state.uploading,false);
});
test('native errors report clearly without falling back or double importing',async()=>{
 const s=setup(),task=s.app.importResourceDrop([{name:'a.png'}],'props');s.reply(null,'不支持的拖拽对象');await assert.rejects(task,/拖拽对象/);assert.equal(s.uploads.length,0);assert.equal(s.requests.length,0);
});
test('bridge send failure permits side-effect-free upload fallback',async()=>{
 const s=setup();s.bridge.postMessageWithAdditionalObjects=()=>{throw new Error('unsupported')};await s.app.importResourceDrop([{name:'a.png'}],'props');assert.equal(s.uploads.length,1);assert.equal(s.requests.length,0);assert.equal(s.listeners.size,0);
});
test('ambiguous import response never retries through byte upload',async()=>{
 const s=setup();vm.runInContext('api=async()=>{throw new Error("lost response")}',s.c);const task=s.app.importResourceDrop([{name:'a.png'}],'props');s.reply(['C:\\a.png']);await assert.rejects(task,/lost response/);assert.equal(s.uploads.length,0);assert.equal(s.app.state.uploading,false);
});
test('migration and exit guards prevent even read-only native dispatch',async()=>{
 for(const flag of ['migrationBusy','exitBusy']){const s=setup();s.app.state[flag]=true;await assert.rejects(s.app.importResourceDrop([{name:'a.png'}],'props'));assert.equal(s.messages.length,0);}
});
