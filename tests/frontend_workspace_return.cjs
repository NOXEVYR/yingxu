'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');

// Keep the real navigation, draft guard, workspace lifecycle and list requests.
// Only replace media/editor rendering and unrelated background controllers.
function setup() {
  const nodes = new Map(), requests = [], messages = [], rendered = [], notices = [], saved = new Map();
  const document = {
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector,{innerHTML:'',hidden:false,value:'',classList:{toggle(){}},replaceChildren(){},insertAdjacentHTML(_where,html){this.innerHTML+=html;}});
      return nodes.get(selector);
    },
    querySelectorAll(){return [];},
  };
  const context = vm.createContext({console,document,window:{chrome:{webview:{postMessage:value=>messages.push(value)}}},localStorage:{getItem:key=>saved.get(key)??null,setItem:(key,value)=>saved.set(key,value)},URLSearchParams,AbortController,requests,rendered,notices});
  vm.runInContext(source+`
    renderNavigation=()=>{};renderHero=()=>{};renderInspector=()=>{};
    renderEditorToolbar=()=>{};renderEditorStatus=()=>{};
    renderEditorBody=tab=>rendered.push(tab);
    hideMenu=()=>{};updateSelection=()=>{};observeThumbnails=()=>{};
    renderResourceGroups=()=>{};groupController=()=>null;
    toast=message=>notices.push(message);report=error=>{throw error;};
    loadSkills=async()=>{};loadTrash=async()=>{};loadContext=async()=>{};
    api=async url=>{requests.push(url);return url.startsWith('/api/folders?')?{folders:state.testFolders||[],truncated:!!state.testFoldersTruncated}:{items:[],total:200,categories:[]};};
    globalThis.app={state,selectSection,returnToWorkspace,activeTab};
  `,context);
  const {state} = context.app;
  const draft = {key:'file:draft',id:'draft',source:'file',item:{name:'未保存剧本',kind:'markdown',project_id:'p'},content:{editable:true},draft:'未保存的中文正文\r\n第二行',dirty:true,mode:'edit'};
  const word = {key:'file:word',id:'word',source:'file',item:{name:'跨页文稿',kind:'docx',project_id:'p'},paragraphs:[{id:'1',text:'第一页草稿'},{id:'81',text:'第三页草稿'}],dirty:true,docxPage:2};
  Object.assign(state,{projects:[{id:'p'},{id:'other'}],projectId:'p',section:'assets',category:'scripts',folderId:'nested',folderPage:1,folderScope:'current',offset:96,q:'夜景',status:'进行中',kind:'markdown',sort:'name',view:'list',activeKey:draft.key,tabs:[draft,word],drafts:{[draft.key]:{draft:draft.draft}},selectedIds:new Set(['selected'])});
  state.testFolders=[{id:'parent',name:'夜景父目录',category:'scripts',parent_id:null},{id:'nested',name:'夜景子目录',category:'scripts',parent_id:'parent'},...Array.from({length:15},(_,i)=>({id:`child-${i}`,name:`夜景 ${i}`,category:'scripts',parent_id:'nested'}))];
  return {...context.app,context,nodes,requests,messages,rendered,notices,draft,word,saved};
}
function location(state) {
  return Object.fromEntries(['projectId','section','category','folderId','folderPage','folderScope','offset','q','status','kind','sort','view','activeKey'].map(key=>[key,state[key]]));
}

test('return restores project/category/nested folder, query, filters, pages and view after multiple workspaces',async()=>{
  const s=setup(), before=location(s.state);
  await s.selectSection('skills');
  assert.equal(s.nodes.get('#returnWorkspaceButton').hidden,false);
  Object.assign(s.state,{q:'技能搜索',offset:48});
  await s.selectSection('trash');
  Object.assign(s.state,{q:'回收搜索',offset:0});
  await s.returnToWorkspace();
  assert.deepEqual(location(s.state),before);
  assert.equal(s.nodes.get('#returnWorkspaceButton').hidden,true);
  for (const [selector,key] of [['#searchInput','q'],['#statusFilter','status'],['#kindFilter','kind'],['#sortFilter','sort'],['#folderScope','folderScope']]) assert.equal(s.nodes.get(selector).value,before[key]);
  const query=new URLSearchParams(s.requests.find(url=>url.startsWith('/api/items?')).split('?')[1]);
  for (const [key,value] of Object.entries({project:'p',category:'scripts',folder:'nested',offset:'96',q:'夜景',status:'进行中',kind:'markdown',sort:'name'})) assert.equal(query.get(key),value);
  assert.equal(s.state.selectedIds.size,0);
});

test('unsaved text and cross-page Word drafts stay open and regain the same active tab without saving',async()=>{
  const s=setup(), tabs=s.state.tabs, drafts=s.state.drafts, paragraphs=s.word.paragraphs;
  await s.selectSection('context');
  assert.equal(s.state.activeKey,null);
  assert.equal(s.state.tabs,tabs);
  assert.equal(s.draft.dirty,true);
  await s.returnToWorkspace();
  assert.equal(s.activeTab(),s.draft);
  assert.equal(s.rendered.at(-1),s.draft);
  assert.equal(s.draft.draft,'未保存的中文正文\r\n第二行');
  assert.equal(s.state.drafts,drafts);
  assert.equal(s.word.paragraphs,paragraphs);
  assert.equal(s.word.paragraphs[1].text,'第三页草稿');
  assert.equal(s.word.docxPage,2);
  assert.equal(s.word.dirty,true);
  assert.ok(s.requests.every(url=>url.startsWith('/api/items?')||url.startsWith('/api/folders?')));
});

