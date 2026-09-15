'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
function setup(read) {
  const nodes=new Map(),listeners=new Map(),uploads=[],requests=[],errors=[];
  const node=key=>{if(!nodes.has(key))nodes.set(key,{open:false,hidden:true,style:{setProperty(){}},dataset:{},classList:{add(){},remove(){},toggle(){}},value:'',addEventListener(){},insertAdjacentHTML(){},querySelectorAll:()=>[],closest:()=>null});return nodes.get(key);};
  const document={activeElement:node('blank'),querySelector:node,querySelectorAll:()=>[],addEventListener:(key,fn)=>listeners.set(key,fn),body:node('body')};
  const context=vm.createContext({document,window:{addEventListener(){}},navigator:{clipboard:{read}},File,Blob,console,setTimeout,clearTimeout,URLSearchParams,AbortController,localStorage:{getItem:()=>null},uploads,requests,errors});
  const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
  vm.runInContext(source+`\nrefreshProjects=async()=>{};loadItems=async()=>{};toast=()=>{};report=e=>errors.push(e.message);uploadFiles=async(...args)=>{uploads.push(args);};api=async(...args)=>{requests.push(args);return {items:[{}]};};globalThis.app={state,pasteResourceFiles,wireEvents};`,context);
  Object.assign(context.app.state,{projectId:'original',category:'characters',folderId:'nested',section:'assets'});
  context.app.wireEvents();
  return {...context.app,context,nodes,listeners,uploads,requests,errors,document};
}
test('Ctrl+V event uploads its image file once without calling native clipboard or reading it again',async()=>{
  const s=setup(()=>{throw Error('unexpected clipboard read');});const file=new File(['png'],'image.png',{type:'image/png'});
  const event={target:s.document.activeElement,clipboardData:{files:[file],items:[{kind:'file',getAsFile:()=>file}]},preventDefault(){this.prevented=true;}};
  s.listeners.get('paste')(event);await new Promise(setImmediate);
  assert.equal(event.prevented,true);assert.equal(s.uploads.length,1);assert.equal(s.uploads[0][0][0],file);
  assert.deepEqual(Array.from(s.uploads[0]).slice(1),['characters','nested','original']);assert.equal(s.requests.length,0);
});
test('clipboard items provide a File even when clipboardData.files is empty',async()=>{
  const s=setup();const image=new File(['fixture'],'image.png',{type:'image/png'});
  await s.pasteResourceFiles(null,{files:[],items:[{kind:'string',getAsFile:()=>{throw Error('text must stay unread');}},{kind:'file',getAsFile:()=>image}]});
  assert.equal(s.uploads[0][0][0],image);assert.equal(s.requests.length,0);
});
test('right-click reads a PNG blob and keeps the clicked destination while navigation changes',async()=>{
  let resolve;const pending=new Promise(done=>resolve=done);const s=setup(()=>pending);
  const target={project_id:'clicked',category:'scenes',folder_id:'scene-folder'};
  const paste=s.pasteResourceFiles(target);assert.equal(s.state.uploading,true);
  target.project_id='mutated';Object.assign(s.state,{projectId:'other',category:'props',folderId:'other-folder'});
  await s.pasteResourceFiles(); // another paste while permission/read is pending must not duplicate it
  const bytes=new Uint8Array([137,80,78,71]);resolve([{types:['text/html','image/png'],getType:async type=>{assert.equal(type,'image/png');return new Blob([bytes],{type});}}]);await paste;
  assert.equal(s.uploads.length,1);assert.deepEqual(Array.from(s.uploads[0]).slice(1),['scenes','scene-folder','clicked']);
  assert.match(s.uploads[0][0][0].name,/\.png$/);assert.deepEqual(new Uint8Array(await s.uploads[0][0][0].arrayBuffer()),bytes);assert.equal(s.state.uploading,false);
});
test('browser permission rejection falls back to native image/file reader at captured target',async()=>{
  const s=setup(async()=>{throw new Error('NotAllowedError');});
  await s.pasteResourceFiles({project_id:'p',category:'all',folder_id:null});
  assert.equal(s.uploads.length,0);assert.equal(s.requests.length,1);
  assert.equal(s.requests[0][0],'/api/clipboard/paste');
  assert.deepEqual(JSON.parse(JSON.stringify(s.requests[0][1].body)),{project_id:'p',category:'unclassified',folder_id:null});assert.equal(s.errors.length,0);
});
test('text and copied URLs are never read or fetched as images',async()=>{
  const s=setup(async()=>[{types:['text/plain','text/html'],getType:()=>{throw Error('must not read strings');}}]);
  await s.pasteResourceFiles();assert.equal(s.requests.length,1);assert.equal(s.uploads.length,0);
});
test('editor text, composing focus and dialogs retain their own paste behavior',async()=>{
  const s=setup(async()=>{throw Error('unexpected read');});
  for(const selector of ['textarea','#editorContent','[contenteditable]']) {
    const target={closest:value=>value.includes(selector)?{}:null};s.document.activeElement=target;
    const event={target,preventDefault(){this.prevented=true;}};s.listeners.get('paste')(event);assert.equal(event.prevented,undefined);
  }
  s.document.activeElement=s.nodes.get('blank');s.nodes.get('#appDialog').open=true;await s.pasteResourceFiles();
  assert.equal(s.requests.length,0);assert.equal(s.uploads.length,0);
});
test('native clipboard failure reports once and releases the busy state',async()=>{
  const s=setup();vm.runInContext('api=async()=>{throw new Error("没有可粘贴的图片或文件");};',s.context);
  await s.pasteResourceFiles();assert.deepEqual(s.errors,['没有可粘贴的图片或文件']);assert.equal(s.state.uploading,false);
});
