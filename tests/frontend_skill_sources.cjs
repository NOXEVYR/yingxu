'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {test}=require('node:test');
const source=fs.readFileSync('frontend/app.js','utf8').replace(/boot\(\);\s*$/,'');
function setup(){
 const nodes=new Map(),calls=[],dialogs=[],messages=[];
 const node=key=>{if(!nodes.has(key))nodes.set(key,{open:false,hidden:false,value:'',innerHTML:'',textContent:'',disabled:false,addEventListener(){},focus(){this.focused=true;},classList:{toggle(){}}});return nodes.get(key);};
 const registry={all_total:3,groups:[{id:'codex',label:'Codex',count:1},{id:'custom',label:'自定义',count:1},{id:'yingxu',label:'映序本地',count:1}],sources:[
  {id:'codex-home',source:'codex',label:'Codex 用户目录',path:'C:/synthetic/<home>/skills',enabled:true,custom:false,status:'ready',count:1,readonly:true},
  {id:'custom-one',source:'custom',label:'团队 <规范>',path:'C:/synthetic/team&skills',enabled:true,custom:true,status:'missing',count:1,readonly:true},
  {id:'yingxu',source:'yingxu',label:'映序本地',path:'C:/synthetic/local',enabled:true,custom:false,status:'ready',count:1,readonly:false}]};
 const context=vm.createContext({URLSearchParams,setTimeout,localStorage:{getItem:()=>null,setItem(){}},window:{},
  document:{querySelector:node,querySelectorAll:()=>[]},
  registry,fixtureApi:async(url,options)=>{calls.push({url,options});return url.startsWith('/api/skills?')?{...registry,skills:[{id:'a'.repeat(32),name:'<只读能力>',source:'codex-home',source_group:'codex',source_label:'Codex',editable:false,bound:true}]}:registry;},
  fixtureDialog:value=>{dialogs.push(value);node('#appDialog').open=true;return node('#appDialog');},fixtureToast:(...values)=>messages.push(values)});
 vm.runInContext(source+`\napi=fixtureApi;showDialog=fixtureDialog;toast=fixtureToast;report=error=>fixtureToast(error.message);configureSection=()=>{};renderInspector=()=>{};hideMenu=()=>{};globalThis.app={state,skillSourceState,acceptSkillSources,skillSourcesHtml,skillSourceRegistryHtml,setSkillSourceFilter,skillSourcesDialog,skillSourceAction,refreshSkillSources,loadSkills,renderSkills,handleAction};`,context);
 context.app.state.section='skills';context.app.state.projectId='project-fixture';context.app.state.bootstrap={capabilities:{native_picker:true}};context.app.acceptSkillSources(registry);
 return {context,...context.app,node,nodes,calls,dialogs,messages,registry};
}
test('source categories show fixed full counts and escaped labels/paths without altering readonly cards',async()=>{
 const s=setup();await s.loadSkills();const html=s.node('#resourceItems').innerHTML;
 assert.match(html,/Codex/);assert.match(html,/全部来源/);assert.match(html,/aria-pressed="true"/);assert.match(html,/只读源文件/);assert.match(html,/项目已启用/);assert.match(html,/data-skill-menu/);assert.match(html,/&lt;只读能力&gt;/);
 const manager=s.skillSourceRegistryHtml();assert.match(manager,/&lt;home&gt;/);assert.match(manager,/team&amp;skills/);assert.match(manager,/未找到目录/);assert.match(manager,/始终启用/);assert.doesNotMatch(manager,/<home>|<规范>/);
 assert.equal((manager.match(/data-action="remove-skill-source"/g)||[]).length,1);
});
test('group then directory filters combine with search/project, reset pagination and never scan',async()=>{
 const s=setup();s.state.q='视频 规范';s.state.offset=96;
 await s.setSkillSourceFilter('group','codex');assert.equal(s.state.offset,0);
 await s.setSkillSourceFilter('directory','codex-home');
 const url=new URL(s.calls.at(-1).url,'http://fixture');assert.equal(url.searchParams.get('source'),'codex');assert.equal(url.searchParams.get('source_id'),'codex-home');assert.equal(url.searchParams.get('q'),'视频 规范');assert.equal(url.searchParams.get('project'),'project-fixture');
 assert.ok(s.calls.every(call=>!call.options?.method));
 const count=s.calls.length;await s.setSkillSourceFilter('directory','custom-one');assert.equal(s.calls.length,count);
 await s.setSkillSourceFilter('group','');assert.equal(s.skillSourceState.directory,'');assert.equal(s.state.q,'视频 规范');
});
test('late list response cannot overwrite a newer source selection or another section',async()=>{
 const s=setup();const waits=[];s.context.fixtureWait=()=>new Promise(resolve=>waits.push(resolve));vm.runInContext('api=fixtureWait;',s.context);
 const first=s.loadSkills(),second=s.loadSkills();waits[1]({...s.registry,skills:[{id:'new',name:'新结果'}]});await second;waits[0]({...s.registry,skills:[{id:'old',name:'旧结果'}]});await first;assert.equal(s.state.skills[0].id,'new');
 const third=s.loadSkills();s.state.section='assets';waits[2]({...s.registry,skills:[{id:'bad'}]});await third;assert.equal(s.state.skills[0].id,'new');
});
test('registry opens read-only metadata without scanning and preserves existing modal',async()=>{
 const s=setup();await s.skillSourcesDialog();assert.equal(s.calls[0].url,'/api/skill-sources');assert.equal(s.calls[0].options,undefined);assert.match(s.dialogs[0].body,/外部 SKILL 保持只读|只读取其中的 SKILL.md/);
 await s.skillSourcesDialog();assert.equal(s.calls.length,1);
});
test('custom add and toggle write only registry endpoint then reload filtered list',async()=>{
 const s=setup();await s.skillSourcesDialog();s.node('#skillSourcePath').value='C:/synthetic/new';s.node('#skillSourceLabel').value='自定义团队';
 await s.skillSourceAction('add');const post=s.calls.find(call=>call.options?.method==='POST');assert.equal(post.url,'/api/skill-sources');assert.equal(post.options.body.path,'C:/synthetic/new');assert.equal(s.node('#skillSourcePath').value,'');
 s.skillSourceState.group='codex';await s.skillSourceAction('toggle','codex-home');const patch=s.calls.find(call=>call.options?.method==='PATCH');assert.equal(patch.url,'/api/skill-sources/codex-home');assert.equal(patch.options.body.enabled,false);assert.match(s.calls.at(-1).url,/source=codex/);assert.equal(s.skillSourceState.busy,false);assert.equal(s.state.modalBusy,false);
});
test('only custom locations can be removed; removal clears directory filter without deleting skill files',async()=>{
 const s=setup();await s.skillSourcesDialog();const count=s.calls.length;await s.skillSourceAction('remove','codex-home');await s.skillSourceAction('toggle','yingxu');assert.equal(s.calls.length,count);
 s.skillSourceState.directory='custom-one';await s.skillSourceAction('remove','custom-one');const deletes=s.calls.filter(call=>call.options?.method==='DELETE');assert.equal(deletes.length,1);assert.equal(deletes[0].url,'/api/skill-sources/custom-one');assert.equal(s.skillSourceState.directory,'');assert.ok(s.calls.every(call=>!call.url.includes('/api/trash')&&!call.url.includes('/api/content')));
});
test('explicit scan is single-flight and releases controls after failure',async()=>{
 const s=setup();let reject;s.context.pendingScan=()=>new Promise((resolve,fail)=>reject=fail);vm.runInContext('api=pendingScan;',s.context);
 const first=s.refreshSkillSources();assert.equal(s.skillSourceState.busy,true);await s.refreshSkillSources();await s.setSkillSourceFilter('group','custom');assert.equal(s.skillSourceState.group,'');reject(new Error('synthetic scan failed'));await assert.rejects(first,/synthetic scan failed/);assert.equal(s.skillSourceState.busy,false);
});
test('failed custom registration retains typed path and leaves external readonly data intact',async()=>{
 const s=setup();await s.skillSourcesDialog();s.node('#skillSourcePath').value='C:/synthetic/keep';s.context.rejectApi=async()=>{throw Error('拒绝访问 <fixture>');};vm.runInContext('api=rejectApi;',s.context);await s.skillSourceAction('add');assert.equal(s.node('#skillSourcePath').value,'C:/synthetic/keep');assert.equal(s.node('#skillSourceNotice').textContent,'拒绝访问 <fixture>');assert.equal(s.skillSourceState.sources[0].readonly,true);assert.equal(s.state.modalBusy,false);
});
test('native folder picker can cancel without adding a source',async()=>{
 const s=setup();await s.skillSourcesDialog();s.context.pickApi=async()=>({paths:[]});vm.runInContext('api=pickApi;',s.context);await s.skillSourceAction('pick');assert.equal(s.node('#skillSourcePath').value,'');assert.equal(s.state.modalBusy,false);assert.equal(s.skillSourceState.busy,false);
});
test('project binding refresh keeps source and search filters and does not grant external editing',async()=>{
 const s=setup();s.skillSourceState.group='codex';s.skillSourceState.directory='codex-home';s.state.q='规范';
 s.state.tabs=[{key:'skill:'+'a'.repeat(32),id:'a'.repeat(32),source:'skill',item:{bound:false,editable:false},content:{editable:false}}];s.state.activeKey=s.state.tabs[0].key;
 vm.runInContext('renderSkillInspector=()=>{};',s.context);
 await s.handleAction('bind-skill',{disabled:false});
 assert.equal(s.calls[0].url,'/api/skills/bind');assert.equal(s.calls[0].options.body.project_id,'project-fixture');
 const url=new URL(s.calls.at(-1).url,'http://fixture');assert.equal(url.searchParams.get('source'),'codex');assert.equal(url.searchParams.get('source_id'),'codex-home');assert.equal(url.searchParams.get('q'),'规范');assert.equal(s.state.tabs[0].content.editable,false);
});
test('source errors remain visible when the registry has no transient errors array',async()=>{
 const s=setup();s.registry.sources[0].status='error';s.registry.errors=[];await s.skillSourcesDialog();assert.match(s.node('#skillSourceNotice').textContent,/未能完整扫描/);
 s.node('#appDialog').open=false;await s.refreshSkillSources();assert.equal(s.messages.at(-1)[1],'info');assert.match(s.messages.at(-1)[0],/未完整读取/);
});
test('custom labels share the backend 80 character limit and refuse oversized writes locally',async()=>{
 const s=setup();await s.skillSourcesDialog();assert.match(s.dialogs[0].body,/id="skillSourceLabel" maxlength="80"/);s.node('#skillSourcePath').value='C:/synthetic/keep';s.node('#skillSourceLabel').value='规'.repeat(81);const count=s.calls.length;await s.skillSourceAction('add');assert.equal(s.calls.length,count);assert.match(s.node('#skillSourceNotice').textContent,/80/);assert.equal(s.node('#skillSourcePath').value,'C:/synthetic/keep');
});
