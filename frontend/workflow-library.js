'use strict';
// UI adapters use the existing authenticated API and dialog/draft lifecycle.
window.YingXuWorkflow = (() => {
  const categories = [['','全部用途'],['planning','策划与文本'],['visual','视觉与分镜'],['video','视频与预演'],['audio','声音与配音'],['development','开发与工具'],['delivery','整理与交付']];
  let view = 'all', category = '', folder = '*', tag = '', selected = new Set(), collections = [], revision = 0;
  let handoffDraft = {client_id:'codex',conversation_id:'',task:''}, handoffProject = null;
  function create(env) {
    const {api,escapeHtml:e,icon,state,showDialog,toast,report,copyText} = env;
    const scopes = [['all','全部技能'],['favorites','我的收藏'],['project','本项目已绑定']];
    let mountedRoot = null, selectionAnchor = null, sourcesExpanded = false, focusView = 'grid', queryComposing = false;
    let loadGeneration = 0;
    let folders = [], metadata = new Map();
    const organizationEnabled = () => !state.bootstrap || !!state.bootstrap.capabilities?.skill_organization;
    const focused = () => !!env.focusLayout?.();
    const button = (action,label,id='') => `<button type="button" class="button button-ghost button-small" data-workflow="${action}" data-id="${e(id)}">${label}</button>`;
    async function load() {
      const generation = ++loadGeneration, projectId = state.projectId || '';
      const [result, organization] = await Promise.all([
        api(`/api/skill-collections?project=${encodeURIComponent(projectId)}`),
        organizationEnabled() ? api('/api/skill-organization') : Promise.resolve({folders:[],metadata:[]})
      ]);
      if(generation !== loadGeneration || projectId !== (state.projectId || ''))return;
      collections = result.collections || [];
      folders = organization.folders || [];
      metadata = new Map((organization.metadata || []).map(x=>[x.skill_id,x]));
      if(folder !== '*' && folder !== '' && !folders.some(x=>x.id===folder))folder='*';
      if(tag && ![...state.skills,...collections].some(x=>(metadata.get(x.skill_id || x.id)?.tags || x.tags || []).some(value=>value.toLocaleLowerCase()===tag.toLocaleLowerCase())))tag='';
      for(const c of result.categories || [])if(!categories.some(x=>x[0]===c.id))categories.push([c.id,c.label]);
      env.onNavigationChange?.(navigation());
    }
    function items(scope = view, filtered = true) {
      const organized = x => ({...x,...(metadata.get(x.skill_id || x.id) || {}),id:x.id,tags:metadata.get(x.skill_id || x.id)?.tags || x.tags || []});
      const external = state.skills.map(x=>organized({...x,collection:false}));
      const own = collections.filter(x=>!env.matchesCollectionSource || env.matchesCollectionSource(x)).map(x=>organized({...x,collection:true}));
      const collected = new Set(own.filter(x=>scope !== 'project' || x.bound).map(x=>x.skill_id));
      let result = scope === 'favorites' ? own : scope === 'project' ? [...own.filter(x=>x.bound),...external.filter(x=>x.bound && !collected.has(x.id))] : [...own,...external.filter(x=>!collected.has(x.id))];
      const query = state.q.trim().toLocaleLowerCase();
      return filtered ? result.filter(x=>(!category || x.category===category) && (folder==='*' || (x.folder_id || '')===folder) && (!tag || (x.tags||[]).some(t=>t.toLocaleLowerCase()===tag.toLocaleLowerCase())) && (!query || query.split(/\s+/).every(part=>`${x.name} ${x.description} ${x.path || ''} ${(x.tags||[]).join(' ')} ${x.notes || ''}`.toLocaleLowerCase().includes(part)))) : result;
    }
    function navigation() {
      const rows=items(view,false),all=items('all',false);
      const current={view,folder,tag,category};
      const folderCounts=new Map(),tagCounts=new Map(),allTags=new Set();
      for(const row of rows){const id=row.folder_id || '';folderCounts.set(id,(folderCounts.get(id)||0)+1);for(const value of new Set(row.tags || []))tagCounts.set(value,(tagCounts.get(value)||0)+1);}
      for(const row of all)for(const value of row.tags || [])allTags.add(value);
      return {scopes:scopes.map(([id,label])=>({id,label,count:items(id,false).length})),selected:{...current},current:{...current},organizationEnabled:organizationEnabled(),
        folders:folders.map(x=>({...x,label:x.path || x.name,count:folderCounts.get(x.id)||0})),
        unfiledCount:folderCounts.get('')||0,
        tags:[...allTags].sort((a,b)=>a.localeCompare(b)).map(id=>({id,label:id,count:tagCounts.get(id)||0}))};
    }
    function navigate(next={}) {
      
      if(Object.hasOwn(next,'view')&&scopes.some(([key])=>key===next.view))view=next.view;
      if(Object.hasOwn(next,'folder')&&(next.folder==='*'||next.folder===''||folders.some(x=>x.id===next.folder)))folder=next.folder;
      if(Object.hasOwn(next,'tag'))tag=String(next.tag || '');
      selected.clear();selectionAnchor=null;state.offset=0;env.render();
    }
    function folderOptions(current, all=false) {
      return `${all?`<option value="*" ${current==='*'?'selected':''}>全部分类</option>`:''}<option value="" ${current===''?'selected':''}>未归类</option>`+folders.map(x=>`<option value="${e(x.id)}" ${current===x.id?'selected':''}>${e(x.path || x.name)}</option>`).join('');
    }
    function organizationHtml(controls=true) {
      if(!organizationEnabled())return '';
      const tags = [...new Set([...state.skills,...collections].flatMap(x=>metadata.get(x.skill_id || x.id)?.tags || x.tags || []))].sort((a,b)=>a.localeCompare(b));
      const children = folder===''?[]:folders.filter(x=>(x.parent_id || '')===(folder==='*'?'':folder));
      const current = folders.find(x=>x.id===folder);
      return `<div class="skill-organization-intro"><strong>分类、标签与备注</strong><span>用个人文件夹分类；点卡片上的“整理”编辑，或右键整理。用途与来源可独立筛选。</span></div><div class="skill-organization-tools" aria-label="SKILL 分类文件夹和标签">${controls?`<label>分类（文件夹）<select id="skillFolder">${folderOptions(folder,true)}</select></label><label>标签<select id="skillTag"><option value="">全部标签</option>${tags.map(x=>`<option value="${e(x)}" ${x===tag?'selected':''}>${e(x)}</option>`).join('')}</select></label>`:''}${button('new-folder',icon('folder')+'新建分类文件夹')}${current?button('edit-folder','编辑文件夹',current.id):''}</div>`+
        (children.length || current ? `<nav class="skill-folder-path" aria-label="技能文件夹">${current?button('folder','返回上级',current.parent_id || '*')+`<span>${e(current.path || current.name)}</span>`:''}</nav><div class="skill-folder-tiles">${children.map(x=>`<button type="button" class="skill-folder-tile" data-workflow="folder" data-id="${e(x.id)}">${icon('folder')}<span>${e(x.name)}</span><small>${Number(x.count)||0}</small></button>`).join('')}</div>`:'');
    }
    function syncQuery() {
      const input=mountedRoot?.querySelector('[data-workflow-query]');
      if(!input || queryComposing)return;
      if(input.value!==state.q)input.value=state.q;
      const clear=mountedRoot.querySelector('[data-workflow-query-clear]');
      if(clear)clear.hidden=!input.value;
    }
    function setQuery(value, immediate=false) {
      if(env.setQuery)return env.setQuery(value,{immediate});
      state.q=String(value);state.offset=0;syncQuery();env.render();
    }
    function mountSearch(root) {
      let search=root.querySelector('[data-workflow-search]');
      if(search)return search;
      queryComposing=false;
      search=document.createElement('div');search.className='skill-library-search';search.dataset.workflowSearch='';
      search.innerHTML=`<label for="skillLibrarySearch">搜索 SKILL</label><div class="skill-library-search-field">${icon('search')}<input id="skillLibrarySearch" type="search" data-workflow-query placeholder="名称、用途、标签或备注…" autocomplete="off" spellcheck="false"><button type="button" class="icon-button" data-workflow-query-clear aria-label="清除 SKILL 搜索" title="清除搜索" hidden>${icon('close')}</button></div>`;
      const input=search.querySelector('[data-workflow-query]'),clear=search.querySelector('[data-workflow-query-clear]');
      input.addEventListener('compositionstart',()=>{queryComposing=true;env.cancelQuery?.();});
      input.addEventListener('compositionend',()=>{queryComposing=false;setQuery(input.value);});
      input.addEventListener('input',event=>{clear.hidden=!input.value;if(!queryComposing&&!event.isComposing)setQuery(input.value);});
      clear.onclick=()=>{queryComposing=false;input.value='';setQuery('',true);syncQuery();input.focus();};
      root.prepend(search);
      return search;
    }
    function render(root) {
      mountedRoot = root;
      const rows=items(), ids=new Set(rows.map(x=>x.id)); selected=new Set([...selected].filter(id=>ids.has(id)));
      state.offset=Math.min(state.offset,Math.max(0,Math.ceil(rows.length/state.limit)-1)*state.limit);
      const focus=focused();
      root.className='resource-grid skill-grid skill-library-cards skill-polished-library'+(focus?' skill-focus-library':'')+(focusView==='list'?' skill-focus-list':'');
      // Keep this input connected across card/filter/page renders, including IME composition.
      const search=mountSearch(root);syncQuery();
      let body=root.querySelector('[data-workflow-body]');
      if(!body){body=document.createElement('div');body.dataset.workflowBody='';root.append(body);}
      // Replace loader skeletons or old result placeholders without detaching the live query.
      for(const child of [...root.childNodes])if(child!==search && child!==body)child.remove();
      const empty = state.q || category || tag || env.hasSourceFilter?.() ? '没有符合筛选条件的技能。' : folder!=='*' ? '这个文件夹还没有技能。可在全部技能中框选，再点“分类 / 标签（所选）”。' : view==='project' ? '当前项目尚未绑定技能，已扫描的技能仍在全部技能中。' : view==='favorites' ? '还没有收藏技能，可以从全部技能中选择。' : '尚未发现技能，请扫描本机 SKILL 或登记扫描位置。';
      const purposes=`<select id="skillPurpose" aria-label="按用途筛选">${categories.map(([id,label])=>`<option value="${id}" ${id===category?'selected':''}>${label}</option>`).join('')}</select>`;
      const sources=`<details class="skill-library-sources" ${sourcesExpanded?'open':''}><summary>来源 · ${e(env.sourceSummary?.() || '全部来源')}</summary><div class="skill-focus-source-panel">${env.sourcesHtml()}</div></details>`;
      const current=folders.find(x=>x.id===folder);
      const organization=organizationEnabled()?`<details class="skill-focus-organization"><summary>个人分类管理</summary>${organizationHtml(focus)}</details>`:'';
      const more=`<details class="skill-library-more"><summary aria-label="技能库管理">${icon('more')}<span>管理</span></summary><div><button type="button" class="button button-ghost button-small" data-action="new-skill">${icon('plus')}创建 SKILL</button>${button('builtin','内置规范')}${button('shared-library','曜核 / 客户端接入')}${organization}</div></details>`;
      const chips=`${folder!=='*'?button('folder',icon('close')+e(folder===''?'未归类':current?.path || '个人分类'),'*'):''}${tag?button('tag',icon('close')+e(tag),''):''}${category||folder!=='*'||tag||state.q||env.hasSourceFilter?.()?button('browse-all','清除筛选'):''}`;
      const classicFilters=focus?'':`<div class="skill-library-inline-navigation" aria-label="技能范围与个人分类"><div class="skill-library-scopes" role="group" aria-label="SKILL 范围">${scopes.map(([key,label])=>`<button type="button" class="button button-secondary button-small ${view===key?'active':''}" data-workflow="view" data-id="${key}" aria-pressed="${view===key}">${label} <small>${items(key,false).length}</small></button>`).join('')}</div>${organizationEnabled()?`<label>分类（文件夹）<select id="skillFolder">${folderOptions(folder,true)}</select></label><label>标签<select id="skillTag"><option value="">全部标签</option>${navigation().tags.map(x=>`<option value="${e(x.id)}" ${x.id===tag?'selected':''}>${e(x.label)}</option>`).join('')}</select></label>`:''}</div>`;
      body.innerHTML=classicFilters+`<div class="skill-library-tools" role="group" aria-label="技能库工具"><label class="skill-focus-select-page"><input type="checkbox" data-workflow-select-page aria-label="选择本页全部技能">全选</label>${purposes}${sources}<span class="skill-focus-filter-chips">${chips}</span><div class="skill-focus-view" role="group" aria-label="技能显示方式">${button('card-view',icon('grid'),'grid')}${button('card-view',icon('list'),'list')}</div>${more}</div><div class="skill-library-selection"><span data-workflow-count></span><span data-workflow-batch hidden>${button('batch-collect','收藏所选')}${organizationEnabled()?button('batch-organize','分类 / 标签（所选）'):button('batch-category','分类所选')}${button('batch-bind','绑定所选')}${button('clear-selection','取消选择')}</span></div>`+
        (rows.length?rows.slice(state.offset,state.offset+state.limit).map(focusCard).join(''):`<div class="skill-library-empty"><h3>${state.q||category||tag||env.hasSourceFilter?.()?'没有匹配的技能':'这里暂时没有技能'}</h3><p>${empty}</p>${button('browse-all','查看全部技能')}</div>`);
      root.querySelector('#skillPurpose').onchange=event=>{category=event.target.value;selected.clear();state.offset=0;render(root);};
      root.querySelector('#skillFolder')?.addEventListener('change',event=>action('folder',event.target.value).catch(report));
      root.querySelector('#skillTag')?.addEventListener('change',event=>navigate({tag:event.target.value}));
      root.querySelectorAll('[data-workflow-select]').forEach(node=>{node.onclick=event=>event.stopPropagation();node.onchange=()=>{selectionAnchor=node.dataset.workflowSelect;node.checked?selected.add(node.dataset.workflowSelect):selected.delete(node.dataset.workflowSelect);updateSelection();};});
      const selectPage=root.querySelector('[data-workflow-select-page]');if(selectPage)selectPage.onchange=()=>{for(const row of rows.slice(state.offset,state.offset+state.limit))selectPage.checked?selected.add(row.id):selected.delete(row.id);updateSelection();};
      root.querySelectorAll('[data-workflow]').forEach(node=>node.onclick=event=>{event.stopPropagation(); action(node.dataset.workflow,node.dataset.id).catch(report);});
      root.querySelectorAll('[data-workflow-collection]').forEach(node=>{
        const own=collections.find(x=>x.id===node.dataset.workflowCollection),source=state.skills.find(x=>x.id===own?.skill_id);
        if(!source?.editable)return;
        const edit=document.createElement('button');edit.type='button';edit.className='button button-ghost button-small';edit.textContent='编辑自建原文';
        edit.onclick=event=>{event.stopPropagation();env.openSkill(source.id).catch(report);};
        node.querySelector('.skill-focus-card-more>div').append(edit);
      });
      root.querySelectorAll('[data-workflow-card]').forEach(node=>{
        const activate=event=>{if(event.target.closest('label')){event.stopPropagation();return;}if(event.target.closest('button,input,summary,select,textarea'))return;event.stopPropagation();if(event.target.closest('.skill-focus-card-more'))return;const id=node.dataset.workflowCard;
          if(event.ctrlKey || event.metaKey || event.shiftKey){const page=rows.slice(state.offset,state.offset+state.limit).map(x=>x.id);if(event.shiftKey && page.includes(selectionAnchor)){const a=page.indexOf(selectionAnchor),b=page.indexOf(id);page.slice(Math.min(a,b),Math.max(a,b)+1).forEach(x=>selected.add(x));}else{selected.has(id)?selected.delete(id):selected.add(id);selectionAnchor=id;}updateSelection();return;}
          selected=new Set([id]);selectionAnchor=id;updateSelection();
        };node.onclick=activate;
        node.ondblclick=event=>{if(event.target.closest('button,input,summary,select,textarea,label')||event.ctrlKey||event.metaKey||event.shiftKey)return;event.preventDefault();event.stopPropagation();action('open',node.dataset.workflowCard).catch(report);};
        node.onkeydown=event=>{if(event.target!==node || !['Enter',' '].includes(event.key))return;event.preventDefault();event.stopPropagation();if(event.key===' '){if(event.shiftKey){activate(event);return;}const id=node.dataset.workflowCard;selected.has(id)?selected.delete(id):selected.add(id);selectionAnchor=id;updateSelection();}else if(!event.ctrlKey&&!event.metaKey&&!event.shiftKey){selected=new Set([node.dataset.workflowCard]);selectionAnchor=node.dataset.workflowCard;updateSelection();action(organizationEnabled()?'organize':'open',node.dataset.workflowCard).catch(report);}else activate(event);};
      });
      root.querySelectorAll('.skill-focus-card-more summary').forEach(node=>node.onclick=event=>{event.stopPropagation();});
      root.querySelectorAll('[data-workflow="card-view"]').forEach(node=>{const on=node.dataset.id===focusView;node.setAttribute('aria-pressed',String(on));node.setAttribute('aria-label',node.dataset.id==='grid'?'卡片视图':'列表视图');node.title=node.getAttribute('aria-label');node.classList.toggle('active',on);});
      root.querySelector('#skillSourceDirectory')?.addEventListener('change',event=>env.setSource(event.target.value).catch(report));
      root.querySelector('.skill-library-sources').ontoggle=event=>{sourcesExpanded=event.target.open;};
      updateSelection();env.pagination(rows.length,focus?'SKILL 库':scopes.find(x=>x[0]===view)[1]);env.onNavigationChange?.(navigation());
    }
    function focusCard(x) {
      const purpose=categories.find(c=>c[0]===x.category)?.[1] || '未分类用途';
      const source=x.collection?x.origin?.source_label || state.skills.find(s=>s.id===x.skill_id)?.source_label || '收藏来源':x.source_label || x.source || (x.editable?'映序自建':'本机');
      const folderLabel=folders.find(f=>f.id===x.folder_id)?.path || '未归类';
      const star='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 2.8 5.7 6.3.9-4.6 4.5 1.1 6.3-5.6-3-5.6 3 1.1-6.3L2.9 9.6l6.3-.9L12 3Z"/></svg>';
      return `<article class="skill-card" tabindex="0" role="button" data-workflow-card="${e(x.id)}" ${x.collection?`data-workflow-collection="${e(x.id)}"`:`data-skill="${e(x.id)}"`} aria-label="选择 ${e(x.name)}；双击阅读"><div class="skill-card-top"><label class="skill-focus-checkbox"><input type="checkbox" data-workflow-select="${e(x.id)}" aria-label="选择 ${e(x.name)}"></label><span class="skill-focus-purpose" title="用途：${e(purpose)}">${e(purpose)}</span><div class="skill-focus-card-actions">${organizationEnabled()?`<button type="button" class="skill-focus-organize" data-workflow="organize" data-id="${e(x.id)}" title="分类 / 标签 / 备注" aria-label="整理 ${e(x.name)}">整理</button>`:''}<button type="button" class="icon-button skill-focus-star ${x.collection?'collected':''}" data-workflow="${x.collection?'versions':'collect'}" data-id="${e(x.id)}" aria-label="${e(x.name)}${x.collection?'已收藏；查看固定版本':'收藏完整包'}" title="${x.collection?'已收藏 · 查看固定版本':'收藏完整技能包'}">${star}</button><details class="skill-focus-card-more"><summary aria-label="${e(x.name)}更多操作" title="更多操作">${icon('more')}</summary><div>${button('open','阅读技能说明',x.id)}${organizationEnabled()?button('organize','分类 / 标签 / 备注',x.id):''}${x.collection?button('classify','编辑用途',x.id)+button('versions','版本管理',x.id)+`<button type="button" class="button button-ghost button-small" data-workflow="bind" data-id="${e(x.id)}" ${state.projectId?'':'disabled title="先打开项目才能绑定"'}>${x.bound?'解除项目绑定':'绑定当前收藏版本'}</button>`:button('collect','收藏完整技能包',x.id)+`<button type="button" class="button button-ghost button-small" data-skill-menu="${e(x.id)}">${x.editable?'编辑 / 原文件操作':'定位 / 隐藏只读源文件'}</button>`}</div></details></div></div><div class="skill-focus-copy"><h3>${e(x.name)}</h3><p>${e(x.description || '查看技能说明')}</p></div><div class="skill-focus-metadata"><span class="skill-source" title="来源：${e(source)}">${e(source)}</span>${x.bound?`<span class="skill-focus-bound" title="${x.collection&&x.bound_version?'项目固定版本：'+e(x.bound_version):'旧项目绑定，尚未固定收藏版本'}">${icon('check')}${x.collection&&x.bound_version?'固定 '+e(x.bound_version.slice(0,8)):'旧绑定 · 未固定'}</span>`:''}</div><div class="skill-card-bottom"><small class="skill-card-folder" title="个人分类：${e(folderLabel)}">${icon('folder')}${e(folderLabel)}</small><small class="skill-card-tags" title="${e((x.tags || []).join(' / '))}">${(x.tags || []).slice(0,3).map(t=>`<button type="button" class="skill-focus-tag" data-workflow="tag" data-id="${e(t)}">${e(t)}</button>`).join('')}${x.tags?.length>3?`<span>+${x.tags.length-3}</span>`:''}</small></div>${x.notes?`<small class="skill-card-notes" title="${e(x.notes)}">备注：${e(x.notes)}</small>`:''}</article>`;
    }
    function updateSelection() {
      if(!mountedRoot)return;
      mountedRoot.querySelectorAll('[data-workflow-card]').forEach(node=>{const on=selected.has(node.dataset.workflowCard);node.classList.toggle('checked',on);node.setAttribute('aria-pressed',String(on));});
      mountedRoot.querySelectorAll('[data-workflow-select]').forEach(node=>node.checked=selected.has(node.dataset.workflowSelect));
      const count=mountedRoot.querySelector('[data-workflow-count]');if(count)count.textContent=selected.size?`已选 ${selected.size} 项`:'在卡片间的空白处拖动可框选；Ctrl / Shift 可多选。';
      const batch=mountedRoot.querySelector('[data-workflow-batch]');if(batch){batch.hidden=!selected.size;const own=[...selected].some(id=>collections.some(x=>x.id===id)),external=[...selected].some(id=>!collections.some(x=>x.id===id));batch.querySelector('[data-workflow="batch-collect"]').disabled=!external;const classify=batch.querySelector('[data-workflow="batch-category"]');if(classify)classify.disabled=!own;batch.querySelector('[data-workflow="batch-bind"]').disabled=!own || !state.projectId;}
      mountedRoot.querySelector('.skill-library-selection')?.classList.toggle('has-selection',!!selected.size);
      const page=mountedRoot.querySelector('[data-workflow-select-page]');if(page){const ids=[...mountedRoot.querySelectorAll('[data-workflow-card]')].map(x=>x.dataset.workflowCard),count=ids.filter(id=>selected.has(id)).length;page.checked=ids.length>0&&count===ids.length;page.indeterminate=count>0&&count<ids.length;}
    }
    function selectionContext() {return [view,category,folder,tag,state.q,focused(),focusView].join('|');}
    async function refresh() { await load(); env.render(); }
    function editFolder(id='') {
      const own = folders.find(x=>x.id===id), parent = own?.parent_id || (folder!=='*'?folder:'');
      const descendants = new Set(id?[id]:[]);
      for(let i=0;i<folders.length;i++)for(const x of folders)if(descendants.has(x.parent_id))descendants.add(x.id);
      const parents = folders.filter(x=>!descendants.has(x.id));
      showDialog({title:own?'编辑分类文件夹':'新建分类文件夹',subtitle:'个人文件夹就是你的 SKILL 分类；只整理映序记录，原文件位置保持不变。',submit:own?'保存':'创建文件夹',body:`<div class="field"><label for="skillFolderName">名称</label><input id="skillFolderName" name="name" required maxlength="80" value="${e(own?.name || '')}" placeholder="例如：开发工具、视频制作"></div><div class="field"><label for="skillFolderParent">上级文件夹</label><select id="skillFolderParent" name="parent"><option value="">顶层</option>${parents.map(x=>`<option value="${e(x.id)}" ${x.id===parent?'selected':''}>${e(x.path || x.name)}</option>`).join('')}</select></div>${own?'<details class="skill-folder-delete"><summary>删除空文件夹</summary><label><input type="checkbox" name="delete"> 确认删除此文件夹；需先移出技能和子文件夹。</label></details>':''}`,onSubmit:async form=>{
        const data=new FormData(form);
        if(own && data.get('delete')){await api('/api/skill-folders/'+encodeURIComponent(id),{method:'DELETE',body:{}});folder=own.parent_id || '*';}
        else {const result=await api('/api/skill-folders'+(own?'/'+encodeURIComponent(id):''),{method:own?'PATCH':'POST',body:{name:String(data.get('name')).trim(),parent_id:data.get('parent')}});if(!own)folder=result.id;}
        selected.clear();state.offset=0;await refresh();
      }});
    }
    function organize(ids) {
      if(!ids.length)return toast('请先选择 SKILL。','info');
      const own=collections.find(x=>x.id===ids[0]), skillId=own?.skill_id || ids[0], first=metadata.get(skillId) || own || {}, single=ids.length===1;
      const skillIds=[...new Set(ids.map(id=>collections.find(x=>x.id===id)?.skill_id || id))];
      showDialog({title:single?'分类、标签与备注':'整理所选 SKILL',subtitle:single?'个人分类、标签和备注保存在映序中；外部 SKILL 正文保持只读。':`已选 ${skillIds.length} 项；批量只改选定字段，保留各自备注。`,submit:'保存整理',body:`<div class="field"><label for="skillOrganizeFolder">分类（个人文件夹）</label><select id="skillOrganizeFolder" name="folder">${!single?'<option value="*">保持各自文件夹</option>':''}${folderOptions(single?(first.folder_id || ''):undefined)}</select></div><div class="field"><label for="skillOrganizeTags">标签（每行一个）</label><textarea id="skillOrganizeTags" name="tags" maxlength="1312" rows="2" placeholder="例如：工具&#10;编程&#10;视频">${single?e((first.tags || []).join('\n')):''}</textarea><p class="field-hint">最多 32 个标签，每个最多 40 字。${single?'':'批量默认追加；不填写时保留各自标签。'}</p></div>${!single?'<label><input type="checkbox" name="replace"> 用上面的标签替换各自标签（空白会清空）</label>':`<div class="field"><label for="skillOrganizeNotes">备注</label><textarea id="skillOrganizeNotes" name="notes" maxlength="4000" rows="4" placeholder="适用场景、使用心得或注意事项…">${e(first.notes || '')}</textarea></div>`}`,onSubmit:async form=>{
        const data=new FormData(form), payload={skill_ids:skillIds}, target=data.get('folder'), tags=String(data.get('tags') || '').split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
        if(target!=='*')payload.folder_id=target;
        if(single?String(data.get('tags') || '')!==(first.tags || []).join('\n'):tags.length || data.get('replace')){payload.tags=tags;payload.tags_mode=single || data.get('replace')?'replace':'append';}
        if(single)payload.notes=String(data.get('notes') || '');
        await api('/api/skill-metadata',{method:'POST',body:payload});await refresh();toast('已保存技能整理。');
      }});
    }
    async function collect(ids) {
      if(!ids.length)return toast('请先选择外部 SKILL。','info');
      const previews=[];
      for(const id of ids) previews.push(await api('/api/skill-collections/preview',{method:'POST',body:{skill_id:id}}));
      const total=previews.reduce((n,p)=>n+(p.total_bytes||0),0);
      showDialog({title:'收藏完整 SKILL 包',subtitle:'保留源文件，复制说明及包内资源；不会执行脚本或安装依赖。',submit:'确认收藏',body:`<p>${previews.length} 个技能包 · ${env.formatSize(total)}</p><div class="skill-package-summary">${previews.map(p=>e(`${p.name || ''} · ${p.file_count || p.files?.length || 0} 个文件\n${(p.warnings||[]).join('\n')}`)).join('\n\n')}</div>`,onSubmit:async()=>{for(const p of previews)await api('/api/skill-collections/collect',{method:'POST',body:{token:p.token}});selected.clear();await refresh();toast('已收藏完整技能包。');}});
    }
    function classify(ids) {
      if(!ids.length)return toast('请先选择收藏中的 SKILL。','info');
      const first=collections.find(x=>x.id===ids[0]);
      showDialog({title:'选择收藏用途',submit:'保存用途',body:`<div class="field"><label for="skillCategory">用途</label><select name="category" id="skillCategory">${[['','未分类'],...categories.slice(1)].map(([id,label])=>`<option value="${id}" ${first?.category===id?'selected':''}>${label}</option>`).join('')}</select></div><p class="field-hint">个人分类、标签和备注请点卡片的“分类 / 标签 / 备注”。</p>`,onSubmit:async form=>{const data=new FormData(form);for(const id of ids)await api('/api/skill-collections/classify',{method:'POST',body:{id,category:data.get('category'),tags:collections.find(x=>x.id===id)?.tags || []}});await refresh();}});
    }
    async function bind(ids,bound=true) {
      if(!state.projectId)return toast('请先选择项目。','info');
      if(!ids.length)return toast('请先选择收藏中的 SKILL。','info');
      const projectId=state.projectId;for(const id of ids)await api('/api/skill-collections/bind',{method:'POST',body:{id,project_id:projectId,bound}});
      await env.reloadSkills();await refresh();toast(bound?'已绑定当前收藏版本。':'已解绑，收藏与源文件保留。');
    }
    async function action(name,id) {
      
      if(name==='view')return navigate({view:id});
      if(name==='clear-selection'){selected.clear();return updateSelection();}
      if(name==='browse-all'){view='all';category='';folder='*';tag='';selected.clear();state.q='';state.offset=0;setQuery('',true);await env.resetFilters?.();return env.render();}
      if(name==='folder')return navigate({folder:id});
      if(name==='tag')return navigate({tag:id});
      if(name==='card-view'){focusView=id==='list'?'list':'grid';return env.render();}
      if(name==='new-folder')return editFolder();
      if(name==='edit-folder')return editFolder(id);
      if(name==='organize')return organize([id]);
      if(name==='batch-organize')return organize([...selected]);
      const own=collections.find(x=>x.id===id);
      if(name==='open'){
        if(!own)return env.openSkill(id);
        const item=await api(`/api/skill-collections/${encodeURIComponent(id)}?version=${encodeURIComponent(own.version)}`);
        return showDialog({title:item.name || own.name,subtitle:'收藏版本 · 只读。更新来源不会覆盖项目固定版本。',body:`<div class="skill-package-summary">${e(item.content||'')}</div><p class="field-hint">${e(item.path||'')}</p>`,actions:'<button type="button" class="button button-primary" data-dialog-cancel>完成</button>'});
      }
      if(name==='versions' && own){
        showDialog({title:'收藏版本与项目锁定',subtitle:'收藏新版本不会自动更换项目正在使用的版本。',submit:state.projectId?'将选中版本用于当前项目':null,body:`<p>来源：${e(own.origin?.path || own.origin?.skill_path || '')}</p><div class="field"><label>收藏版本<select name="version">${own.versions.map(v=>`<option value="${e(v.version)}" ${v.version===own.version?'selected':''}>${e(v.version.slice(0,12))} · ${env.formatSize(v.total_bytes)}${v.version===own.bound_version?' · 项目正在使用':''}</option>`).join('')}</select></label></div><button type="button" class="button button-secondary" id="collectNewSkillVersion">检查并收藏源版本</button><p class="field-hint">查看收藏不会执行其中的脚本。升级前请先查看源技能及其资源变化。</p>`,onSubmit:async form=>{if(!state.projectId)return;await api('/api/skill-collections/bind',{method:'POST',body:{id,project_id:state.projectId,bound:true,version:new FormData(form).get('version')}});await refresh();}});
        document.querySelector('#collectNewSkillVersion').onclick=()=>collect([own.skill_id]).catch(report);return;
      }
      if(name==='collect')return collect([id]);
      if(name==='classify')return classify([id]);
      if(name==='bind')return own?bind([id],!own.bound):toast('请先确认收藏完整技能包，再绑定收藏版本。','info');
      if(name==='batch-collect')return collect([...selected].filter(id=>!collections.some(x=>x.id===id)));
      if(name==='batch-category')return classify([...selected].filter(id=>collections.some(x=>x.id===id)));
      if(name==='batch-bind')return bind([...selected].filter(id=>collections.some(x=>x.id===id)));
      if(name==='builtin'){
        const templates=[
          {name:'项目整理与命名',description:'按用途整理选定资料，先预览再移动。',content:'# 项目整理与命名\n\n只整理用户本轮选定的资料。先列出当前结构、命名冲突和建议目标；保持文件身份、引用和未保存文稿。分类显示调整不自动移动磁盘目录。真实迁移前给出来源、目标、容量及回退方案，取得确认后复制校验；不删除原件。结束时逐项报告成功、跳过和失败。'},
          {name:'分镜连续性检查',description:'核对选定镜头的角色、空间、道具和声音。',content:'# 分镜连续性检查\n\n读取用户选定的镜头与角色场景设定。依次核对出入画方向、视线、人物位置、服装道具、时间光线及声音衔接。区分已有证据和推测。列出问题镜号、具体冲突、最小修正和待确认项，不擅自重写整篇剧本。'},
          {name:'交付检查与回执',description:'对照任务检查产物、版本、遗漏和验证状态。',content:'# 交付检查与回执\n\n以本轮明确任务为准逐项检查文件是否存在、格式是否正确及引用是否完整。区分源码、候选包、正式安装和公开发布；测试通过不替代真实业务验收。回执列出产物位置、摘要、验证方法、未完成项及可恢复方案。不得擅自发布、永久删除或发送私人资料。'}
        ];
        showDialog({title:'选择内置规范',subtitle:'轻量纯文本模板，无模型、脚本或外部依赖。只添加你选择的一份。',submit:'添加到我的收藏',body:`<div class="field"><label>规范<select name="template">${templates.map((x,i)=>`<option value="${i}">${e(x.name)}</option>`).join('')}</select></label></div><p class="field-hint">用于整理项目、检查连续性和交付回执。收藏后需要明确绑定到项目才会出现在交接中。</p>`,onSubmit:async form=>{const template=templates[Number(new FormData(form).get('template'))];if(!template)throw Error('请选择有效规范。');const skill=await api('/api/skills',{method:'POST',body:template});const preview=await api('/api/skill-collections/preview',{method:'POST',body:{skill_id:skill.id}});await api('/api/skill-collections/collect',{method:'POST',body:{token:preview.token}});await env.reloadSkills();await refresh();toast('所选规范已加入收藏。');}});return;
      }
      if(name==='shared-library'){
        const info=await api('/api/skill-collections/export',{method:'POST',body:{project_id:state.projectId || ''}});
        return showDialog({title:'与曜核和客户端共享',subtitle:'共享固定版本的技能目录，不需要因端口改变重复收藏。',body:`<p>在支持自定义 SKILL 目录的客户端中登记下方目录。映序不改写其他客户端的配置。</p><input readonly aria-label="共享目录" value="${e(info.path||info.root||'')}"><p class="field-hint">已生成版本清单；${info.skipped_unpinned?.length ? `有 ${info.skipped_unpinned.length} 项旧绑定未固定版本，未导出。请先收藏并固定版本。` : ''}可访问不等于已加载或执行。远程客户端无法直接读取本机目录时，需要传递完整技能包。</p>`,actions:'<button type="button" class="button button-primary" data-dialog-cancel>完成</button>'});
      }
    }
    function mountHandoff(root,{mcp=true}={}) {
      if(mcp)window.YingXuMCP?.mount(root,{api,state,escapeHtml:e,copyText,toast,report});
      if(handoffProject!==state.projectId){handoffProject=state.projectId;handoffDraft={client_id:'codex',conversation_id:'',task:''};try{const saved=JSON.parse(env.storage?.get('yingxu:handoff:'+state.projectId)||'null');if(saved && typeof saved.client_id==='string' && typeof saved.conversation_id==='string')handoffDraft={...handoffDraft,client_id:saved.client_id,conversation_id:saved.conversation_id};}catch{}}
      const chosen=state.handoffSelection?.projectId===state.projectId?[...state.handoffSelection.ids]:[...state.selectedIds];
      const section=document.createElement('section');section.className='handoff-compose';
      section.innerHTML=`<h3>本轮交接</h3><p class="field-hint">同一会话只交接变化；换端口时保留客户端和会话名称。新会话自动使用完整交接。</p><div class="fields-two"><div class="field"><label>客户端<select name="client">${['codex','dsh','zcode','workbuddy','other'].map(x=>`<option ${handoffDraft.client_id===x?'selected':''}>${x}</option>`).join('')}</select></label></div><div class="field"><label>会话名称<input name="conversation" maxlength="120" placeholder="例如：第一集制作" value="${e(handoffDraft.conversation_id)}"></label></div></div><div class="field"><label>本轮完整任务<textarea name="task" maxlength="20000" placeholder="本轮需要 AI 完成什么…">${e(handoffDraft.task)}</textarea></label></div><div class="field"><label>资源范围<select name="scope"><option value="all">项目全部资源</option><option value="selected" ${chosen.length ? 'selected' : ''}>资源区已选的 ${chosen.length} 项</option></select></label></div><label><input type="checkbox" name="forceFull"> 本轮发送完整交接（对方丢失上下文时使用）</label><div class="settings-buttons"><button type="button" class="button button-primary" data-generate>生成本轮交接</button><button type="button" class="button button-secondary" data-new>新会话</button><button type="button" class="button button-ghost" data-history>查看上次交接</button></div><div data-output></div>`;
      root.querySelector('.handoff-card')?.after(section);
      const q=s=>section.querySelector(s),output=q('[data-output]');let busy=false,snapshot=null,alive=()=>section.isConnected && handoffProject===state.projectId;
      const read=()=>{handoffDraft={client_id:q('[name=client]').value,conversation_id:q('[name=conversation]').value.trim(),task:q('[name=task]').value};};
      for(const key of ['client','conversation','task','scope','forceFull'])q(`[name=${key}]`).oninput=()=>{revision++;read();snapshot=null;output.innerHTML='';};
      q('[data-new]').onclick=()=>{revision++;q('[name=conversation]').value=`会话-${new Date().toISOString().replace(/[:.]/g,'-')}`;read();snapshot=null;output.innerHTML='';};
      q('[data-history]').onclick=async()=>{
        read();if(!handoffDraft.conversation_id)return toast('请先填写会话名称。','info');
        const params=new URLSearchParams({project_id:state.projectId,client_id:handoffDraft.client_id,conversation_id:handoffDraft.conversation_id});
        try{const history=await api('/api/handoffs?'+params);if(!alive())return;if(!history.latest)return toast('这个会话还没有交接记录。','info');const last=await api('/api/handoffs/'+history.latest.snapshot_id);if(!alive())return;showDialog({title:'上次交接记录',subtitle:history.acknowledged_baseline?'已有用户确认的交接基准。':'尚未确认交接，下一轮仍使用完整交接。',body:`<p>记录 ${e(last.snapshot_id)}</p><textarea readonly class="handoff-output" aria-label="上次交接内容">${e(last.prompt)}</textarea>`,actions:'<button type="button" class="button button-primary" data-dialog-cancel>完成</button>'});}catch(error){if(alive())report(error);}
      };
      q('[data-generate]').onclick=async()=>{
        if(busy)return;read();if(!handoffDraft.conversation_id || !handoffDraft.task.trim())return toast('请填写会话名称和本轮任务。','info');
        if(env.hasPendingEdits?.() || state.tabs.some(t=>t.dirty || t.propertiesDirty || t.saving || t.propertiesSaving))return toast('请先保存文稿，再生成包含最新内容的交接。','info');
        busy=true;q('[data-generate]').disabled=true;const token=++revision;env.storage?.set('yingxu:handoff:'+state.projectId,JSON.stringify({client_id:handoffDraft.client_id,conversation_id:handoffDraft.conversation_id}));
        try{
          const result=await api('/api/handoffs',{method:'POST',body:{project_id:state.projectId,...handoffDraft,force_full:q('[name=forceFull]').checked,asset_ids:q('[name=scope]').value==='selected'?chosen:null}});
          if(!alive() || token!==revision)return;snapshot=result;
          output.innerHTML=`<p><strong>${result.mode==='delta'?'本轮变化':'首次完整交接'}</strong> · ${e(result.snapshot_id || result.id)}</p><pre class="handoff-output">${e(result.prompt)}</pre><div class="settings-buttons"><button type="button" class="button button-secondary" data-copy>复制交接</button><button type="button" class="button button-secondary" data-ack>我已交给该会话</button></div><p class="field-hint">复制不会推进基准。确认已交给这个会话后，下一轮才与此版本比较；没有自动发送消息。</p>`;
          q('[data-copy]').onclick=()=>copyText(snapshot.prompt,'已复制；发送后请点“我已交给该会话”。');
          q('[data-ack]').onclick=async()=>{const ack=q('[data-ack]');ack.disabled=true;try{await api('/api/handoffs/acknowledge',{method:'POST',body:{snapshot_id:snapshot.snapshot_id || snapshot.id}});if(alive())ack.textContent='已记录交接基准';}catch(err){if(alive())ack.disabled=false;report(err);}};
        }catch(err){if(alive())report(err);}finally{busy=false;if(alive())q('[data-generate]').disabled=false;}
      };
    }
    return {load,render,syncQuery,mountHandoff,action,navigation,navigate,getSelection:()=>selected,setSelection:ids=>{selected=new Set(ids);updateSelection();},selectionContext};
  }
  return {create};
})();