test('a newly opened dirty SKILL remains open when returning to the previous document',async()=>{
  const s=setup();await s.selectSection('skills');
  const skill={key:'skill:new',source:'skill',item:{kind:'skill',name:'新技能'},draft:'未保存技能',dirty:true};
  s.state.tabs.push(skill);s.state.activeKey=skill.key;
  await s.returnToWorkspace();
  assert.equal(s.activeTab(),s.draft);
  assert.ok(s.state.tabs.includes(skill));assert.equal(skill.draft,'未保存技能');assert.equal(skill.dirty,true);
});

test('cancelled property confirmation or active composition cannot change the return location',async()=>{
  const s=setup();await s.selectSection('skills');
  const skill={key:'skill:new',source:'skill',item:{kind:'skill',name:'新技能'},propertiesDirty:true,propertiesDraft:{name:'改名'}};
  s.state.tabs.push(skill);s.state.activeKey=skill.key;
  vm.runInContext('choose=async()=>"cancel";',s.context);
  const before=location(s.state);await s.returnToWorkspace();
  assert.deepEqual(location(s.state),before);assert.equal(skill.propertiesDirty,true);assert.equal(s.requests.length,0);
  skill.propertiesDirty=false;skill.textComposing=true;
  await s.returnToWorkspace();assert.deepEqual(location(s.state),before);assert.equal(s.requests.length,0);assert.equal(s.notices.length,1);
});

test('closed remembered tab is not reopened and returning does not resurrect its draft',async()=>{
  const s=setup();await s.selectSection('trash');
  s.state.tabs=s.state.tabs.filter(tab=>tab!==s.draft);delete s.state.drafts[s.draft.key];
  await s.returnToWorkspace();
  assert.equal(s.state.activeKey,null);assert.equal(s.state.drafts[s.draft.key],undefined);assert.equal(s.state.tabs.length,1);
});

test('the next departure replaces the saved workspace rather than retaining an older location',async()=>{
  const s=setup();await s.selectSection('skills');await s.returnToWorkspace();
  Object.assign(s.state,{category:'characters',folderId:null,folderPage:0,folderScope:'all',q:'新检索',offset:48,view:'grid'});
  const expected=location(s.state);
  await s.selectSection('trash');await s.returnToWorkspace();
  assert.deepEqual(location(s.state),expected);
});

test('return from a different project persists the restored project selection',async()=>{
  const s=setup();await s.selectSection('skills');
  s.state.projectId='other';s.saved.set('yingxu:project','other');
  await s.returnToWorkspace();
  assert.equal(s.state.projectId,'p');assert.equal(s.saved.get('yingxu:project'),'p');
});

test('removed remembered folder falls back to category root before requesting items',async()=>{
  const s=setup();await s.selectSection('trash');
  s.state.testFolders=s.state.testFolders.filter(folder=>folder.id!=='nested');
  await s.returnToWorkspace();
  assert.equal(s.state.category,'scripts');assert.equal(s.state.folderId,null);assert.equal(s.state.folderPage,0);assert.equal(s.state.offset,0);
  const itemRequests=s.requests.filter(url=>url.startsWith('/api/items?'));
  assert.equal(itemRequests.length,1);
  const query=new URLSearchParams(itemRequests[0].split('?')[1]);
  assert.equal(query.get('folder'),'root');assert.equal(query.get('offset'),'0');
  assert.equal(s.activeTab(),s.draft);assert.equal(s.draft.dirty,true);
});

test('a truncated directory list cannot prove that the remembered folder was removed',async()=>{
  const s=setup();await s.selectSection('trash');
  s.state.testFolders=[];s.state.testFoldersTruncated=true;
  await s.returnToWorkspace();
  assert.equal(s.state.folderId,'nested');assert.equal(s.state.offset,96);
  const query=new URLSearchParams(s.requests.find(url=>url.startsWith('/api/items?')).split('?')[1]);
  assert.equal(query.get('folder'),'nested');
});

test('folder preflight cannot overwrite a newer navigation while its response is pending',async()=>{
  const s=setup();await s.selectSection('skills');
  let resolve;
  s.context.waitForFolders=()=>new Promise(done=>{resolve=done;});
  vm.runInContext('api=async url=>{requests.push(url);return waitForFolders();};',s.context);
  const pending=s.returnToWorkspace();await Promise.resolve();await Promise.resolve();
  assert.equal(typeof resolve,'function');
  Object.assign(s.state,{section:'assets',projectId:'other',category:'characters',folderId:'new-folder',offset:0,q:'新位置'});
  const expected=location(s.state);
  resolve({folders:[]});await pending;
  assert.deepEqual(location(s.state),expected);
  assert.equal(s.requests.filter(url=>url.startsWith('/api/items?')).length,0);
  assert.equal(s.saved.has('yingxu:project'),false);
});
