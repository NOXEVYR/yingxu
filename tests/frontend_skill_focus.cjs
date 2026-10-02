'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {test}=require('node:test'),{execFile}=require('node:child_process'),{promisify}=require('node:util'),{pathToFileURL}=require('node:url');

async function runFixture(t,width,interactions,mode='focus'){
  const browser=[process.env.YINGXU_TEST_BROWSER,'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(value=>value&&fs.existsSync(value));
  if(!browser){t.skip('Existing Chromium required; no downloads');return;}
  const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'yingxu-skill-focus-'));
  t.after(()=>{const resolved=path.resolve(temporary);assert.ok(resolved.startsWith(path.resolve(os.tmpdir())+path.sep)&&path.basename(resolved).startsWith('yingxu-skill-focus-'));fs.rmSync(resolved,{recursive:true,force:true,maxRetries:10,retryDelay:100});});
  for(const file of ['workflow-library.js','workflow-library.css','styles.css','appearance.css','marquee.js'])fs.copyFileSync(path.join(__dirname,'../frontend',file),path.join(temporary,file));
  const runner=`(async()=>{
    const checks=[],errors=[],check=(name,value)=>{checks.push({name,ok:!!value});if(!value)throw Error(name);};
    const tick=(delay=0)=>new Promise(resolve=>setTimeout(resolve,delay));
    const e=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
    const root=document.querySelector('#root'),viewport=document.querySelector('#viewport');
    const skills=Array.from({length:16},(_,i)=>({id:'s'+i,name:i===5?'team-mode（小队模式）':i===6?'oil-codex-title（话题命名）':i===2?'<img src=x onerror=window.injected=true>':'合成创作方法 '+i,description:'把故事、角色与镜头安排整理成可执行的创作计划，检查资料和制作约定是否一致。',source_label:i%2?'Codex 插件技能':'Codex 用户技能',category:i===5||i===6?'development':'visual',editable:i===15,bound:false}));
    const collections=[{id:'col0',skill_id:'s0',name:'已收藏的合成方法',description:skills[0].description,category:'visual',origin:{source_label:'Codex 用户技能',source_id:'codex'},version:'current-version-2',bound_version:'fixed-version-1',bound:true,versions:[{version:'current-version-2',total_bytes:120},{version:'fixed-version-1',total_bytes:100}]}];
    const folders=[{id:'writing',name:'剧本写作',path:'剧本写作',parent_id:''},{id:'boards',name:'分镜',path:'剧本写作/分镜',parent_id:'writing'}];
    const metadata=new Map([['s0',{skill_id:'s0',folder_id:'boards',tags:['常用','项目模板'],notes:'固定项目版本'}],['s2',{skill_id:'s2',folder_id:'writing',tags:['常用'],notes:'独立备注'}]]);
    const calls=[],dialogs=[],opened=[],toasts=[];let focus=${mode==='focus'},globalOpen=0,navigationEvents=0,ui;
    const state={section:'skills',projectId:'project-fixture',bootstrap:{capabilities:{skill_organization:true}},skills,q:'',offset:0,limit:48,tabs:[],selectedIds:new Set()};
    const api=async(url,options)=>{
      const method=options?.method||'GET',body=options?.body;calls.push({url,method,body});
      if(url.startsWith('/api/skill-collections?'))return {collections};
      if(url==='/api/skill-organization')return {folders,metadata:[...metadata.values()]};
      if(url==='/api/skill-metadata'){
        for(const id of body.skill_ids){const row={skill_id:id,folder_id:'',tags:[],notes:'',...metadata.get(id)};if(Object.hasOwn(body,'folder_id'))row.folder_id=body.folder_id;if(Object.hasOwn(body,'tags'))row.tags=body.tags_mode==='append'?[...new Set([...row.tags,...body.tags])]:body.tags;if(Object.hasOwn(body,'notes'))row.notes=body.notes;metadata.set(id,row);}return {updated:body.skill_ids.length};
      }
      if(url==='/api/skill-collections/preview')return {token:'preview-'+body.skill_id,name:skills.find(x=>x.id===body.skill_id).name,version:'package-version-1',total_bytes:120,file_count:3,files:[{path:'SKILL.md'},{path:'references/example.md'},{path:'scripts/sample.py'}],warnings:['合成包预览']};
      if(url==='/api/skill-collections/collect'){const id=body.token.slice(8),source=skills.find(x=>x.id===id);collections.push({...source,id:'col-'+id,skill_id:id,version:'package-version-1',bound:false,versions:[{version:'package-version-1',total_bytes:120}],origin:{source_label:source.source_label}});return {ok:true};}
      if(url==='/api/skill-collections/bind'){const own=collections.find(x=>x.id===body.id);own.bound=body.bound;own.bound_version=body.bound?(body.version||own.version):null;return {ok:true};}
      if(url.startsWith('/api/skill-collections/col'))return {name:'固定包说明',content:'# 真实契约的合成包文稿',path:'synthetic-package/SKILL.md'};
      throw Error('Unexpected fixture API: '+method+' '+url);
    };
    const closeDialogs=()=>{for(const dialog of document.querySelectorAll('dialog')){dialog.close();dialog.remove();}};
    const showDialog=spec=>{
      closeDialogs();const dialog=document.createElement('dialog'),form=document.createElement('form');
      form.innerHTML='<h2>'+e(spec.title)+'</h2><p>'+e(spec.subtitle)+'</p>'+spec.body+(spec.actions||'<button type="submit">'+e(spec.submit||'确定')+'</button><button type="button" data-dialog-cancel>取消</button>');dialog.append(form);document.body.append(dialog);
      const entry={dialog,form,spec,pending:Promise.resolve()};dialogs.push(entry);
      form.onsubmit=event=>{event.preventDefault();entry.pending=Promise.resolve(spec.onSubmit?.(form)).then(()=>{dialog.close();dialog.remove();}).catch(error=>{errors.push(String(error));throw error;});};
      form.querySelector('[data-dialog-cancel]')?.addEventListener('click',()=>{dialog.close();dialog.remove();});dialog.showModal();return dialog;
    };
    const env={state,api,escapeHtml:e,icon:()=>'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14"/></svg>',showDialog,toast:message=>toasts.push(message),report:error=>errors.push(String(error)),copyText:async()=>{},formatSize:n=>n+' bytes',sourcesHtml:()=>'<div class="skill-source-toolbar"><div class="skill-source-top"><span>来源信息</span><button type="button" data-action="manage-skill-sources">扫描位置</button></div><div>Codex · 扫描完成 · 16 项</div></div>',sourceSummary:()=> '2 个来源 · 扫描完成',setSource:async()=>{},pagination:()=>{},openSkill:async id=>opened.push(id),reloadSkills:async()=>{},render:()=>ui.render(root),focusLayout:()=>focus,onNavigationChange:()=>navigationEvents++};
    // Mirrors app.js's delegated external-skill open so duplicate activation is observable.
    document.addEventListener('click',event=>{if(event.target.closest('[data-skill]')&&!event.target.closest('[data-skill-menu]'))globalOpen++;});
    ui=window.YingXuWorkflow.create(env);await ui.load();ui.render(root);
    const card=id=>root.querySelector('[data-workflow-card="'+id+'"]'),command=(name,id='')=>root.querySelector('[data-workflow="'+name+'"][data-id="'+id+'"]');
    check('focus reads real contracts without scanning',calls.length===2&&calls.every(x=>x.method==='GET'));
    check('favorite deduplication keeps every unique skill',root.querySelectorAll('[data-workflow-card]').length===16&&!card('s0')&&!!card('col0'));
    check('compact tools collapse source and organization walls',root.classList.contains('skill-polished-library')&&!root.querySelector('.skill-library-sources').open&&!root.querySelector('.skill-organization-intro').getClientRects().length);
    check('injected external title remains escaped',!root.querySelector('img')&&card('s2').textContent.includes('<img src=x'));
    check('fixed project badge uses pinned version',card('col0').querySelector('.skill-focus-bound').getAttribute('title').includes('fixed-version-1')&&!card('col0').querySelector('.skill-focus-bound').getAttribute('title').includes('current-version-2'));
    const nav=ui.navigation();check('navigation exposes scopes folders tags and source-deduplicated counts',nav.scopes.find(x=>x.id==='all').count===16&&nav.folders.find(x=>x.id==='writing').count===1&&nav.tags.find(x=>x.id==='常用').count===2&&nav.current.folder==='*'&&navigationEvents>=1);
    const assertBounds=()=>{for(const node of root.querySelectorAll('[data-workflow-card]')){const r=node.getBoundingClientRect();for(const child of node.querySelectorAll('.skill-card-top,.skill-focus-copy,.skill-focus-metadata,.skill-card-bottom,.skill-card-folder,.skill-card-tags,.skill-focus-tag')){const c=child.getBoundingClientRect();if(c.width&&c.height&&(c.left<r.left-1||c.right>r.right+1||c.top<r.top-1||c.bottom>r.bottom+1))throw Error('Card child outside border: '+node.dataset.workflowCard+' '+child.className);}}};
    assertBounds();check('visible metadata and tags stay within card borders',true);
    check('viewport has no horizontal overflow',viewport.scrollWidth<=viewport.clientWidth+1&&document.documentElement.scrollWidth<=innerWidth+1);
    if(innerWidth>1000)check('desktop first card follows compact shared tools',root.querySelector('[data-workflow-card]').getBoundingClientRect().top<=${mode==='focus'?220:265});
    if(innerWidth<=720&&innerWidth>480)check('540px retains two card columns',card('col0').getBoundingClientRect().top===card('s1').getBoundingClientRect().top);
    if(innerWidth<=480)check('small window uses one card column',card('s1').getBoundingClientRect().top>card('col0').getBoundingClientRect().bottom);
    if(${!!interactions}){
      const initialCalls=calls.length;ui.navigate({view:'favorites',folder:'boards',tag:'常用'});check('sidebar navigation combines scope folder and tag without writes or scans',root.querySelectorAll('[data-workflow-card]').length===1&&ui.navigation().selected.tag==='常用'&&calls.length===initialCalls);
      ui.navigate({view:'all',folder:'*',tag:''});
      const first=card('s1');first.click();check('single click selects without replacing the card',card('s1')===first&&first.classList.contains('checked')&&globalOpen===0&&dialogs.length===0);
      await tick(600);check('single click never schedules a dialog',dialogs.length===0);command('organize','s1').click();check('explicit organization opens real organization dialog',dialogs.at(-1)?.dialog.open&&dialogs.at(-1).spec.title==='分类、标签与备注');
      const edit=dialogs.at(-1);edit.form.elements.namedItem('folder').value='writing';edit.form.elements.namedItem('tags').value='常用'+String.fromCharCode(10)+'复用';edit.form.elements.namedItem('notes').value='保存后的真实整理备注';edit.form.querySelector('[type=submit]').click();await edit.pending;await tick();
      check('explicit organization submits native form to metadata API',calls.some(x=>x.url==='/api/skill-metadata'&&x.body.skill_ids.join(',')==='s1'&&x.body.notes==='保存后的真实整理备注')&&metadata.get('s1').tags.includes('复用'));
      check('metadata save neither rewrites sources nor changes fixed versions',calls.every(x=>x.url!='/api/skills')&&collections[0].bound_version==='fixed-version-1');
      const beforeDialogs=dialogs.length,dbl=card('s2');dbl.click();dbl.click();dbl.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));await tick(470);
      check('double click reads the original skill without a delayed dialog',opened.join(',')==='s2'&&dialogs.length===beforeDialogs&&card('s2')===dbl&&globalOpen===0);
      card('s1').click();card('s1').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));await tick(470);check('selection does not create a delayed dialog',dialogs.length===beforeDialogs);
      ui.setSelection(new Set());const box=card('s1').querySelector('input');box.closest('label').click();await tick(470);check('checkbox label selects without delegated source opening',ui.getSelection().has('s1')&&dialogs.length===beforeDialogs&&globalOpen===0);
      const second=card('s1');second.dispatchEvent(new KeyboardEvent('keydown',{key:' ',bubbles:true}));check('Space deselects selected card',!ui.getSelection().has('s1'));second.dispatchEvent(new KeyboardEvent('keydown',{key:' ',bubbles:true}));check('Space reselects without opening',ui.getSelection().has('s1')&&dialogs.length===beforeDialogs);
      ui.setSelection(new Set());card('s1').dispatchEvent(new MouseEvent('click',{bubbles:true,ctrlKey:true}));card('s4').dispatchEvent(new MouseEvent('click',{bubbles:true,shiftKey:true}));await tick(470);check('Ctrl and Shift preserve multiple selection without details',ui.getSelection().size===4&&dialogs.length===beforeDialogs);
      card('s6').dispatchEvent(new KeyboardEvent('keydown',{key:' ',bubbles:true,shiftKey:true}));check('Shift Space extends the same selection range without details',ui.getSelection().size===6&&dialogs.length===beforeDialogs);
      card('s2').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));check('Enter opens organization without delegated source open',dialogs.at(-1).dialog.open&&dialogs.length===beforeDialogs+1&&globalOpen===0);closeDialogs();
      ui.setSelection(new Set(['col0']));check('batch collect is additive and disabled for existing collection',command('batch-collect').disabled);const writesBefore=calls.filter(x=>x.method!=='GET').length;command('batch-collect').click();check('batch collection does not remove an existing favorite',calls.filter(x=>x.method!=='GET').length===writesBefore&&collections.length===1);
      const beforePreview=calls.length;command('collect','s3').click();await tick();check('collection first previews complete package without copying',calls.length===beforePreview+1&&calls.at(-1).url==='/api/skill-collections/preview'&&dialogs.at(-1).spec.body.includes('3 个文件'));
      const preview=dialogs.at(-1);preview.form.querySelector('[type=submit]').click();await preview.pending;await tick();check('only confirmed preview token collects package',calls.some(x=>x.url==='/api/skill-collections/collect'&&x.body.token==='preview-s3')&&!!card('col-s3')&&!card('s3'));
      command('bind','col-s3').click();await tick();await tick();check('binding uses the collected package identity and fixed version',calls.some(x=>x.url==='/api/skill-collections/bind'&&x.body.id==='col-s3'&&x.body.project_id==='project-fixture')&&collections.find(x=>x.id==='col-s3').bound_version==='package-version-1');
      command('versions','col0').click();await tick();check('version manager preserves project lock and source refresh entry',dialogs.at(-1).spec.body.includes('fixed-version-1')&&dialogs.at(-1).spec.body.includes('collectNewSkillVersion'));closeDialogs();
      skills[0].editable=true;ui.render(root);const originalEdit=[...card('col0').querySelectorAll('button')].find(x=>x.textContent==='编辑自建原文');check('collected self-created skill retains original editor access',!!originalEdit);originalEdit.click();await tick();check('self-created editor opens source identity while collection remains pinned',opened.at(-1)==='s0'&&collections[0].bound_version==='fixed-version-1'&&globalOpen===0);skills[0].editable=false;ui.render(root);
      viewport.setPointerCapture=()=>{};viewport.hasPointerCapture=()=>false;ui.setSelection(new Set());const marquee=window.YingXuMarquee.install({viewport,getItems:()=>root.querySelectorAll('[data-workflow-card]'),getId:node=>node.dataset.workflowCard,getSelection:ui.getSelection,onChange:ui.setSelection,getContext:ui.selectionContext});
      const target=card('s1');target.scrollIntoView({block:'nearest'});const r=target.getBoundingClientRect(),send=(type,x,y)=>viewport.dispatchEvent(new PointerEvent(type,{bubbles:true,pointerId:7,pointerType:'mouse',button:0,clientX:x,clientY:y}));send('pointerdown',r.left-5,r.top+10);send('pointermove',r.left+30,r.top+40);send('pointerup',r.left+30,r.top+40);await tick(470);check('blank marquee selects preserved card nodes without dialogs',ui.getSelection().has('s1')&&card('s1')===target&&!document.querySelector('dialog[open]'));marquee.destroy();
      command('card-view','list').click();assertBounds();check('list is usable and keeps workflow identities',root.classList.contains('skill-focus-list')&&root.querySelectorAll('[data-workflow-card]').length===16);
      const switchCalls=calls.length;focus=false;ui.render(root);check('original mode restores scope and organization controls without requests',!root.classList.contains('skill-focus-library')&&!!root.querySelector('[data-workflow="view"][data-id="all"]')&&!!root.querySelector('#skillFolder')&&calls.length===switchCalls);focus=true;ui.render(root);
      state.projectId=null;ui.render(root);check('no-project state disables collection binding with reason',command('bind','col0').disabled&&command('bind','col0').title.includes('项目'));ui.setSelection(new Set(['col0']));check('no-project batch binding is disabled',command('batch-bind').disabled);
      check('no hidden scanning or direct file mutations were introduced',calls.every(x=>!x.url.includes('/refresh')&&!x.url.includes('/api/content')&&x.method!=='DELETE')&&errors.length===0);
    }
    document.querySelector('#result').textContent=JSON.stringify({checks,errors,viewportWidth:innerWidth});
  })().catch(error=>{document.querySelector('#result').textContent=JSON.stringify({error:String(error),stack:error.stack});});`;
  new (require('node:vm').Script)(runner);
  fs.writeFileSync(path.join(temporary,'runner.js'),runner);
  fs.writeFileSync(path.join(temporary,'fixture.html'),'<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="styles.css"><link rel="stylesheet" href="appearance.css"><link rel="stylesheet" href="workflow-library.css"><style>body{display:block;margin:0;padding:16px}h1{font-size:24px;margin:0 0 20px}#viewport{overflow:auto;height:760px;padding:0 8px 20px;max-width:1100px}#root{min-height:720px}dialog{max-width:520px;max-height:85vh;overflow:auto}dialog form{display:block}#result{white-space:pre-wrap}</style></head><body><h1>SKILL 库</h1><div id="viewport"><div id="root"><div class="skeleton"></div><div class="skeleton"></div><div class="legacy-placeholder">旧错误占位</div><article class="skill-card" data-workflow-card="obsolete">旧结果</article></div></div><pre id="result"></pre><script src="marquee.js"></script><script src="workflow-library.js"></script><script src="runner.js"></script></body></html>');
  const {stdout,stderr}=await promisify(execFile)(browser,['--headless','--disable-gpu','--no-first-run','--disable-background-networking',`--user-data-dir=${path.join(temporary,'profile')}`,`--window-size=${width},960`,'--virtual-time-budget=10000','--dump-dom',pathToFileURL(path.join(temporary,'fixture.html')).href],{windowsHide:true,timeout:30000,maxBuffer:3*1024*1024});
  const match=stdout.match(/<pre id="result">([\s\S]*?)<\/pre>/);assert.ok(match&&match[1].trim(),stdout.slice(-2500)+'\n'+stderr.slice(-1500));
  const result=JSON.parse(match[1].replace(/&quot;/g,'"').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>'));
  assert.equal(result.error,undefined,JSON.stringify(result));assert.ok(result.checks.length>8);for(const row of result.checks)assert.equal(row.ok,true,row.name);assert.equal(result.errors.length,0);
  if(width<480&&result.viewportWidth>=480){t.skip(`Windows Chromium clamps CLI width to ${result.viewportWidth}px; below-480 verification requires CDP device metrics.`);return;}
  t.diagnostic(result.checks.map(row=>row.name).join('; '));
}
test('focus workflow preserves real API identity and native Chromium interactions',async t=>runFixture(t,1440,true));
test('focus metadata stays within two-column cards at 540px',async t=>runFixture(t,540,false));
test('focus metadata stays within one-column cards below 480px',async t=>runFixture(t,420,false));

test('classic workflow uses the same polished cards and complete interactions',async t=>runFixture(t,1440,true,'classic'));
test('classic metadata stays within two-column cards at 540px',async t=>runFixture(t,540,false,'classic'));
