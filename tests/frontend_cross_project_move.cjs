'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
function setup(){
 const nodes=new Map(),dialogs=[],calls=[],folders=[],closed=[],notices=[],conflictPrompts=[];let allow=true,respond=async()=>({items:[],stats:{moved:1},warnings:[]}),folderResponse=async()=>[],conflictChoice=async()=>null;
 const node=(value='')=>({value,dataset:{},disabled:false,addEventListener(type,fn){this[type]=fn;}});
 nodes.set('#appDialog',{open:false});nodes.set('#dialogError',{});
 const c=vm.createContext({window:{},document:{querySelector:k=>nodes.get(k)},localStorage:{getItem:()=>null},console,setTimeout,clearTimeout,FormData:class{constructor(form){this.form=form;}get(k){return this.form[k]??null;}},
 show:opts=>{dialogs.push(opts);c.app.state.modalSequence++;nodes.get('#appDialog').open=true;nodes.set('#moveProject',node('b'));nodes.set('#moveCategory',node('characters'));nodes.set('#moveFolder',node());return nodes.get('#appDialog');},
 guard:async()=>allow,apiMock:async(url,options)=>{calls.push({url,options});return respond(url,options);},folderMock:async(...args)=>{folders.push(args);return folderResponse(...args);},close:tabs=>closed.push(tabs),notice:m=>notices.push(m),conflictPrompt:async values=>{conflictPrompts.push(values);return conflictChoice(values);},nodes});
 vm.runInContext(source+`\nconst originalApi=api,originalChooseMoveConflict=chooseMoveConflict;showDialog=show;prepareTabs=guard;api=apiMock;chooseMoveConflict=conflictPrompt;folderChoices=folderMock;refreshProjects=async()=>{};loadItems=async()=>{};renderTabs=()=>{};renderInspector=()=>{};updateSelection=()=>{};removeOpenTabs=close;toast=notice;globalThis.app={state,moveDialog,performMove,originalApi,originalChooseMoveConflict};`,c);
 Object.assign(c.app.state,{projectId:'a',section:'assets',items:[{id:'item',project_id:'a',category:'characters'}],projects:[{id:'a',name:'源项目'},{id:'b',name:'目标项目'}],tabs:[{source:'file',id:'item',key:'tab',item:{project_id:'a'}}]});
 return {c,app:c.app,nodes,dialogs,calls,folders,closed,notices,conflictPrompts,setChoice:fn=>conflictChoice=fn,setAllow:v=>allow=v,setApi:fn=>respond=fn,setFolders:fn=>folderResponse=fn};
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

const nameConflict=()=>Object.assign(new Error('同名文件'),{status:409,code:'move_name_conflict',conflicts:[{id:'item',name:'图片.png',source:'项目/文本/图片.png',target:'项目/角色/图片.png'}]});
test('API preserves the known precommit conflict code and conflict details',async()=>{
 const s=setup(),error=nameConflict();s.c.fetch=async()=>({ok:false,status:409,json:async()=>({error:error.message,code:error.code,conflicts:error.conflicts})});
 await assert.rejects(s.app.originalApi('/api/move',{method:'POST',body:{ids:['item']}}),e=>e.status===409&&e.code==='move_name_conflict'&&e.conflicts[0].id==='item');
});
test('explicit rename or skip retries the fixed selection and destination exactly once',async()=>{
 for(const choice of ['rename','skip']){
  const s=setup(),ids=['item'];let prepares=0;s.c.reprepare=async()=>{prepares++;return true;};vm.runInContext('prepareTabs=reprepare',s.c);
  s.setApi(async()=>{if(s.calls.length===1)throw nameConflict();return {items:[],stats:{moved:choice==='rename'?1:0,skipped:choice==='skip'?1:0}};});
  s.setChoice(async()=>{assert.equal(s.app.state.moveBusy,true);ids.push('late');s.app.state.projectId='b';s.app.state.category='delivery';s.app.state.folderId='wrong';return choice;});
  assert.equal(await s.app.performMove(ids,'characters','nested'),true);assert.equal(prepares,2);assert.equal(s.calls.length,2);
  assert.deepEqual(JSON.parse(JSON.stringify(s.calls[1].options.body)),{ids:['item'],category:'characters',folder_id:'nested',conflict:choice});
  if(choice==='skip')assert.ok(s.notices.some(value=>value.includes('1 个同名文件已跳过')));
 }
});
test('cancel and Escape retain selection without retrying the move',async()=>{
 for(const choice of ['cancel',null]){const s=setup();s.app.state.selectedIds.add('item');s.setApi(async()=>{throw nameConflict();});s.setChoice(async()=>choice);
  assert.equal(await s.app.performMove(['item'],'characters',null),false);assert.equal(s.calls.length,1);assert.equal(s.app.state.selectedIds.has('item'),true);assert.equal(s.app.state.moveBusy,false);
 }
});
test('generic failures and cross-project conflicts never offer or retry conflict resolution',async()=>{
 for(const [error,target] of [[new Error('断线'),null],[Object.assign(new Error('其它冲突'),{status:409}),null],[nameConflict(),'b']]){
  const s=setup();s.setApi(async()=>{throw error;});await assert.rejects(s.app.performMove(['item'],'characters',null,target));assert.equal(s.calls.length,1);assert.equal(s.conflictPrompts.length,0);
 }
});
test('retry rechecks draft refusal and migration state after conflict confirmation',async()=>{
 for(const block of ['draft','before','after']){
  const s=setup();s.setApi(async()=>{throw nameConflict();});s.setChoice(async()=>{if(block==='draft')s.setAllow(false);if(block==='before')s.app.state.migrationBusy=true;if(block==='after'){s.c.blockDuringPrepare=async()=>{s.app.state.migrationBusy=true;return true;};vm.runInContext('prepareTabs=blockDuringPrepare',s.c);}return 'rename';});
  if(block==='draft')assert.equal(await s.app.performMove(['item'],'characters',null),false);else await assert.rejects(s.app.performMove(['item'],'characters',null),/尚未完成/);
  assert.equal(s.calls.length,1);assert.equal(s.app.state.moveBusy,false);
 }
});
test('failed explicit retry never prompts or retries a second time',async()=>{
 const s=setup();s.setApi(async()=>{throw nameConflict();});s.setChoice(async()=>'rename');await assert.rejects(s.app.performMove(['item'],'characters',null));assert.equal(s.calls.length,2);assert.equal(s.conflictPrompts.length,1);
});

test('all skipped retains visible skipped selection and never claims files moved',async()=>{
 const s=setup();s.setApi(async()=>{if(s.calls.length===1)throw nameConflict();return {items:[],skipped_ids:['item'],stats:{moved:0,skipped:1}};});s.setChoice(async()=>'skip');
 await s.app.performMove(['item'],'characters',null);assert.equal(s.app.state.selectedIds.has('item'),true);assert.ok(s.notices.some(value=>value.startsWith('未移动文件')&&value.includes('1 个同名文件已跳过')));
});

test('conflict modal defaults to skip, escapes paths, and preserves the existing move form',async()=>{
 for(const choice of ['skip','rename',null]){
  const s=setup(),handlers={},buttons=['cancel','rename','skip'].map(value=>({dataset:{moveChoice:value},addEventListener(type,fn){this[type]=fn;}}));
  const popup={setAttribute(){},querySelectorAll:()=>buttons,addEventListener(type,fn){handlers[type]=fn;},showModal(){this.open=true;},remove(){this.removed=true;},close(){this.open=false;handlers.close();}};
  s.c.document.createElement=()=>popup;s.c.document.body={append:node=>assert.equal(node,popup)};s.nodes.get('#appDialog').open=true;s.app.state.modalBusy=true;
  const pending=s.app.originalChooseMoveConflict([{name:'<x>.png',source:'<source>',target:'target'}]);
  assert.match(popup.innerHTML,/button-primary[^>]+data-move-choice="skip" autofocus/);assert.match(popup.innerHTML,/&lt;x&gt;/);assert.doesNotMatch(popup.innerHTML,/<source>/);
  assert.equal(s.nodes.get('#appDialog').open,true);assert.equal(s.app.state.modalBusy,true);
  let stopped=false;handlers.keydown({stopPropagation(){stopped=true;}});assert.equal(stopped,true);
  if(choice)buttons.find(button=>button.dataset.moveChoice===choice).click();else popup.close();
  assert.equal(await pending,choice);assert.equal(popup.removed,true);
 }
});
