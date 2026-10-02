'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {test}=require('node:test'),{execFile}=require('node:child_process'),{promisify}=require('node:util'),{pathToFileURL}=require('node:url');

test('actual skill context menus organize external and favorite identities',async()=>{
  const vm=require('node:vm'),calls=[];
  const context=vm.createContext({window:{},document:{querySelector:()=>({value:'',classList:{toggle(){}}})},localStorage:{getItem(){return null;}},setTimeout,clearTimeout,calls});
  const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
  vm.runInContext(source+`;workflowController=()=>({action:async(name,id)=>calls.push([name,id])});report=error=>{throw error;};globalThis.fixture={state,menuCommands,contextMenuTarget,runMenu};`,context);
  const ui=context.fixture;ui.state.bootstrap={capabilities:{skill_organization:true,skill_collections:true}};
  const labels=ui.menuCommands('skill',{editable:false},0,false);
  assert.ok(labels.some(row=>row?.[0]==='organize-skill'&&row[1]==='分类、标签与备注…'));
  assert.ok(labels.some(row=>row?.[0]==='trash-skill'&&row[1]==='隐藏这个 SKILL'));
  const own=ui.menuCommands('workflow-skill',null,0,false);assert.ok(own.some(row=>row?.[0]==='organize-skill'));assert.ok(own.every(row=>!row?.[0]?.startsWith('trash-')));
  const favorite={getAttribute:key=>key==='data-workflow-collection'?'favorite-a':null,closest:key=>key==='[data-workflow-collection]'?favorite:null};
  const target=ui.contextMenuTarget(favorite);assert.equal(target.kind,'workflow-skill');assert.equal(target.id,'favorite-a');
  await ui.runMenu('organize-skill',{id:'skill-b'});await ui.runMenu('organize-skill',target);await ui.runMenu('open-workflow-skill',target);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)),[['organize','skill-b'],['organize','favorite-a'],['open','favorite-a']]);
  ui.state.bootstrap.capabilities.skill_organization=false;
  assert.ok(ui.menuCommands('skill',{editable:false},0,false).every(row=>row?.[0]!=='organize-skill'));
});

