'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function setup(){
 const nodes=new Map(),events={},calls=[],notices=[],classes=new Set();
 const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{classList:{add(){},remove(){},toggle(){}},addEventListener(){},value:'',open:false});return nodes.get(selector)};
 const document={querySelector:node,querySelectorAll:()=>[],body:{classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),toggle:(x,on)=>on?classes.add(x):classes.delete(x)}},addEventListener:(type,fn,capture)=>{(events[type]||=[]).push({fn,capture})}};
 const c=vm.createContext({window:{},document,localStorage:{getItem:()=>null,setItem(){}},console,setTimeout,clearTimeout,notices,calls});
 const source=fs.readFileSync(require('node:path').join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
 vm.runInContext(source+`\napi=async(url,opt)=>{calls.push([url,opt]);return {job_id:'job'};};monitorJob=async()=>({project_id:'new'});guardProperties=async()=>true;selectProject=async id=>calls.push(['select',id]);renderWorkspace=()=>{};configureSection=()=>{};toast=m=>notices.push(m);globalThis.app={state,wireDragAndDrop,importProjectFolders};`,c);
 Object.assign(c.app.state,{projectId:'old',projectLibraryFolder:'category',projectLibrary:{folders:[{id:'category'}],projects:[{id:'new',folder_id:'category'}]}});
 return{c,app:c.app,calls,notices,events,classes};
}
test('folder import copies via library API using captured category and opens new project',async()=>{
 const s=setup();await s.app.importProjectFolders(['C:\\source']);
 assert.equal(s.calls[0][0],'/api/project-library/open-folder');assert.deepEqual(JSON.parse(JSON.stringify(s.calls[0][1].body)),{path:'C:\\source',folder_id:'category'});
 assert.deepEqual(Array.from(s.calls[1]),['select','new']);assert.equal(s.app.state.category,'all');assert.equal(s.app.state.uploading,false);
});
test('busy held before path resolution and same drop cannot enqueue twice',async()=>{
 const s=setup();let resolve;
 s.c.resolvePath=new Promise(r=>resolve=r);vm.runInContext('resolveDroppedPaths=()=>resolvePath',s.c);
 const task=s.app.importProjectFolders(null,[{name:'folder'}]);await Promise.resolve();
 assert.equal(s.app.state.uploading,true);await s.app.importProjectFolders(['C:\\other']);assert.equal(s.calls.length,0);
 s.app.state.projectLibraryFolder='other';resolve(['C:\\source']);await task;
 assert.equal(s.calls[0][1].body.folder_id,'category');
});
test('unsupported browser directory uses picker, never byte uploads into current project',async()=>{
 const s=setup();vm.runInContext('resolveDroppedPaths=async()=>null;projectFolderImportDialog=()=>calls.push(["picker"]);uploadFiles=()=>{throw new Error("wrong route")}',s.c);
 await s.app.importProjectFolders(null,[{name:'directory'}]);assert.equal(s.calls[0][0],'picker');assert.equal(s.calls.length,1);
});
test('failed job or ambiguous response never automatically repeats copying',async()=>{
 const s=setup();vm.runInContext('monitorJob=async()=>{throw new Error("copy failed")}',s.c);
 await assert.rejects(s.app.importProjectFolders(['C:\\one','C:\\two']),/copy failed/);
 assert.equal(s.calls.length,1);assert.equal(s.app.state.uploading,false);
});
test('capture-phase library drop is exclusive and has its own copy hint',async()=>{
 const s=setup();vm.runInContext('importProjectFolders=async(paths,files)=>calls.push(["library",files])',s.c);s.app.wireDragAndDrop();
 const target={closest:q=>q==='#projectLibraryButton'?{}:null};let stopped=false,prevented=false;
 const event={target,dataTransfer:{types:['Files'],files:[{name:'folder'}]},preventDefault(){prevented=true},stopImmediatePropagation(){stopped=true},stopPropagation(){}};
 for(const listener of s.events.dragover.filter(e=>e.capture)){listener.fn(event);if(stopped)break;}assert.equal(s.classes.has('project-library-file-drag'),true);
 stopped=false;for(const listener of s.events.drop.filter(e=>e.capture)){listener.fn(event);if(stopped)break;}assert.ok(stopped&&prevented);assert.equal(s.calls[0][0],'library');assert.equal(s.classes.has('project-library-file-drag'),false);
});
test('internal resource drags are not intercepted as project folders',()=>{
 const s=setup();s.app.wireDragAndDrop();let prevented=false;
 for(const listener of s.events.drop.filter(e=>e.capture))listener.fn({target:{closest:q=>q==='#projectLibraryButton'?{}:null},dataTransfer:{types:['Files','application/x-yingxu-item'],files:[{}]},preventDefault(){prevented=true}});
 assert.equal(prevented,false);assert.equal(s.calls.length,0);
});
