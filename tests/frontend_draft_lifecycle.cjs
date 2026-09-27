'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8').replace(/boot\(\);\s*$/, '');
const externalA = 'a'.repeat(32), externalB = 'b'.repeat(32);
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function setup(drafts = {}) {
  const store = new Map([['yingxu:drafts', JSON.stringify(drafts)]]), requests = [], notices = [], renders = [];
  const context = vm.createContext({
    console, setTimeout, clearTimeout,
    localStorage: {getItem:key=>store.get(key) || null, setItem:(key,value)=>store.set(key,value)},
    window:{}, document:{querySelector:()=>({open:false})},
    fakeApi:path=>{const request=deferred();requests.push({path,...request});return request.promise;},
    fakeToast:(...args)=>notices.push(args), fakeRender:()=>renders.push('render')
  });
  vm.runInContext(source + `
    api=fakeApi;toast=fakeToast;report=()=>{};renderWorkspace=fakeRender;renderTabs=fakeRender;
    renderEditorBody=()=>{};renderEditorStatus=()=>{};renderInspector=()=>{};guardProperties=async()=>true;
    globalThis.app={state,readDrafts,openItem,openSkill,openExternal,closeTab,prepareTabs,removeOpenTabs,persistDrafts};`, context);
  context.app.readDrafts();
  return {...context.app, context, requests, notices, renders, store};
}
function draft(id, source, extra = {}) {
  return {id,source,name:'合成恢复草稿',draft:'磁盘上没有的未保存文字',etag:'old',when:Date.now(),...extra};
}
function stored(s, key) { return JSON.parse(s.store.get('yingxu:drafts'))[key]; }

test('project loading close retains recovered draft and late old response cannot affect same-key reopen', async()=>{
  const key='file:1',s=setup({[key]:draft('1','file')});
  const oldOpen=s.openItem('1');await Promise.resolve();const oldTab=s.state.tabs[0];
  await s.closeTab(key);assert.equal(s.state.tabs.length,0);assert.equal(stored(s,key).draft,'磁盘上没有的未保存文字');
  const newOpen=s.openItem('1');await Promise.resolve();const newTab=s.state.tabs[0];
  assert.notEqual(newTab,oldTab);const renderCount=s.renders.length;
  s.requests[0].resolve({id:'1',name:'旧响应',kind:'text'});await oldOpen;
  assert.equal(s.requests.length,2);assert.equal(s.renders.length,renderCount);assert.equal(newTab.item.name,'正在打开…');
  s.requests[1].resolve({id:'1',name:'新响应',kind:'text'});await new Promise(setImmediate);
  assert.equal(s.requests[2].path,'/api/content/1');
  s.requests[2].resolve({content:'磁盘正文',etag:'old',editable:true});await newOpen;
  assert.equal(newTab.item.name,'新响应');assert.equal(newTab.draft,'磁盘上没有的未保存文字');assert.equal(newTab.dirty,true);
  assert.equal(stored(s,key).draft,'磁盘上没有的未保存文字');
});

test('closing while content read is pending ignores its late body and keeps recovery',async()=>{
  const key='file:1',s=setup({[key]:draft('1','file')});const opening=s.openItem('1');await Promise.resolve();
  s.requests[0].resolve({id:'1',name:'合成文本',kind:'text'});await new Promise(setImmediate);
  assert.equal(s.requests[1].path,'/api/content/1');await s.closeTab(key);
  const renderCount=s.renders.length;s.requests[1].resolve({content:'磁盘正文',etag:'old',editable:true});await opening;
  assert.equal(s.state.tabs.length,0);assert.equal(s.renders.length,renderCount);
  assert.equal(stored(s,key).draft,'磁盘上没有的未保存文字');
});

test('failed project detail can be closed without erasing recovered draft',async()=>{
  const key='file:1',s=setup({[key]:draft('1','file')});const opening=s.openItem('1');await Promise.resolve();
  s.requests[0].reject(new Error('合成详情失败'));await opening;assert.equal(s.state.tabs[0].error,'合成详情失败');
  await s.closeTab(key);assert.equal(s.state.tabs.length,0);assert.equal(stored(s,key).draft,'磁盘上没有的未保存文字');
});

test('changed file type keeps a draft that cannot be applied to the loaded resource',async()=>{
  const key='file:1',s=setup({[key]:draft('1','file')});const opening=s.openItem('1');await Promise.resolve();
  s.requests[0].resolve({id:'1',name:'已变成图片',kind:'image'});await opening;
  assert.equal(s.state.tabs[0].unappliedDraft,true);assert.equal(s.state.tabs[0].dirty,false);
  s.persistDrafts(true);assert.equal(stored(s,key).draft,'磁盘上没有的未保存文字');
  assert.equal(await s.prepareTabs([...s.state.tabs]),false);
  assert.equal(await s.prepareTabs([...s.state.tabs],{exiting:true}),true);
  await s.closeTab(key);assert.equal(stored(s,key).draft,'磁盘上没有的未保存文字');
});