test('real Edge SKILL organization forms, filters, deduplication and selection',async t=>{
  const browser=[process.env.YINGXU_TEST_BROWSER,'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(value=>value&&fs.existsSync(value));
  if(!browser){t.skip('Existing Chromium required; no downloads');return;}
  const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'yingxu-skill-organization-'));
  t.after(()=>{const resolved=path.resolve(temporary);assert.ok(resolved.startsWith(path.resolve(os.tmpdir())+path.sep)&&path.basename(resolved).startsWith('yingxu-skill-organization-'));fs.rmSync(resolved,{recursive:true,force:true,maxRetries:10,retryDelay:100});});
  const front=path.join(__dirname,'../frontend');
  for(const file of ['workflow-library.js','workflow-library.css','styles.css','appearance.css','marquee.js'])fs.copyFileSync(path.join(front,file),path.join(temporary,file));
  const escapeSource=fs.readFileSync(path.join(front,'app.js'),'utf8').match(/^const escapeHtml = .*;$/m);
  assert.ok(escapeSource,'Use the application HTML escaper in the browser fixture.');
  const runner=escapeSource[0]+`\n(async()=>{
    const checks=[],check=(name,ok)=>{checks.push({name,ok:!!ok});if(!ok)throw Error(name);},tick=()=>new Promise(resolve=>setTimeout(resolve,0));
    const nativeFormData=window.FormData,formDataEntries=[];
    window.FormData=function(form){const result=new nativeFormData(form);formDataEntries.push([...result.entries()].map(([key,value])=>[key,String(value)]));return result;};
    window.FormData.prototype=nativeFormData.prototype;
    const sourceBodies={
      'skill-a':'immutable source body A',
      'skill-b':'immutable source body B',
      'skill-c':'immutable source body C'
    };
    const originalBodies=JSON.stringify(sourceBodies),allSkills=[
      {id:'skill-a',name:'收藏源技能',description:'已收藏来源',path:'C:/synthetic/skills/a/SKILL.md',source:'codex',source_id:'codex',source_group:'codex',source_label:'Codex',editable:false,bound:false},
      {id:'skill-b',name:'<img src=x onerror=window.injected=true> External B',description:'未收藏的来源技能',path:'C:/synthetic/skills/b/SKILL.md',source:'codex',source_id:'codex',source_group:'codex',source_label:'Codex',editable:false,bound:false},
      {id:'skill-c',name:'External C',description:'另一个来源技能',path:'C:/synthetic/skills/c/SKILL.md',source:'dsh',source_id:'dsh',source_group:'dsh',source_label:'DSH',editable:false,bound:false}
    ];
    const collectionRows=[{id:'favorite-a',skill_id:'skill-a',name:'收藏版 A',description:'固定版本',category:'visual',version:'v1',bound:true,source_id:'codex',origin:{source_id:'codex'}}];
    const metadata=new Map([
      ['skill-a',{skill_id:'skill-a',folder_id:'',tags:['reused-tag'],notes:'reused note'}],
      ['skill-b',{skill_id:'skill-b',folder_id:'',tags:['review,formal'],notes:'original note B'}],
      ['skill-c',{skill_id:'skill-c',folder_id:'',tags:['base-c'],notes:'original note C'}]
    ]);
    let skills=allSkills.map(value=>({...value})),folders=[],folderSerial=1,sourceFilter='',sourceWrites=0;
    const calls=[],dialogs=[],formSubmissions=[],errors=[],toasts=[];let paginationCount=0,ui,root=document.querySelector('#root');
    const clone=value=>JSON.parse(JSON.stringify(value));
    const folderPath=id=>{const row=folders.find(value=>value.id===id);if(!row)return '';return row.parent_id?folderPath(row.parent_id)+'/'+row.name:row.name;};
    const folderView=id=>{const row=folders.find(value=>value.id===id);if(!row)return null;return {...row,path:folderPath(id),count:[...metadata.values()].filter(value=>value.folder_id===id).length};};
    const allFolderViews=()=>folders.map(value=>folderView(value.id));
    const allMetadata=()=>[...metadata.values()].map(value=>clone(value));
    const sourceRows=()=>sourceFilter?allSkills.filter(value=>value.source_id===sourceFilter):allSkills;
    const api=async(path,options={})=>{
      const method=options.method||'GET',body=options.body||{};calls.push({path,method,body:clone(body)});
      if(method==='GET'&&path.startsWith('/api/skill-collections?'))return {collections:clone(collectionRows),categories:[]};
      if(method==='GET'&&path==='/api/skill-organization')return {folders:allFolderViews(),metadata:allMetadata()};
      if(path==='/api/skill-folders'&&method==='POST'){
        const id='fld_'+String(folderSerial++).padStart(32,'0'),row={id,name:body.name,parent_id:body.parent_id||''};folders.push(row);return folderView(id);
      }
      const folderPrefix='/api/skill-folders/',folderId=path.startsWith(folderPrefix)?decodeURIComponent(path.slice(folderPrefix.length)):'';
      if(folderId&&method==='PATCH'){
        const row=folders.find(value=>value.id===folderId);if(!row)throw Error('unknown synthetic folder');
        if(Object.hasOwn(body,'name'))row.name=body.name;if(Object.hasOwn(body,'parent_id'))row.parent_id=body.parent_id||'';return folderView(row.id);
      }
      if(folderId&&method==='DELETE'){
        const id=folderId;if([...metadata.values()].some(value=>value.folder_id===id)||folders.some(value=>value.parent_id===id))throw Error('synthetic folder must be empty');
        folders=folders.filter(value=>value.id!==id);return {id};
      }
      if(path==='/api/skill-metadata'&&method==='POST'){
        for(const id of body.skill_ids){const row=metadata.get(id)||{skill_id:id,folder_id:'',tags:[],notes:''};
          if(Object.hasOwn(body,'folder_id'))row.folder_id=body.folder_id;
          if(Object.hasOwn(body,'tags'))row.tags=body.tags_mode==='append'?[...new Set([...row.tags,...body.tags])]:[...body.tags];
          if(Object.hasOwn(body,'notes'))row.notes=body.notes;
          metadata.set(id,row);
        }
        return {updated:body.skill_ids.length};
      }
      if(path==='/api/skills'||path.startsWith('/api/skills/'))sourceWrites++;
      throw Error('Unexpected synthetic API request: '+method+' '+path);
    };
    const addDialog=spec=>{
      const dialog=document.createElement('dialog'),form=document.createElement('form');dialog.className='fixture-dialog';
      form.innerHTML='<h2>'+escapeHtml(spec.title||'')+'</h2><div class="field-hint">'+escapeHtml(spec.subtitle||'')+'</div>'+spec.body+'<div class="settings-buttons"><button type="submit" class="button button-primary">'+escapeHtml(spec.submit||'完成')+'</button><button type="button" class="button button-ghost" data-dialog-cancel>取消</button></div>';
      dialog.append(form);document.body.append(dialog);const entry={dialog,form,spec,promise:Promise.resolve()};dialogs.push(entry);
      form.addEventListener('submit',event=>{event.preventDefault();formSubmissions.push([...new nativeFormData(form).entries()].map(([key,value])=>[key,String(value)]));entry.promise=Promise.resolve(spec.onSubmit?.(form)).then(()=>{dialog.close();dialog.remove();}).catch(error=>{errors.push(String(error));throw error;});});
      form.querySelector('[data-dialog-cancel]').onclick=()=>{dialog.close();dialog.remove();};dialog.showModal();return dialog;
    };
    const sourceMarkup='<label>来源<select id="skillSourceDirectory"><option value="">全部来源</option><option value="codex">Codex</option><option value="dsh">DSH</option></select></label>';
    const env={state:{projectId:'synthetic-project',bootstrap:{capabilities:{skill_organization:true}},skills,q:'',offset:0,limit:48,selectedIds:new Set(),tabs:[]},api,escapeHtml,icon:()=>'<svg aria-hidden="true"></svg>',showDialog:addDialog,toast:value=>toasts.push(value),report:error=>errors.push(String(error)),copyText:async()=>{},formatSize:value=>String(value),sourcesHtml:()=>sourceMarkup,
      hasSourceFilter:()=>!!sourceFilter,sourceSummary:()=>sourceFilter||'全部来源',matchesCollectionSource:value=>!sourceFilter||(value.origin?.source_id||value.source_id)===sourceFilter,
      setSource:async value=>{sourceFilter=value;skills=sourceRows().map(item=>({...item}));env.state.skills=skills;await ui.load();ui.render(root);},resetFilters:async()=>{sourceFilter='';skills=allSkills.map(item=>({...item}));env.state.skills=skills;await ui.load();},pagination:count=>{paginationCount=count;},openSkill:async()=>{},reloadSkills:async()=>{},render:()=>ui.render(root)};
    ui=window.YingXuWorkflow.create(env);await ui.load();ui.render(root);
    const card=id=>root.querySelector('[data-workflow-card="'+id+'"]'),clickWorkflow=(action,id)=>root.querySelector('[data-workflow="'+action+'"][data-id="'+id+'"]')?.click();
    const openDialog=async()=>{for(let i=0;i<20;i++){const entry=[...dialogs].reverse().find(value=>value.dialog.open);if(entry)return entry;await tick();}throw Error('dialog did not open');};
    const submitDialog=async(values={})=>{const entry=await openDialog();for(const [name,value] of Object.entries(values)){const control=entry.form.elements.namedItem(name);if(!control)throw Error('missing dialog field '+name);if(control.type==='checkbox')control.checked=!!value;else control.value=String(value);control.dispatchEvent(new Event('input',{bubbles:true}));}
      entry.form.querySelector('[type="submit"]').click();await entry.promise;await tick();return entry;};
    const selectFolder=value=>{const select=root.querySelector('#skillFolder');select.value=value;select.dispatchEvent(new Event('change',{bubbles:true}));};
    const selectTag=value=>{const select=root.querySelector('#skillTag');select.value=value;select.dispatchEvent(new Event('change',{bubbles:true}));};
    const latestCall=path=>calls.filter(value=>value.path===path).at(-1);
    check('load uses skill collections and skill organization contracts',calls.some(value=>value.method==='GET'&&value.path.startsWith('/api/skill-collections?'))&&calls.some(value=>value.path==='/api/skill-organization'));
    check('an external uncollected skill has a rendered organize entry',!!card('skill-b')&&!!card('skill-b').querySelector('[data-workflow="organize"]'));
    check('visible organization introduction names categories tags and notes',root.querySelector('.skill-organization-intro')?.textContent.includes('分类、标签与备注')&&root.querySelector('.skill-organization-intro')?.textContent.includes('个人文件夹'));
    const organize=card('skill-b').querySelector('[data-workflow="organize"]');
    check('compact organize entry exposes every editable metadata field',organize?.textContent==='整理'&&organize.title==='分类 / 标签 / 备注');
    check('personal category controls stay separate from purpose and source',root.querySelector('#skillFolder')?.parentElement.textContent.includes('分类（文件夹）')&&!!root.querySelector('#skillPurpose')&&!!root.querySelector('#skillSourceDirectory'));
    check('reading the library never rewrites existing metadata',!calls.some(value=>value.method!=='GET'));
    check('existing favorite deduplicates its external source item',root.querySelectorAll('[data-workflow-card]').length===3&&!card('skill-a')&&!!card('favorite-a'));
    check('favorite reuses stored tags and notes metadata',card('favorite-a').querySelector('.skill-card-tags')?.textContent.includes('reused-tag')&&card('favorite-a').querySelector('.skill-card-notes')?.textContent.includes('reused note'));
    check('favorite exposes a distinct context-menu identity',card('favorite-a').dataset.workflowCollection==='favorite-a');
    check('injected skill name is rendered as escaped text',!root.querySelector('img[onerror]')&&card('skill-b').textContent.includes('<img src=x onerror=window.injected=true>'));
    clickWorkflow('new-folder','');await openDialog();await submitDialog({name:'Studio',parent:''});
    const studio=folders.find(value=>value.name==='Studio');check('root folder creation uses POST skill-folders',!!studio&&latestCall('/api/skill-folders')?.method==='POST');
    clickWorkflow('new-folder','');await openDialog();await submitDialog({name:'Boards'});
    const boards=folders.find(value=>value.name==='Boards');check('nested folder creation uses the selected parent',!!boards&&boards.parent_id===studio.id&&latestCall('/api/skill-folders')?.body.parent_id===studio.id);
    check('folder path shows the hierarchy after creation',root.querySelector('.skill-folder-path')?.textContent.includes('Studio/Boards'));
    selectFolder(studio.id);await tick();check('folder filter selects parent and renders child tile',root.querySelector('#skillFolder').value===studio.id&&!!root.querySelector('[data-workflow="folder"][data-id="'+boards.id+'"]'));
    clickWorkflow('folder',boards.id);await tick();check('folder tile opens its child folder',root.querySelector('#skillFolder').value===boards.id&&root.querySelector('.skill-folder-path')?.textContent.includes('Studio/Boards'));
    clickWorkflow('edit-folder',boards.id);await openDialog();await submitDialog({name:'Boards revised'});
    check('folder edit uses PATCH and refreshes its path',latestCall('/api/skill-folders/'+boards.id)?.method==='PATCH'&&root.querySelector('.skill-folder-path')?.textContent.includes('Studio/Boards revised'));
    selectFolder('*');await tick();clickWorkflow('new-folder','');await openDialog();await submitDialog({name:'Empty scratch',parent:''});
    const scratch=folders.find(value=>value.name==='Empty scratch');clickWorkflow('edit-folder',scratch.id);await openDialog();
    const deleteDetails=document.querySelector('dialog[open] .skill-folder-delete');deleteDetails.open=true;
    await submitDialog({name:'Empty scratch',parent:'',delete:true});
    check('confirmed empty-folder delete uses DELETE and removes the folder',!folders.some(value=>value.id===scratch.id)&&latestCall('/api/skill-folders/'+scratch.id)?.method==='DELETE');
    selectFolder('*');await tick();clickWorkflow('organize','skill-b');await openDialog();
    const notesOnlyForm=document.querySelector('dialog[open] form'),initialTags=notesOnlyForm.elements.namedItem('tags');
    check('existing comma-containing tag opens as one textarea line',initialTags?.tagName==='TEXTAREA'&&initialTags.value==='review,formal');
    await submitDialog({notes:'needle-only <svg onload=window.injected=true>'});
    const notesOnlyCall=latestCall('/api/skill-metadata');
    check('notes-only save preserves the identity of a comma-containing tag',!Object.hasOwn(notesOnlyCall.body,'tags')&&metadata.get('skill-b').tags.length===1&&metadata.get('skill-b').tags[0]==='review,formal'&&metadata.get('skill-b').notes.includes('needle-only'));
    clickWorkflow('organize','skill-b');await openDialog();
    const singleForm=document.querySelector('dialog[open] form');
    await submitDialog({folder:studio.id,tags:'review,formal\\nsingle-tag',notes:'needle-only <svg onload=window.injected=true>'});
    const singlePayload=latestCall('/api/skill-metadata')?.body;
    check('single external skill saves folder, newline-delimited replacement tags and notes through real FormData',singlePayload?.skill_ids?.join(',')==='skill-b'&&singlePayload.folder_id===studio.id&&singlePayload.tags_mode==='replace'&&JSON.stringify(singlePayload.tags)===JSON.stringify(['review,formal','single-tag'])&&singlePayload.notes?.includes('needle-only'));
    await ui.load();ui.render(root);
    check('metadata reload retains category tags and notes through the read API',card('skill-b').querySelector('.skill-card-folder')?.textContent.includes('Studio')&&card('skill-b').querySelector('.skill-card-tags')?.textContent.includes('single-tag')&&card('skill-b').querySelector('.skill-card-notes')?.textContent.includes('needle-only'));
    check('real dialog form submission constructed native FormData',formSubmissions.length>=4&&formDataEntries.length>=2&&singleForm?.elements.namedItem('notes')?.value.includes('needle-only'));
    check('metadata save never writes the external skill file',sourceWrites===0&&JSON.stringify(sourceBodies)===originalBodies);
    env.state.q='needle-only';ui.render(root);check('keyword search includes saved notes',root.querySelectorAll('[data-workflow-card]').length===1&&!!card('skill-b'));
    env.state.q='not-present';ui.render(root);check('an empty search result offers a return-to-all action',root.textContent.includes('没有符合筛选条件')&&!!root.querySelector('[data-workflow="browse-all"]'));
    clickWorkflow('browse-all','');await tick();check('browse-all clears the empty filter and restores every unique item',env.state.q===''&&root.querySelectorAll('[data-workflow-card]').length===3);
    clickWorkflow('organize','skill-c');await openDialog();await submitDialog({folder:boards.id,tags:'base-c',notes:'original note C'});
    for(const id of ['skill-b','skill-c']){const box=root.querySelector('[data-workflow-select="'+id+'"]');box.checked=true;box.dispatchEvent(new Event('change',{bubbles:true}));}
    check('real rendered checkboxes enable batch organization',!root.querySelector('[data-workflow="batch-organize"]').parentElement.hidden);
    clickWorkflow('batch-organize','');const batchEntry=await openDialog();
    check('batch dialog omits notes so it cannot overwrite per-skill notes',!batchEntry.form.elements.namedItem('notes')&&!!batchEntry.form.elements.namedItem('replace'));
    await submitDialog({folder:'*',tags:'batch-added'});
    const batchPayload=latestCall('/api/skill-metadata')?.body,afterB=metadata.get('skill-b'),afterC=metadata.get('skill-c');
    check('batch folder keep and tag append omit folder and notes writes',batchPayload?.skill_ids?.slice().sort().join(',')==='skill-b,skill-c'&&!Object.hasOwn(batchPayload,'folder_id')&&!Object.hasOwn(batchPayload,'notes')&&batchPayload.tags_mode==='append'&&batchPayload.tags?.join(',')==='batch-added');
    check('batch append preserves each folder, existing tags and notes',afterB.folder_id===studio.id&&afterC.folder_id===boards.id&&afterB.tags.includes('review,formal')&&afterB.tags.includes('single-tag')&&afterB.tags.includes('batch-added')&&afterC.tags.includes('base-c')&&afterC.tags.includes('batch-added')&&afterB.notes.includes('needle-only')&&afterC.notes==='original note C');
    selectFolder(studio.id);await tick();check('folder filter returns only the skill in that folder',root.querySelectorAll('[data-workflow-card]').length===1&&!!card('skill-b'));
    selectFolder('*');await tick();selectTag('batch-added');check('tag filter finds both appended-tag skills',root.querySelectorAll('[data-workflow-card]').length===2);
    selectTag('single-tag');check('tag filter can narrow to one skill',root.querySelectorAll('[data-workflow-card]').length===1&&!!card('skill-b'));
    selectFolder('*');await tick();selectTag('');
    const sourceSelect=root.querySelector('#skillSourceDirectory');sourceSelect.value='dsh';sourceSelect.dispatchEvent(new Event('change',{bubbles:true}));await tick();await tick();
    check('source filter applies to external skills and excludes unrelated favorites',root.querySelectorAll('[data-workflow-card]').length===1&&!!card('skill-c'));
    root.querySelector('#skillSourceDirectory').value='codex';root.querySelector('#skillSourceDirectory').dispatchEvent(new Event('change',{bubbles:true}));await tick();await tick();
    check('source filter includes matching favorite and keeps its duplicate hidden',root.querySelectorAll('[data-workflow-card]').length===2&&!card('skill-a')&&!!card('favorite-a')&&!!card('skill-b'));
    root.querySelector('#skillSourceDirectory').value='';root.querySelector('#skillSourceDirectory').dispatchEvent(new Event('change',{bubbles:true}));await tick();await tick();
    check('clearing source filter restores all unique source and favorite items',root.querySelectorAll('[data-workflow-card]').length===3);
    env.state.q='missing-keyword';ui.render(root);clickWorkflow('browse-all','');await tick();
    check('empty-search reset returns to all items without a scan',root.querySelectorAll('[data-workflow-card]').length===3&&!calls.some(value=>value.path==='/api/skills/refresh'));
    const viewport=document.querySelector('#viewport');viewport.setPointerCapture=()=>{};viewport.hasPointerCapture=()=>false;
    const marquee=window.YingXuMarquee.install({viewport,getItems:()=>root.querySelectorAll('[data-workflow-card]'),getId:node=>node.dataset.workflowCard,getSelection:ui.getSelection,onChange:ui.setSelection,getContext:ui.selectionContext});
    const target=card('skill-b');target.scrollIntoView({block:'nearest'});const rect=target.getBoundingClientRect(),send=(type,x,y,extra={})=>viewport.dispatchEvent(new PointerEvent(type,{bubbles:true,pointerId:7,pointerType:'mouse',button:0,clientX:x,clientY:y,...extra}));
    send('pointerdown',rect.left-8,rect.top+8);send('pointermove',rect.left+rect.width/2,rect.top+rect.height/2);send('pointerup',rect.left+rect.width/2,rect.top+rect.height/2);
    check('blank-space marquee selects the actual rendered skill card',ui.getSelection().has('skill-b')&&target.classList.contains('checked'));
    marquee.destroy();check('all organization mutations leave every synthetic source body unchanged',sourceWrites===0&&JSON.stringify(sourceBodies)===originalBodies&&errors.length===0);
    document.querySelector('#result').textContent=JSON.stringify({checks,errors,toasts,paginationCount});
  })().catch(error=>{document.querySelector('#result').textContent=JSON.stringify({error:String(error),stack:error.stack});});`;
  fs.writeFileSync(path.join(temporary,'runner.js'),runner);
  fs.writeFileSync(path.join(temporary,'fixture.html'),'<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="styles.css"><link rel="stylesheet" href="appearance.css"><link rel="stylesheet" href="workflow-library.css"><style>body{margin:0;padding:20px}#viewport{width:1000px;height:720px;overflow:auto;border:1px solid #aaa;padding:16px}#root{min-height:680px}.fixture-dialog{max-width:540px}</style></head><body><div id="viewport"><div id="root"></div></div><pre id="result"></pre><script src="marquee.js"></script><script src="workflow-library.js"></script><script src="runner.js"></script></body></html>');
  const {stdout,stderr}=await promisify(execFile)(browser,['--headless','--disable-gpu','--no-first-run','--disable-background-networking','--enable-logging=stderr',`--user-data-dir=${path.join(temporary,'profile')}`,'--window-size=1180,940','--virtual-time-budget=10000','--dump-dom',pathToFileURL(path.join(temporary,'fixture.html')).href],{windowsHide:true,timeout:30000,maxBuffer:3*1024*1024});
  const match=stdout.match(/<pre id="result">([\s\S]*?)<\/pre>/);assert.ok(match&&match[1].trim(),stdout.slice(-3000)+'\n'+stderr.slice(-3000));
  const result=JSON.parse(match[1].replace(/&quot;/g,'"').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>'));
  assert.equal(result.error,undefined,JSON.stringify(result));assert.ok(Array.isArray(result.checks),JSON.stringify(result));assert.ok(result.checks.length>=24,JSON.stringify(result.checks));
  for(const row of result.checks)assert.equal(row.ok,true,row.name);
  t.diagnostic(result.checks.map(row=>row.name).join('; '));
});
