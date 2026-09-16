'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
function setup(){
  const dialogs=[],calls=[],jobs=[],guards=[],nodes=new Map(),buttons=[];
  const dialog={open:false,querySelectorAll:()=>buttons.slice(-2)};
  let respond=async()=>({job_id:'synthetic-import'});
  const context=vm.createContext({setTimeout,window:{},localStorage:{getItem:()=>null,setItem(){}},document:{querySelector:key=>nodes.get(key),querySelectorAll:()=>[]},
    FormData:class {constructor(form){this.form=form;}get(name){return this.form[name] ?? null;}},
    fixtureDialog:options=>{dialogs.push(options);dialog.open=true;context.app.state.modalSequence++;nodes.set('#importPaths',{value:''});nodes.set('#dialogError',{textContent:'',hidden:true});for(const kind of ['files','folder'])buttons.push({dataset:{pick:kind},disabled:false,addEventListener(name,fn){this[name]=fn;}});return dialog;},fixtureApi:async(url,options)=>{calls.push({url,options});return respond(url,options);},
    fixtureMonitor:(id,label)=>jobs.push({id,label}),fixtureGuard:selector=>guards.push(selector)});
  vm.runInContext(source+`\nshowDialog=fixtureDialog;api=fixtureApi;monitorJob=fixtureMonitor;requireFolderSelection=fixtureGuard;bindFolderSelector=()=>{};globalThis.app={state,importDialog};`,context);
  context.app.state.projectId='project-one';context.app.state.category='characters';context.app.state.folderId='folder-one';
  return {...context.app,dialogs,calls,jobs,guards,context,buttons,nodes,dialog,setApi(fn){respond=fn;}};
}
test('import defaults to physical copy and clearly offers explicit reference choice',()=>{
  const s=setup();s.importDialog();const html=s.dialogs[0].body;
  assert.match(html,/<select id="importMode" name="mode">/);assert.match(html,/<option value="copy" selected>复制到项目分类/);
  assert.match(html,/<option value="reference">仅引用原位置/);assert.match(html,/保留原文件/);assert.match(html,/ZIP 始终解压/);
});
for(const mode of [undefined,'copy','reference'])test(`import submits ${mode ?? 'default copy'} with captured project and chosen category/folder`,async()=>{
  const s=setup();s.importDialog();s.state.projectId='another-project';
  await s.dialogs[0].onSubmit({category:'characters',folder_id:'nested-folder',paths:' "D:/合成图片.png"\nD:/合成文件夹\n',mode});
  assert.equal(s.calls.length,1);assert.equal(s.calls[0].url,'/api/import');assert.equal(s.calls[0].options.method,'POST');
  const body=s.calls[0].options.body;assert.equal(body.mode,mode||'copy');assert.equal(body.project_id,'project-one');assert.equal(body.category,'characters');assert.equal(body.folder_id,'nested-folder');
  assert.deepEqual(Array.from(body.paths),['D:/合成图片.png','D:/合成文件夹']);assert.deepEqual(s.guards,['#importFolder']);assert.equal(s.jobs[0].id,'synthetic-import');
});
test('empty import selection never creates a copy job',async()=>{
  const s=setup();s.importDialog();await assert.rejects(s.dialogs[0].onSubmit({category:'scripts',paths:'\n  \n'}),/请选择文件/);assert.equal(s.calls.length,0);assert.equal(s.jobs.length,0);
});
for(const fail of [false,true])test(`late picker ${fail?'error':'selection'} cannot modify a replacement import dialog`,async()=>{
  const s=setup();s.importDialog();let resolve,reject;s.setApi(()=>new Promise((yes,no)=>{resolve=yes;reject=no;}));
  const pending=s.buttons[0].click();await s.buttons[0].click();assert.equal(s.calls.length,1);
  s.dialog.open=false;s.state.projectId='second-project';s.importDialog();const input=s.nodes.get('#importPaths');input.value='D:/新项目的文件.png';
  if(fail)reject(Error('旧选择窗口报错'));else resolve({paths:['D:/旧项目的文件.png']});await pending;
  assert.equal(input.value,'D:/新项目的文件.png');assert.equal(s.nodes.get('#dialogError').hidden,true);assert.equal(s.nodes.get('#dialogError').textContent,'');
});
test('current picker appends unique selections without duplicating paths',async()=>{
  const s=setup();s.importDialog();s.nodes.get('#importPaths').value='D:/原文件.png';s.setApi(async()=>({paths:['D:/原文件.png','D:/新文件.png']}));
  await s.buttons[1].click();assert.equal(s.nodes.get('#importPaths').value,'D:/原文件.png\nD:/新文件.png');assert.equal(s.buttons[1].disabled,false);
});
