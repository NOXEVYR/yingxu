
'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function fixture(){
 const calls=[],nodes=new Map(),document={documentElement:{dataset:{}},querySelector(selector){if(!nodes.has(selector))nodes.set(selector,{textContent:'',open:false});return nodes.get(selector);}};
 const context=vm.createContext({window:{},document,localStorage:{getItem:()=>null},setTimeout,clearTimeout,URLSearchParams,fetch:async(url,options)=>{calls.push({url,options});return{ok:true,json:async()=>({ok:true})};}});
 const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
 vm.runInContext(source+';globalThis.ui={state,api,migrationDraftProblem,collaborationHasPendingEdits,refreshContextTools};flushCanvas=()=>{};',context);
 context.ui.state.bootstrap={token:'synthetic-session-only'};
 return{...context.ui,calls,context};
}
test('task and MCP status reads send the workbench session token; ordinary assets retain old contract',async()=>{
 const f=fixture();for(const url of ['/api/ai-tasks?project_id=synthetic','/api/ai-tasks/abc/runs/def','/api/ai-tasks/abc/runs/def/calls/ghi/receipts','/api/ai-tasks/abc/runs/def/calls/ghi/receipts/jkl','/api/mcp/status','/api/ai-connections','/api/ai-connections/abc','/api/ai-calls/status'])await f.api(url);
 assert.ok(f.calls.every(c=>c.options.headers['X-YingXu-Token']==='synthetic-session-only'));
 await f.api('/api/items?project=synthetic');assert.equal(f.calls.at(-1).options.headers['X-YingXu-Token'],undefined);
});

test('late context refresh cannot overwrite a different project or repaint the assets page',async()=>{
 const f=fixture(),pending=[];f.context.pending=pending;
 vm.runInContext('api=()=>new Promise(resolve=>pending.push(resolve));renderContext=()=>{throw Error("Stale refresh must not remount any page");};toast=()=>{};',f.context);
 Object.assign(f.state,{projectId:'project-A',section:'context',context:{path:'A-before'},listSequence:2});
 const target={isConnected:true,disabled:false,closest:()=>null};const request=f.refreshContextTools(target);assert.equal(target.disabled,true);
 Object.assign(f.state,{projectId:'project-B',section:'assets',context:{path:'B-kept'},listSequence:3});
 pending.shift()({path:'A-late'});await request;assert.equal(f.state.context.path,'B-kept');assert.equal(target.disabled,false);
 Object.assign(f.state,{projectId:'project-A',section:'context',context:{path:'A-current'},listSequence:4});
 const departed=f.refreshContextTools(target);target.isConnected=false;pending.shift()({path:'detached-late'});await departed;
 assert.equal(f.state.context.path,'A-current');
});
test('collaboration writes block project migration and freezing another round until accepted work drains',()=>{
 const f=fixture();f.state.aiCollaborationBusy=true;assert.match(f.migrationDraftProblem(),/等待/);assert.equal(f.collaborationHasPendingEdits(),true);
 f.state.aiCollaborationBusy=false;assert.equal(f.migrationDraftProblem(),'');assert.equal(f.collaborationHasPendingEdits(),false);
 const tab={dirty:true,source:'file',draft:'保留正文'};f.state.tabs=[tab];vm.runInContext('markdownInputReady=()=>true;',f.context);assert.equal(f.collaborationHasPendingEdits(),true);assert.equal(tab.draft,'保留正文');
});

test('tool-call writes share editor and migration protection but remote-pending alone is not a lock',()=>{
 const f=fixture();f.state.aiToolCallBusy=true;assert.match(f.migrationDraftProblem(),/等待/);assert.equal(f.collaborationHasPendingEdits(),true);
 f.state.aiToolCallBusy=false;f.state.aiToolRemotePending=3;assert.equal(f.migrationDraftProblem(),'');assert.equal(f.collaborationHasPendingEdits(),false);
});

test('incomplete successful write response is unknown submission, never a false success object',async()=>{
 const f=fixture();vm.runInContext("fetch=async()=>({ok:true,status:201,json:async()=>{throw Error('cut response');}});",f.context);
 await assert.rejects(f.api('/api/ai-tasks',{method:'POST',body:{title:'测试'}}),error=>error.unknownResult===true&&error.status===201);
 await assert.rejects(f.api('/api/ai-tasks?project_id=synthetic'),error=>error.unknownResult===false);
});
