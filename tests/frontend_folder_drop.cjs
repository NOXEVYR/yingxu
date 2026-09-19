'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
function setup({failFolder=false,failUpload=false}={}){
 const nodes=new Map(),writes=[],uploads=[],errors=[],notices=[];
 const node=k=>{if(!nodes.has(k))nodes.set(k,{});return nodes.get(k);};
 class XHR{
  constructor(){this.upload={};}open(method,url){this.url=url;}setRequestHeader(){}
  send(file){uploads.push({file,params:Object.fromEntries(new URL(this.url,'http://localhost').searchParams)});this.status=failUpload?400:201;this.responseText=JSON.stringify(failUpload?{error:'合成读取错误'}:{id:'item'});queueMicrotask(()=>this.onload());}
 }
 const c=vm.createContext({window:{},document:{querySelector:node},localStorage:{getItem:()=>null},console,setTimeout,clearTimeout,URLSearchParams,XMLHttpRequest:XHR,writes,errors,notices,failFolder});
 vm.runInContext(source+`\napi=async(url,o)=>{writes.push({url,body:o.body});if(failFolder)throw new Error('同名文件夹');return {id:'folder-'+writes.length};};refreshProjects=async()=>{};loadItems=async()=>{};report=e=>errors.push(e.message);toast=m=>notices.push(m);globalThis.app={state,browserFolderPlan,droppedEntries,importResourceDrop};`,c);
 Object.assign(c.app.state,{projectId:'original',bootstrap:{token:'test'},section:'assets'});
 return {app:c.app,writes,uploads,errors,notices,node};
}
const file=(name,size=5)=>({name,isFile:true,file:ok=>ok({name,size})});
function folder(name,...batches){return {name,isDirectory:true,createReader(){let i=0;return {readEntries:ok=>ok(batches[i++]||[])};}};}
test('browser folder drop preserves nested and empty folders across readEntries batches',async()=>{
 const s=setup(),entry=folder('素材',[file('一.png'),folder('子目录',[file('二.md')])],[folder('空目录'),file('安装.exe')]);
 await s.app.importResourceDrop([{name:'素材'}],'characters','selected',[entry]);
 assert.deepEqual(s.writes.map(x=>[x.body.name,x.body.parent_id,x.body.project_id,x.body.category]),[
  ['素材','selected','original','characters'],['子目录','folder-1','original','characters'],['空目录','folder-1','original','characters']]);
 assert.deepEqual(s.uploads.map(x=>[x.file.name,x.params.folder_id]),[['一.png','folder-1'],['二.md','folder-2']]);
 assert.equal(s.errors.length,0);assert.match(s.notices[0],/跳过 1/);assert.equal(s.app.state.uploading,false);
});
test('destination and busy guard captured before asynchronous browser enumeration',async()=>{
 const s=setup();let resume;
 const entry={name:'收集',isDirectory:true,createReader(){let read=false;return {readEntries(ok){if(read)ok([]);else {read=true;resume=()=>ok([file('图.png')]);}}};}};
 const pending=s.app.importResourceDrop([{name:'收集'}],'props','selected',[entry]);
 assert.equal(s.app.state.uploading,true);s.app.state.projectId='changed';
 await s.app.importResourceDrop([{name:'another.png'}],'scenes');assert.equal(s.uploads.length,0);
 resume();await pending;assert.equal(s.writes[0].body.project_id,'original');assert.equal(s.uploads[0].params.project,'original');
});
test('unreadable, incomplete, too deep and oversized trees fail before any write',async()=>{
 let deep=file('a.png');for(let i=0;i<20;i++)deep=folder('层',[deep]);
 for(const entries of [[folder('root',[{name:'bad.png',isFile:true,file:(ok,fail)=>fail(new Error('读取失败'))}])],[folder('root'),null],[deep],[folder('large',[file('large.png',17*1024**3)])]]){
  const s=setup();await assert.rejects(s.app.importResourceDrop([{name:'root'}],'props',null,entries));assert.equal(s.writes.length,0);assert.equal(s.uploads.length,0);assert.equal(s.app.state.uploading,false);
 }
});
test('directory conflict and file failure stop without retries or deleting partial copies',async()=>{
 for(const flags of [{failFolder:true},{failUpload:true}]){
  const s=setup(flags);await s.app.importResourceDrop([{name:'root'}],'props',null,[folder('root',[file('a.png'),file('b.png')])]);
  assert.equal(s.writes.length,1);assert.equal(s.uploads.length,flags.failFolder?0:1);assert.equal(s.errors.length,1);assert.match(s.errors[0],/已完成的副本保留/);assert.equal(s.notices.length,0);
 }
});
test('migration guard applies to browser directories too',async()=>{
 const s=setup();s.app.state.migrationBusy=true;await assert.rejects(s.app.importResourceDrop([{name:'root'}],'props',null,[folder('root')]));assert.equal(s.writes.length,0);
});
test('drop entries are captured synchronously and never inferred from file extensions',()=>{
 const s=setup(),entry=folder('目录.png');let called=0;
 const result=s.app.droppedEntries({items:[{kind:'string'},{kind:'file',webkitGetAsEntry(){called++;return entry;}}]});
 assert.equal(called,1);assert.equal(result[0],entry);
});
