'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
function setup(){
 const nodes=new Map(),dialogs=[],calls=[],folders=[],closed=[],notices=[];let allow=true,respond=async()=>({items:[],stats:{moved:1},warnings:[]}),folderResponse=async()=>[];
 const node=(value='')=>({value,dataset:{},disabled:false,addEventListener(type,fn){this[type]=fn;}});
 nodes.set('#appDialog',{open:false});nodes.set('#dialogError',{});
 const c=vm.createContext({window:{},document:{querySelector:k=>nodes.get(k)},localStorage:{getItem:()=>null},console,setTimeout,clearTimeout,FormData:class{constructor(form){this.form=form;}get(k){return this.form[k]??null;}},
 show:opts=>{dialogs.push(opts);c.app.state.modalSequence++;nodes.get('#appDialog').open=true;nodes.set('#moveProject',node('b'));nodes.set('#moveCategory',node('characters'));nodes.set('#moveFolder',node());return nodes.get('#appDialog');},
 guard:async()=>allow,apiMock:async(url,options)=>{calls.push({url,options});return respond(url,options);},folderMock:async(...args)=>{folders.push(args);return folderResponse(...args);},close:tabs=>closed.push(tabs),notice:m=>notices.push(m),nodes});
 vm.runInContext(source+`\nshowDialog=show;prepareTabs=guard;api=apiMock;folderChoices=folderMock;refreshProjects=async()=>{};loadItems=async()=>{};renderTabs=()=>{};renderInspector=()=>{};removeOpenTabs=close;toast=notice;globalThis.app={state,moveDialog,performMove};`,c);
 Object.assign(c.app.state,{projectId:'a',section:'assets',items:[{id:'item',project_id:'a',category:'characters'}],projects:[{id:'a',name:'源项目'},{id:'b',name:'目标项目'}],tabs:[{source:'file',id:'item',key:'tab',item:{project_id:'a'}}]});
 return {c,app:c.app,nodes,dialogs,calls,folders,closed,notices,setAllow:v=>allow=v,setApi:fn=>respond=fn,setFolders:fn=>folderResponse=fn};
}
test('drag destination opens a project/category/folder chooser; only confirmation moves',async()=>{
 const s=setup();await s.app.moveDialog(['item'],'b');assert.match(s.dialogs[0].body,/目标项目/);assert.equal(s.calls.length,0);assert.deepEqual(Array.from(s.folders[0]),['b','characters']);
 await s.dialogs[0].onSubmit({target_project_id:'b',category:'scenes',folder_id:'nested'});
 assert.deepEqual(JSON.parse(JSON.stringify(s.calls[0].options.body)),{ids:['item'],category:'scenes',folder_id:'nested',target_project_id:'b'});assert.equal(s.closed.length,1);assert.equal(s.app.state.moveBusy,false);
});
test('cancelled unsaved draft protection makes no move request or chooser',async()=>{
 const s=setup();s.setAllow(false);await s.app.moveDialog(['item'],'b');assert.equal(s.dialogs.length,0);assert.equal(await s.app.performMove(['item'],'props',null,'b'),false);assert.equal(s.calls.length,0);assert.equal(s.closed.length,0);
});
test('backend refusal retains open tabs and selections',async()=>{
 const s=setup();s.app.state.selectedIds.add('item');s.setApi(async()=>{throw new Error('请一并选择关联文件')});await assert.rejects(s.app.performMove(['item'],'props',null,'b'),/关联/);assert.equal(s.closed.length,0);assert.equal(s.app.state.selectedIds.has('item'),true);assert.equal(s.app.state.moveBusy,false);
});
test('same-project move retains existing route contract and open tabs',async()=>{
 const s=setup();await s.app.performMove(['item'],'props',null);assert.equal('target_project_id' in s.calls[0].options.body,false);assert.equal(s.closed.length,0);
});
test('folder enumeration failure keeps submit blocked instead of choosing wrong root',async()=>{
 const s=setup();s.setFolders(async()=>{throw new Error('文件夹读取失败')});await s.app.moveDialog(['item'],'b');assert.equal(s.nodes.get('#moveFolder').disabled,true);await assert.rejects(s.dialogs[0].onSubmit({target_project_id:'b',category:'props'}));assert.equal(s.calls.length,0);
});
test('migration prevents cross-project move requests',async()=>{
 const s=setup();s.app.state.migrationBusy=true;await assert.rejects(s.app.performMove(['item'],'props',null,'b'));assert.equal(s.calls.length,0);
});
test('move completion exposes cleanup warnings without repeating writes',async()=>{
 const s=setup();s.setApi(async()=>({items:[],stats:{moved:1},warnings:['源副本保留，稍后处理']}));await s.app.performMove(['item'],'props',null,'b');assert.equal(s.calls.length,1);assert.match(s.notices[1],/源副本保留/);
});



test('overlapping move calls cannot both pass async draft preparation',async()=>{
 const s=setup();let finish;s.c.waitPrepare=new Promise(resolve=>finish=()=>resolve(true));vm.runInContext('prepareTabs=()=>waitPrepare',s.c);
 const first=s.app.performMove(['item'],'props',null,'b');await assert.rejects(s.app.performMove(['item'],'props',null,'b'),/尚未完成/);assert.equal(s.calls.length,0);finish();await first;assert.equal(s.calls.length,1);
});
test('post-commit refresh failure closes the completed operation without another write',async()=>{
 const s=setup();vm.runInContext('refreshProjects=async()=>{throw new Error("lost read")}',s.c);assert.equal(await s.app.performMove(['item'],'props',null,'b'),true);assert.equal(s.calls.length,1);assert.ok(s.notices.some(m=>m.includes('无需重复移动')));
});
test('late input while a move completes is retained instead of closing the draft',async()=>{
 const s=setup();s.setApi(async()=>{s.app.state.tabs[0].dirty=true;return {items:[{id:'item',project_id:'b'}],content_changed:['item'],stats:{moved:1}}});
 await s.app.performMove(['item'],'props',null,'b');assert.equal(s.closed[0].length,0);assert.equal(s.app.state.tabs[0].dirty,true);assert.equal(s.app.state.tabs[0].conflict,true);assert.equal(s.app.state.tabs[0].item.project_id,'b');
});
