'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
function fixture(){
 const nodes=new Map(),calls=[],layouts=[],doc={documentElement:{dataset:{}},querySelector:key=>{if(!nodes.has(key))nodes.set(key,{classList:{toggle:(name,value)=>layouts.push([name,value])},focus(){},value:''});return nodes.get(key);}};
 const context=vm.createContext({window:{},document:doc,localStorage:{getItem:()=>null},setTimeout,clearTimeout,URLSearchParams,calls});
 const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
 vm.runInContext(source+`;api=async(url,options)=>{calls.push({url,options});return {...state.bootstrap.settings,...options.body};};toast=()=>{};renderNavigation=()=>calls.push('navigation');renderSkills=()=>calls.push('skills');renderWorkspace=()=>{throw Error('Mode switch must not render editor');};globalThis.fixture={state,setWorkspaceLayout,applyWorkbenchLayout,focusWorkbench,skillNavigationHtml,preference};`,context);
 const ui=context.fixture;ui.state.bootstrap={settings:{workspace_layout:'focus'}};return {...ui,context,nodes,calls,layouts};
}
test('mode changes only layout and persists preference while keeping editor and draft identities',async()=>{
 const s=fixture(),editor={undo:['kept'],selection:{anchor:3}};
 const tab={key:'file:a',source:'file',item:{kind:'markdown'},draft:'正文未保存',dirty:true,markdownEditor:editor};
 Object.assign(s.state,{tabs:[tab],activeKey:tab.key,section:'skills',projectId:'project',q:'query',offset:48});
 for(const mode of ['classic','focus','classic','focus'])await s.setWorkspaceLayout(mode);
 assert.equal(s.state.tabs[0],tab);assert.equal(tab.markdownEditor,editor);assert.equal(tab.draft,'正文未保存');assert.equal(tab.dirty,true);
 assert.equal(s.state.projectId,'project');assert.equal(s.state.q,'query');assert.equal(s.state.offset,48);assert.equal(s.state.activeKey,tab.key);
 assert.equal(s.calls.filter(x=>x?.url).length,4);assert.ok(s.calls.filter(x=>x?.url).every(x=>x.url==='/api/settings'&&x.options.method==='PATCH'&&Object.keys(x.options.body).join()==='workspace_layout'));
 assert.ok(s.layouts.filter(x=>x[0]==='skill-focus-view').every(x=>x[1]===false),'An open document must never be hidden by list-only focus layout');
});
test('failure and duplicate clicks cannot claim saved mode or make extra writes',async()=>{
 const s=fixture();await s.setWorkspaceLayout('focus');await s.setWorkspaceLayout('unknown');assert.equal(s.calls.length,0);
 vm.runInContext("api=async()=>{throw Error('offline');}",s.context);
 await assert.rejects(s.setWorkspaceLayout('classic'),/offline/);assert.equal(s.preference('workspace_layout'),'focus');assert.equal(s.state.layoutSaving,false);
});
test('skills sidebar escapes metadata and bounds visible tags; resource navigation is a reachable action',()=>{
 const s=fixture(),nav={selected:{view:'all',folder:'*',tag:''},organizationEnabled:true,scopes:[{id:'all',label:'全部技能',count:30}],folders:[{id:'f',name:'<script>',label:'<script>',count:2}],tags:Array.from({length:80},(_,i)=>({id:'t'+i,label:'tag'+i,count:1}))};
 const html=s.skillNavigationHtml(nav);assert.match(html,/data-action="return-workspace"/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);assert.equal((html.match(/data-skill-tag=/g)||[]).length,17);assert.match(html,/查看全部 80 个标签/);
});
