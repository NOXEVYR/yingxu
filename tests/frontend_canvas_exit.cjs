'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
function setup(choices=['discard']){
 const messages=[],destroyed=[],choicesSeen=[],renders=[];
 const context=vm.createContext({console,setTimeout,clearTimeout,localStorage:{getItem:()=>null,setItem(){}},window:{chrome:{webview:{postMessage:m=>messages.push(m)}}},document:{querySelector:()=>({open:false}),querySelectorAll:()=>[]},choices,choicesSeen,renders});
 const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
 vm.runInContext(source+`\nchoose=async()=>{choicesSeen.push(true);return choices.shift();};toast=()=>{};persistDrafts=()=>{};renderTabs=()=>{};renderEditorStatus=()=>{};renderInspector=()=>{};renderEditorBody=()=>renders.push('body');renderWorkspace=()=>renders.push('workspace');markDirty=tab=>{tab.dirty=true;};saveTab=async tab=>{tab.content.content=tab.draft;tab.dirty=false;return true;};globalThis.app={state,canvasTabs,handleDesktopMessage,flushCanvas};`,context);
 const app=context.app;
 function canvas(id='c',dirty=true){let value='changed',composing=false;const tab={id,key:'file:'+id,source:'file',item:{name:id,kind:'excalidraw'},content:{content:'saved'},draft:dirty?'changed':'saved',dirty,canvasHost:{remove(){}},canvasEditor:{getValue:()=>value,isComposing:()=>composing,destroy:()=>destroyed.push(id)}};app.state.tabs.push(tab);app.canvasTabs.add(tab);app.state.activeKey=tab.key;return {tab,setValue:v=>value=v,setComposing:v=>composing=v};}
  return {...app,context,messages,destroyed,choicesSeen,renders,canvas};
}
test('discarding an open canvas exits without resurrecting its unsaved iframe state',async()=>{
 const s=setup(),{tab}=s.canvas();s.state.drafts[tab.key]={content:'changed'};
 await s.handleDesktopMessage({action:'prepare-exit',requestId:'exit'});
 assert.equal(s.messages.at(-1).allow,true);assert.deepEqual(s.destroyed,['c']);assert.equal(tab.draft,'saved');assert.equal(tab.dirty,false);assert.equal(s.canvasTabs.size,0);assert.equal(s.state.drafts[tab.key],undefined);assert.deepEqual(s.renders,[]);
 s.flushCanvas(tab);assert.equal(tab.dirty,false);
});
test('cancel keeps the canvas instance and unsaved drawing',async()=>{
 const s=setup(['cancel']),{tab}=s.canvas();const editor=tab.canvasEditor;
 await s.handleDesktopMessage({action:'prepare-exit',requestId:'cancel'});
 assert.equal(s.messages.at(-1).allow,false);assert.equal(tab.canvasEditor,editor);assert.equal(tab.draft,'changed');assert.deepEqual(s.destroyed,[]);
});
test('a later cancelled tab restores the discarded active canvas for continued use',async()=>{
 const s=setup(['discard','cancel']),{tab}=s.canvas();s.state.tabs.push({key:'file:text',item:{kind:'text',name:'text'},draft:'draft',content:{content:'original'},dirty:true});s.state.activeKey=tab.key;
 await s.handleDesktopMessage({action:'prepare-exit',requestId:'multi'});
 assert.equal(s.messages.at(-1).allow,false);assert.deepEqual(s.renders,['workspace']);assert.equal(s.state.tabs[1].draft,'draft');assert.equal(s.state.tabs[1].dirty,true);
});
test('save failure and composition block exit without destroying drawings',async()=>{
 const s=setup(['save']),canvas=s.canvas();vm.runInContext('saveTab=async()=>false;',s.context);
 await s.handleDesktopMessage({action:'prepare-exit',requestId:'save'});assert.equal(s.messages.at(-1).allow,false);assert.deepEqual(s.destroyed,[]);
 canvas.setComposing(true);await s.handleDesktopMessage({action:'prepare-exit',requestId:'ime'});assert.equal(s.messages.at(-1).allow,false);assert.equal(s.choicesSeen.length,1);assert.deepEqual(s.destroyed,[]);
});
test('latest iframe edits are flushed before deciding whether confirmation is needed',async()=>{
 const s=setup(['cancel']),{tab}=s.canvas('c',false);
 await s.handleDesktopMessage({action:'prepare-exit',requestId:'flush'});
 assert.equal(s.choicesSeen.length,1);assert.equal(tab.draft,'changed');assert.equal(s.messages.at(-1).allow,false);
});
test('saving an open canvas permits exit without discarding its instance',async()=>{
 const s=setup(['save']);s.canvas();await s.handleDesktopMessage({action:'prepare-exit',requestId:'save'});assert.equal(s.messages.at(-1).allow,true);assert.deepEqual(s.destroyed,[]);
});
test('a saving later tab prevents any earlier discard confirmation',async()=>{
 const s=setup();s.canvas();s.state.tabs.push({key:'file:busy',item:{kind:'text',name:'busy'},saving:true});
 await s.handleDesktopMessage({action:'prepare-exit',requestId:'busy'});assert.equal(s.messages.at(-1).allow,false);assert.equal(s.choicesSeen.length,0);assert.deepEqual(s.destroyed,[]);
});
test('cancelled move preparation also restores the explicitly discarded active canvas',async()=>{
 const s=setup(['discard','cancel']),{tab}=s.canvas();s.state.tabs.push({key:'file:text',item:{kind:'text',name:'text'},content:{content:'saved'},draft:'changed',dirty:true});s.state.activeKey=tab.key;
 const result=await vm.runInContext('prepareTabs([...state.tabs])',s.context);
 assert.equal(result,false);assert.deepEqual(s.renders,['workspace']);assert.equal(tab.draft,'saved');assert.equal(s.state.tabs[1].dirty,true);
});