test('closed external load never migrates path draft; explicit new capability still restores it',async()=>{
  const oldKey=`external:${externalA}`,newKey=`external:${externalB}`,s=setup({[oldKey]:draft(externalA,'external',{path:'C:/synthetic/note.md'})});
  const oldOpen=s.openExternal(externalA);await Promise.resolve();await s.closeTab(oldKey);
  s.requests[0].resolve({id:externalA,name:'旧响应',kind:'text',path:'C:/synthetic/note.md',content:{content:'磁盘正文',etag:'old',editable:true}});await oldOpen;
  assert.equal(s.state.drafts[oldKey].draft,'磁盘上没有的未保存文字');assert.equal(s.state.drafts[newKey],undefined);
  const newOpen=s.openExternal(externalB);await Promise.resolve();
  s.requests[1].resolve({id:externalB,name:'新响应',kind:'text',path:'C:/synthetic/note.md',content:{content:'磁盘正文',etag:'old',editable:true}});await newOpen;
  assert.equal(s.state.drafts[oldKey],undefined);assert.equal(s.state.tabs[0].key,newKey);
  assert.equal(s.state.tabs[0].draft,'磁盘上没有的未保存文字');assert.equal(s.state.tabs[0].dirty,true);
});

test('skill loading close retains its draft and ignores late response',async()=>{
  const key='skill:1',s=setup({[key]:draft('1','skill')});s.state.skills=[{id:'1',name:'合成技能'}];
  const opening=s.openSkill('1');await Promise.resolve();await s.closeTab(key);const count=s.renders.length;
  s.requests[0].resolve({id:'1',name:'迟到技能',content:'磁盘正文',etag:'old',editable:true});await opening;
  assert.equal(s.state.tabs.length,0);assert.equal(s.renders.length,count);assert.equal(stored(s,key).draft,'磁盘上没有的未保存文字');
});

test('closing during lazy editor load cannot let an old skill response restore a closed tab',async()=>{
  const key='skill:1',s=setup({[key]:draft('1','skill')});s.state.skills=[{id:'1',name:'合成技能'}];
  const lazy=deferred();s.context.lazy=lazy.promise;vm.runInContext('ensureMarkdownLoaded=()=>lazy;',s.context);
  const opening=s.openSkill('1');await Promise.resolve();
  s.requests[0].resolve({id:'1',name:'技能',content:'磁盘正文',etag:'old',editable:true});await new Promise(setImmediate);
  assert.equal(s.state.tabs[0].loading,true);await s.closeTab(key);const count=s.renders.length;
  lazy.resolve();await opening;assert.equal(s.state.tabs.length,0);assert.equal(s.renders.length,count);
  assert.equal(stored(s,key).draft,'磁盘上没有的未保存文字');
});

test('exit and batch preparation wait for loading instead of treating pending draft as clean',async()=>{
  const key='file:1',s=setup({[key]:draft('1','file')});const opening=s.openItem('1');await Promise.resolve();
  assert.equal(await s.prepareTabs([...s.state.tabs],{exiting:true}),false);
  assert.equal(await s.prepareTabs([...s.state.tabs]),false);
  assert.equal(stored(s,key).draft,'磁盘上没有的未保存文字');
  await s.closeTab(key);s.requests[0].resolve({id:'1',name:'迟到',kind:'text'});await opening;
});

test('batch removal preserves new same-key tab and dirty or pending drafts',async()=>{
  const key='file:1',s=setup({[key]:draft('1','file')});
  const old={key,id:'1',source:'file',item:{kind:'text',name:'旧标签'},dirty:false};
  const current={key,id:'1',source:'file',item:{kind:'text',name:'新标签'},dirty:true,draft:'新输入',content:{editable:true,etag:'old'}};
  s.state.tabs=[current];s.state.activeKey=key;s.removeOpenTabs([old]);
  assert.equal(s.state.tabs[0],current);assert.equal(stored(s,key).draft,'新输入');
  s.removeOpenTabs([current]);assert.equal(s.state.tabs[0],current);
});

test('explicit discard closes loaded tab and clears its draft',async()=>{
  const key='file:1',s=setup({[key]:draft('1','file')});
  const tab={key,id:'1',source:'file',item:{kind:'text',name:'合成文件'},content:{content:'磁盘正文',editable:true},draft:'未保存文字',dirty:true};
  s.state.tabs=[tab];s.state.activeKey=key;vm.runInContext('choose=async()=>"discard";',s.context);
  await s.closeTab(key);assert.equal(s.state.tabs.length,0);assert.equal(stored(s,key),undefined);
});
