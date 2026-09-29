'use strict';
// UI adapters use the existing authenticated API and dialog/draft lifecycle.
window.YingXuWorkflow = (() => {
  const categories = [['','全部用途'],['planning','策划与文本'],['visual','视觉与分镜'],['video','视频与预演'],['audio','声音与配音'],['development','开发与工具'],['delivery','整理与交付']];
  let view = null, category = '', selected = new Set(), collections = [], revision = 0;
  let handoffDraft = {client_id:'codex',conversation_id:'',task:''}, handoffProject = null;
  function create(env) {
    const {api,escapeHtml:e,icon,state,showDialog,toast,report,copyText} = env;
    if(view === null)view = state.projectId ? 'project' : 'favorites';
    let loadGeneration = 0;
    const button = (action,label,id='') => `<button type="button" class="button button-ghost button-small" data-workflow="${action}" data-id="${e(id)}">${label}</button>`;
    async function load() {
      const generation = ++loadGeneration, projectId = state.projectId || '';
      const result = await api(`/api/skill-collections?project=${encodeURIComponent(projectId)}`);
      if(generation !== loadGeneration || projectId !== (state.projectId || ''))return;
      collections = result.collections || [];
      for(const c of result.categories || [])if(!categories.some(x=>x[0]===c.id))categories.push([c.id,c.label]);
    }
    function items() {
      const external = state.skills.map(x=>({...x,collection:false}));
      const own = collections.filter(x=>!env.matchesCollectionSource || env.matchesCollectionSource(x)).map(x=>({...x,collection:true}));
      const collected = new Set(own.map(x=>x.skill_id));
      let result = view === 'favorites' ? own : view === 'project' ? [...own.filter(x=>x.bound),...external.filter(x=>x.bound && !collected.has(x.id))] : [...own,...external.filter(x=>!collected.has(x.id))];
      const query = state.q.trim().toLocaleLowerCase();
      return result.filter(x=>(!category || x.category===category) && (!query || `${x.name} ${x.description} ${(x.tags||[]).join(' ')}`.toLocaleLowerCase().includes(query)));
    }
    function render(root) {
      const rows=items(), ids=new Set(rows.map(x=>x.id)); selected=new Set([...selected].filter(id=>ids.has(id)));
      root.className='resource-grid skill-grid skill-library-list';
      root.innerHTML=env.sourcesHtml()+`<div class="skill-library-tools" role="group" aria-label="SKILL 范围">${[['project','本项目'],['favorites','我的收藏'],['all','全部来源']].map(([key,label])=>`<button type="button" class="button button-secondary button-small ${view===key?'active':''}" data-workflow="view" data-id="${key}" aria-pressed="${view===key}">${label}</button>`).join('')}<select id="skillPurpose" aria-label="按用途筛选">${categories.map(([id,label])=>`<option value="${id}" ${id===category?'selected':''}>${label}</option>`).join('')}</select>${button('batch-bind','绑定所选')}${button('batch-category','分类所选')}${button('batch-collect','收藏所选')}${button('builtin','内置规范')}${button('shared-library','曜核 / 客户端接入')}</div><p class="field-hint">收藏保留完整技能包；项目绑定固定版本。端口变化不影响绑定。共 ${rows.length} 项。</p>`+
        (rows.length?rows.slice(state.offset,state.offset+state.limit).map(x=>`<div class="skill-library-row"><input type="checkbox" data-workflow-select="${e(x.id)}" aria-label="选择 ${e(x.name)}" ${selected.has(x.id)?'checked':''}><button class="skill-copy" type="button" data-workflow="open" data-id="${e(x.id)}"><strong>${e(x.name)}</strong><small>${e(x.description || '查看技能说明')}</small><small>${e(categories.find(c=>c[0]===x.category)?.[1] || '未分类')} · ${e(x.collection?'我的收藏':x.source_label || x.source || '本机')} ${x.bound?' · 项目已启用':''}${x.version?' · '+e(x.version.slice(0,8)):''}${x.tags?.length?' · '+e(x.tags.join(' / ')):''}</small></button><div class="skill-actions">${x.collection?button('classify','分类',x.id)+button('versions','版本',x.id)+button('bind',x.bound?'解绑':'绑定项目',x.id):button('collect','收藏',x.id)}</div></div>`).join(''):'<p class="field-hint">这个范围暂无 SKILL。可在全部来源中选择收藏，再绑定当前项目。</p>');
      root.querySelector('#skillPurpose').onchange=event=>{category=event.target.value;state.offset=0;render(root);};
      root.querySelectorAll('[data-workflow-select]').forEach(node=>node.onchange=()=>{node.checked?selected.add(node.dataset.workflowSelect):selected.delete(node.dataset.workflowSelect);});
      root.querySelectorAll('[data-workflow]').forEach(node=>node.onclick=event=>{event.stopPropagation(); action(node.dataset.workflow,node.dataset.id).catch(report);});
      root.querySelector('#skillSourceDirectory')?.addEventListener('change',event=>env.setSource(event.target.value).catch(report));
      env.pagination(rows.length);
    }
    async function refresh() { await load(); env.render(); }
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
      showDialog({title:'整理 SKILL 用途',submit:'保存分类',body:`<div class="field"><label for="skillCategory">用途</label><select name="category" id="skillCategory">${[['','未分类'],...categories.slice(1)].map(([id,label])=>`<option value="${id}" ${first?.category===id?'selected':''}>${label}</option>`).join('')}</select></div><div class="field"><label for="skillTags">自定义标签（逗号分隔）</label><input id="skillTags" name="tags" maxlength="400" value="${e((first?.tags||[]).join('，'))}"></div>`,onSubmit:async form=>{const data=new FormData(form);for(const id of ids)await api('/api/skill-collections/classify',{method:'POST',body:{id,category:data.get('category'),tags:String(data.get('tags')).split(/[,，]/).map(x=>x.trim()).filter(Boolean)}});await refresh();}});
    }
    async function bind(ids,bound=true) {
      if(!state.projectId)return toast('请先选择项目。','info');
      if(!ids.length)return toast('请先选择收藏中的 SKILL。','info');
      const projectId=state.projectId;for(const id of ids)await api('/api/skill-collections/bind',{method:'POST',body:{id,project_id:projectId,bound}});
      await env.reloadSkills();await refresh();toast(bound?'已绑定当前收藏版本。':'已解绑，收藏与源文件保留。');
    }
    async function action(name,id) {
      if(name==='view'){view=id;state.offset=0;return env.render();}
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
      if(name==='bind')return bind([id],!own.bound);
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
    function mountHandoff(root) {
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
    return {load,render,mountHandoff};
  }
  return {create};
})();
