'use strict';
// The task ledger owns no editor or media player. All results open in the existing workspace.
window.YingXuCollaboration = (() => {
  const sessions = new WeakMap();
  const kinds = {skill_test:'技能测试',video:'视频制作',general:'一般任务'};
  const labels = {draft:'待开始',active:'进行中',in_progress:'进行中',frozen:'已冻结',handed_off:'已交接',received:'已收件',pending:'待审核',pending_review:'待审核',completed:'已完成',accepted:'已通过',rejected:'未采用',needs_revision:'需返工',verified:'已核验',partial:'部分成功',interrupted:'中断',error:'失败',archived:'已归档'};
  const lines = value => String(value || '').split('\n').map(x=>x.trim()).filter(Boolean);
  const identity = value => encodeURIComponent(String(value));
  const key = () => window.crypto?.randomUUID?.() || 'receipt-'+Date.now()+'-'+Math.random().toString(36).slice(2);
  const object = value => !!value && typeof value==='object' && !Array.isArray(value);
  const entityId = value => typeof value==='string' && /^[a-f0-9]{32}$/.test(value);
  const unconfirmed = label => {const error=new Error(label+'结果未确认，可用相同请求核对。');error.unknownResult=true;return error;};
  const createPayload = (projectId,draft) => ({project_id:projectId,title:draft.title.trim(),kind:draft.kind,goal:draft.goal,acceptance:lines(draft.acceptance)});
  function navigation(state) {
    const session=sessions.get(state),cache=session?.projects.get(state.projectId);
    if(!cache)return null;
    return {tasks:cache.tasks.map(task=>({id:task.id,title:task.title,status:task.status,status_label:labels[task.status] || task.status})),
      total:cache.taskTotal,offset:cache.taskOffset,selected:cache.selected,creating:cache.creating,loading:cache.loading,
      busy:session.writes.has(state.projectId) || !!state.aiToolCallBusy};
  }
  async function navigate(state,name,id='') {
    if(state.section!=='context' || !['select','create','reload','tasks-prev','tasks-next'].includes(name))return;
    const cache=sessions.get(state)?.projects.get(state.projectId);
    if(!cache || name==='select' && !cache.tasks.some(task=>task.id===id))return;
    return cache.navigate?.(name,id);
  }
  function mount(root,env) {
    const {state,api,escapeHtml:e,toast,report} = env;
    let session = sessions.get(state);
    if(!session){session={projects:new Map(),writes:new Set(),writeCount:0,views:new Set()};sessions.set(state,session);}
    const projectId = state.projectId;
    root.querySelector('.ai-collaboration')?.remove();
    for(const view of Array.from(session.views))if(!view.alive())view.dispose();
    const node = document.createElement('section');node.className='ai-collaboration';root.append(node);
    if(!projectId){node.innerHTML='<div class="ai-empty"><h3>先打开一个项目</h3><p>任务、输入和成果都保存在所属项目中。</p></div>';return {isBusy:()=>!!session.writeCount};}
    let cache=session.projects.get(projectId);
    if(!cache){cache={tasks:[],taskOffset:0,taskTotal:0,selected:'',task:null,creating:false,createDraft:{title:'',kind:'general',goal:'',acceptance:''},drafts:new Map(),runs:new Map(),handoffs:new Map(),assets:[],assetOffset:0,assetTotal:0,collections:[],notice:'',loading:true,sequence:0};session.projects.set(projectId,cache);}
    cache.runLoading=false;cache.runError='';
    let disposed=false,loadSequence=0,runLoadSequence=0,assetLoadSequence=0,callView=null;
    const alive=()=>!disposed && node.isConnected && state.projectId===projectId && (!state.section || state.section==='context');
    const taskUrl=id=>'/api/ai-tasks/'+identity(id);
    const runUrl=(taskId,runId)=>taskUrl(taskId)+'/runs/'+identity(runId);
    const isBusy=()=>session.writes.has(projectId) || !!state.aiToolCallBusy;
    const lockControls=()=>node.querySelectorAll('[data-ai], [data-ai-field], [data-ai-input], [data-ai-pin], [data-ai-version], [data-ai-receive], [data-ai-receive-path], [data-ai-review-notes]').forEach(control=>{control.disabled=true;});
    const currentTask=()=>cache.task?.id===cache.selected?cache.task:null;
    const currentRun=()=>{const task=currentTask();return task?.runs?.find(x=>x.id===cache.runs.get(task.id)) || task?.runs?.reduce((latest,run)=>!latest || Number(run.run_number)>Number(latest.run_number)?run:latest,null) || null;};
    const draft=()=>{
      const task=currentTask();if(!task)return null;
      if(!cache.drafts.has(task.id)){
        const run=currentRun(),input=run?.input_snapshot || {};
        cache.drafts.set(task.id,{goal:input.goal || task.goal || '',acceptance:(input.acceptance || task.acceptance || []).join('\n'),client_id:run?.client_id || 'codex',conversation_id:run?.conversation_id || '',force_full:false,inputIds:new Set(input.input_item_ids || (input.inputs || []).map(x=>x.item_id || x.id)),pins:new Map((input.skill_pins || cache.collections.filter(x=>x.bound_version).map(x=>({collection_id:x.id,version:x.bound_version}))).map(x=>[x.collection_id,x.version])),pinVersions:new Map(),receiveIds:new Set(),receivePaths:new Set(),receiveRole:'result',receiptKey:null,receipt:null,reviewNotes:new Map(),preparing:!run});
      }
      return cache.drafts.get(task.id);
    };
    const button=(action,text,extra='',primary=false)=>`<button type="button" class="button ${primary?'button-primary':'button-secondary'} button-small" data-ai="${action}" ${isBusy()?'disabled':''} ${extra}>${text}</button>`;
    const options=(values,value)=>Object.entries(values).map(([id,title])=>`<option value="${e(id)}" ${value===id?'selected':''}>${e(title)}</option>`).join('');
    const field=(name,label,value,area=false)=>`<label class="ai-field">${label}${area?`<textarea data-ai-field="${name}" maxlength="20000">${e(value)}</textarea>`:`<input data-ai-field="${name}" maxlength="120" value="${e(value)}">`}</label>`;
    function assetChoices(selected,scope) {
      return `<div class="ai-choice-list">${cache.assets.map(item=>`<label><input type="checkbox" data-ai-${scope}="${e(item.id)}" ${selected.has(item.id)?'checked':''}><span>${e(item.name || item.id)}<small>${e(item.category || item.kind || '')}</small></span></label>`).join('') || '<p class="field-hint">本页没有已登记的项目文件；可先通过导入入口加入项目。</p>'}</div><div class="ai-page">${button('assets-prev','上一页',cache.assetOffset===0?'disabled':'')}${button('assets-next','下一页',cache.assetOffset+48>=cache.assetTotal?'disabled':'')}<span>已选 ${selected.size} 项 · 共 ${cache.assetTotal} 项</span></div>`;
    }
    function prepareHtml(task,d) {
      return `<section class="ai-section"><h4>准备下一轮</h4>${field('goal','本轮目标',d.goal,true)}${field('acceptance','验收要求（每行一项）',d.acceptance,true)}<div class="ai-form-two"><label class="ai-field">客户端<select data-ai-field="client_id">${options({codex:'Codex',dsh:'DSH',zcode:'ZCode',workbuddy:'WorkBuddy',other:'其他'},d.client_id)}</select></label>${field('conversation_id','会话名称',d.conversation_id)}</div><h5>固定 SKILL 版本</h5><div class="ai-choice-list">${cache.collections.map(c=>`<label><input type="checkbox" data-ai-pin="${e(c.id)}" ${d.pins.has(c.id)?'checked':''}><span>${e(c.name)}${c.bound_version?'<small>项目已固定</small>':''}</span><select aria-label="${e(c.name)}的收藏版本" data-ai-version="${e(c.id)}">${(c.versions || [{version:c.version}]).map(v=>`<option value="${e(v.version)}" ${(d.pins.get(c.id) || d.pinVersions.get(c.id) || c.bound_version || c.version)===v.version?'selected':''}>${e(v.version.slice(0,12))}</option>`).join('')}</select></label>`).join('') || '<p class="field-hint">暂无收藏版本。请先到 SKILL 库收藏所需技能。</p>'}</div><h5>本轮输入 · 项目已登记文件</h5>${assetChoices(d.inputIds,'input')}<p class="field-hint">固定本轮后，输入与技能版本保留在轮次记录中。请先保存文稿。</p>${button('freeze','固定输入，建立本轮','',true)}</section>`;
    }
    function frozenHtml(run) {
      const input=run.input_snapshot || {},skills=input.skill_pins || input.skills || [];
      return `<details class="ai-frozen"><summary>查看本轮固定材料与验收</summary><h5>验收要求</h5><ul>${(input.acceptance || []).map(x=>`<li>${e(x)}</li>`).join('') || '<li>本轮未填写验收条目。</li>'}</ul><h5>输入材料</h5><ul>${(input.inputs || []).map(x=>`<li>${e(x.name || x.item_id)}<small>${x.verification==='sha256'?'已记录内容摘要':'仅记录索引元数据，未核验内容'}</small></li>`).join('') || '<li>本轮未指定输入文件。</li>'}</ul><h5>固定 SKILL</h5><ul>${skills.map(x=>`<li>${e(x.name || x.collection_id)} · ${e(x.version)}${(x.warnings || []).map(w=>`<small>${e(w)}</small>`).join('')}</li>`).join('') || '<li>本轮未指定技能。</li>'}</ul></details>`;
    }
    function runHtml(task,run,d) {
      const handoff=cache.handoffs.get(run.id),directories=run.directories || {},artifacts=(run.artifacts || task.artifacts || []).filter(x=>x.run_id===run.id),receipts=(run.receipts || task.receipts || []).filter(x=>x.run_id===run.id);
      const receipt=d.receipt?.run_id && d.receipt.run_id!==run.id?null:d.receipt;
      return `<section class="ai-section"><div class="ai-section-heading"><h4>第 ${Number(run.run_number)||1} 轮</h4><span class="ai-status">${e(labels[run.status] || run.status || '已冻结')}</span><select data-ai-field="run_id" aria-label="选择轮次">${task.runs.map(r=>`<option value="${e(r.id)}" ${r.id===run.id?'selected':''}>第 ${Number(r.run_number)||1} 轮</option>`).join('')}</select></div><p class="ai-goal">${e(run.input_snapshot?.goal || task.goal || '')}</p>${frozenHtml(run)}<div class="ai-run-summary"><span>${Number(run.input_snapshot?.input_item_ids?.length || run.input_snapshot?.inputs?.length || 0)} 项输入</span><span>${Number(run.input_snapshot?.skill_pins?.length || 0)} 个固定技能</span>${directories.generated?button('folder','打开本轮成果目录'):''}${directories.references?button('references-folder','打开本轮记录目录'):''}</div><div class="ai-form-two"><label class="ai-field">客户端<select data-ai-field="client_id">${options({codex:'Codex',dsh:'DSH',zcode:'ZCode',workbuddy:'WorkBuddy',other:'其他'},d.client_id)}</select></label>${field('conversation_id','交接会话名称',d.conversation_id)}</div><div class="ai-actions"><label><input type="checkbox" data-ai-field="force_full" ${d.force_full?'checked':''}> 发送完整上下文</label>${button('new-session','新会话')}${button('handoff','生成交接','',true)}</div>${handoff?`<div class="ai-handoff"><strong>${handoff.mode==='delta'?'本轮变化交接':'完整交接'}</strong><pre>${e(handoff.prompt)}</pre><div class="ai-actions">${button('copy','复制交接')}${button('ack','我已交给该会话',handoff.acknowledged?'disabled':'')}</div><p class="field-hint">复制不会推进基准；确认已发送后，下一轮才与此版本比较。</p></div>`:''}<div class="ai-section-heading"><h4>接收成果</h4>${button('candidates','查看本轮目录文件')}</div>${d.candidates?`<div class="ai-choice-list">${d.candidates.files.map(f=>`<label><input type="checkbox" data-ai-receive-path="${e(f.relative_path)}" ${d.receivePaths.has(f.relative_path)?'checked':''}><span>${e(f.relative_path)}<small>${Number(f.size)||0} 字节</small></span></label>`).join('') || '<p class="field-hint">目录中暂无可接收文件。</p>'}</div>${(d.candidates.errors || []).map(x=>`<p class="ai-error">${e(x.error || x.message || x)}</p>`).join('')}`:''}<details class="ai-existing"><summary>或选择已有项目文件</summary>${assetChoices(d.receiveIds,'receive')}</details><div class="ai-actions"><label class="ai-field ai-role">成果用途<input data-ai-field="receiveRole" maxlength="40" value="${e(d.receiveRole)}"></label>${button('receive','接收所选成果','',true)}</div>${receipt?receiptHtml(receipt):''}${receipts.length?`<details><summary>持久收件记录 · ${receipts.length} 次</summary>${receipts.map(r=>`<div class="ai-receipt-row"><span>${e(labels[r.state] || r.state)} · ${e(r.id.slice(0,12))}</span>${button('receipt','查看核验记录',`data-receipt="${e(r.id)}"`)}</div>`).join('')}</details>`:''}<h4>成果与审核</h4><div class="ai-artifacts">${artifacts.map(a=>`<article class="ai-artifact"><div><strong>${e(a.name || a.item_name || a.item?.name || a.item_id)}</strong><small>${e(a.role || 'result')} · ${e(labels[a.verification_status] || a.verification_status || '待核验')} · ${e(labels[a.review_status] || a.review_status || '待审核')}</small></div>${button('preview','打开预览',`data-artifact="${e(a.id)}"`)}<label class="ai-field">审核备注<textarea maxlength="4000" data-ai-review-notes="${e(a.id)}">${e(d.reviewNotes.get(a.id) ?? a.review_notes ?? '')}</textarea></label><div class="ai-actions">${button('review','通过',`data-artifact="${e(a.id)}" data-decision="accepted"`)}${button('review','需返工',`data-artifact="${e(a.id)}" data-decision="needs_revision"`)}${button('review','不采用',`data-artifact="${e(a.id)}" data-decision="rejected"`)}</div></article>`).join('') || '<p class="field-hint">接收并核验后，成果会出现在这里。点击预览逐项查看，再记录审核结果。</p>'}</div><div class="ai-actions">${button('next','准备下一轮')}${button('complete','确认任务完成',['completed','已完成','archived','已归档'].includes(task.status)?'disabled':'')}</div></section>${state.bootstrap?.capabilities?.ai_tool_calls?'<div data-ai-tool-calls></div>':''}`;
    }
    function receiptHtml(receipt) {return `<div class="ai-receipt" role="status"><strong>收件：${e(labels[receipt.state] || receipt.state)}</strong>${(receipt.results || []).map(r=>`<p class="${r.status==='error'?'ai-error':''}">${e(r.relative_path || r.item_id || r.name || '文件')} · ${e(labels[r.status] || r.status)}${r.error?' · '+e(r.error):''}${r.sha256?`<small>SHA-256 ${e(r.sha256)}</small>`:''}</p>`).join('')}</div>`;}
    function paint() {
      if(!alive())return;
      env.onNavigationChange?.();
      callView?.dispose();callView=null;
      if(cache.runLoading || cache.runError){
        node.innerHTML=`<div class="ai-empty" role="status"><h3>${cache.runLoading?'正在读取本轮固定记录…':'无法读取本轮记录'}</h3>${cache.runError?button('reload','刷新重试'):''}</div>`;
        node.oninput=node.onchange=null;node.onclick=event=>{const target=event.target.closest('[data-ai]');if(target && !target.disabled && node.contains(target)){event.stopPropagation();void action(target.dataset.ai,target).catch(report);}};return;
      }
      const task=currentTask(),run=currentRun(),d=task?draft():null,c=cache.createDraft;
      node.innerHTML=`<header class="ai-heading"><div><h3>协作任务</h3><p>固定材料、交接任务、收回成果，逐轮推进。</p></div>${button('create','新建任务','',true)}${button('reload','刷新记录')}</header>${cache.notice?`<p class="ai-notice" role="status">${e(cache.notice)}</p>`:''}<div class="ai-layout"><nav class="ai-task-list" aria-label="项目协作任务">${cache.tasks.map(t=>`<button type="button" class="ai-task ${cache.selected===t.id&&!cache.creating?'active':''}" data-ai="select" data-task="${e(t.id)}" ${isBusy()?'disabled':''}><strong>${e(t.title)}</strong><span>${e(kinds[t.kind] || t.kind)} · ${e(labels[t.status] || t.status)}</span></button>`).join('') || `<div class="ai-empty">${cache.loading?'正在读取任务…':'这个项目还没有协作任务。'}${!cache.loading?button('create','创建第一个任务'):''}</div>`}</nav><div class="ai-detail">${cache.creating?`<section class="ai-section"><h4>新建任务</h4>${field('title','任务标题',c.title)}<label class="ai-field">任务类型<select data-ai-field="kind">${options(kinds,c.kind)}</select></label>${field('goal','目标',c.goal,true)}${field('acceptance','验收要求（每行一项）',c.acceptance,true)}<div class="ai-actions">${button('save-create','创建任务','',true)}${button('cancel-create','取消')}</div></section>`:task?`<div class="ai-task-heading"><h3>${e(task.title)}</h3><span>${e(kinds[task.kind] || task.kind)} · ${e(labels[task.status] || task.status)}</span></div>${run?runHtml(task,run,d):''}${d.preparing || !run?prepareHtml(task,d):''}`:`<div class="ai-empty"><h3>${cache.loading?'正在读取…':'选择任务，或开始新的协作'}</h3></div>`}</div></div>`;
      if(cache.taskTotal>48){const pager=document.createElement('div');pager.className='ai-page';pager.innerHTML=`${button('tasks-prev','上一页任务',cache.taskOffset===0?'disabled':'')}${button('tasks-next','下一页任务',cache.taskOffset+48>=cache.taskTotal?'disabled':'')}<span>共 ${cache.taskTotal} 个任务</span>`;(node.querySelector('.ai-task-list') || node).append(pager);}
      node.onclick=event=>{const target=event.target.closest('[data-ai]');if(!target || !node.contains(target) || target.disabled)return;event.stopPropagation();void action(target.dataset.ai,target).catch(error=>{if(alive())report(error);});};
      node.oninput=node.onchange=event=>{
        const target=event.target,activeDraft=cache.creating?cache.createDraft:draft();if(isBusy() || !activeDraft)return;
        if(target.dataset.aiField){const name=target.dataset.aiField;if(name==='run_id'){cache.runs.set(task.id,target.value);const td=draft();td.receipt=null;td.candidates=null;td.receivePaths.clear();td.receiveIds.clear();td.receiptKey=null;void readRun(task.id,target.value).catch(error=>{if(alive())report(error);});paint();}else {activeDraft[name]=target.type==='checkbox'?target.checked:target.value;if(name==='receiveRole')activeDraft.receiptKey=null;}}
        if(target.dataset.aiInput)(target.checked?activeDraft.inputIds.add(target.dataset.aiInput):activeDraft.inputIds.delete(target.dataset.aiInput));
        if(target.dataset.aiReceive){target.checked?activeDraft.receiveIds.add(target.dataset.aiReceive):activeDraft.receiveIds.delete(target.dataset.aiReceive);activeDraft.receiptKey=null;}
        if(target.dataset.aiReceivePath){target.checked?activeDraft.receivePaths.add(target.dataset.aiReceivePath):activeDraft.receivePaths.delete(target.dataset.aiReceivePath);activeDraft.receiptKey=null;}
        if(target.dataset.aiPin){const id=target.dataset.aiPin,c=cache.collections.find(x=>x.id===id);target.checked?activeDraft.pins.set(id,activeDraft.pinVersions.get(id) || c?.bound_version || c?.version):activeDraft.pins.delete(id);}
        if(target.dataset.aiVersion){activeDraft.pinVersions.set(target.dataset.aiVersion,target.value);if(activeDraft.pins.has(target.dataset.aiVersion))activeDraft.pins.set(target.dataset.aiVersion,target.value);}
        if(target.dataset.aiReviewNotes)activeDraft.reviewNotes.set(target.dataset.aiReviewNotes,target.value);
      };
      const callRoot=node.querySelector('[data-ai-tool-calls]');
      if(callRoot && task && run && window.YingXuToolCalls)callView=window.YingXuToolCalls.mount(callRoot,{...env,task,run,isCurrent:()=>alive() && currentTask()?.id===task.id && currentRun()?.id===run.id,onBusyChanged:()=>{
        // Busy state is shared, but this notification reads no old task data.
        // A complete parent remount must receive the original write's release.
        for(const view of Array.from(session.views)){
          if(!view.alive())view.dispose();else view.changed();
        }
      },onReceipt:async()=>{
        if(alive() && currentTask()?.id===task.id && currentRun()?.id===run.id)await readTask(task.id);
      }});
      if(isBusy())lockControls();
    }
    async function readTask(taskId) {
      const sequence=++loadSequence;
      const value=await api(taskUrl(taskId));
      if(!alive() || sequence!==loadSequence || cache.selected!==taskId)return;
      const task=value.task || value;
      if(task.id!==taskId || task.project_id && task.project_id!==projectId)throw Error('返回的任务不属于当前项目。');
      cache.task=task;cache.tasks=cache.tasks.map(x=>x.id===taskId?task:x);
      const run=currentRun();if(run && !run.input_snapshot)await readRun(taskId,run.id,sequence);else paint();
    }
    async function readRun(taskId,runId,taskSequence=loadSequence) {
      const sequence=++runLoadSequence;
      cache.runLoading=true;cache.runError='';paint();
      const valid=()=>alive() && taskSequence===loadSequence && sequence===runLoadSequence && cache.selected===taskId && currentRun()?.id===runId;
      try {
        const value=await api(runUrl(taskId,runId));if(!valid())return;
        const run=value.run || value;
        if(run.id!==runId || run.task_id!==taskId)throw Error('返回的轮次不属于当前任务。');
        cache.task.runs=cache.task.runs.map(x=>x.id===runId?run:x);
      }catch(error){if(valid()){cache.runError='读取失败';report(error);}}finally{if(valid()){cache.runLoading=false;paint();}}
    }
    async function loadAssets(offset=cache.assetOffset) {
      const sequence=++assetLoadSequence,taskId=cache.selected,runId=currentRun()?.id;
      const value=await api(`/api/items?project=${identity(projectId)}&limit=48&offset=${offset}&sort=updated`);
      if(!alive() || sequence!==assetLoadSequence || taskId!==cache.selected || runId!==currentRun()?.id)return;
      cache.assets=value.items || [];cache.assetTotal=Number(value.total)||cache.assets.length;cache.assetOffset=offset;paint();
    }
    async function load() {
      const sequence=++cache.sequence;
      const results=await Promise.allSettled([api('/api/ai-tasks?project_id='+identity(projectId)+(cache.taskOffset?'&limit=48&offset='+cache.taskOffset:'')),api('/api/skill-collections?project='+identity(projectId)),api(`/api/items?project=${identity(projectId)}&limit=48&offset=${cache.assetOffset}&sort=updated`)]);
      if(!alive() || sequence!==cache.sequence)return;
      cache.loading=false;
      if(results[0].status==='rejected'){cache.notice='无法读取任务记录，请刷新重试。';paint();report(results[0].reason);return;}
      cache.tasks=results[0].value.tasks || results[0].value.items || [];
      cache.taskTotal=Number(results[0].value.total)||cache.tasks.length;
      if(results[1].status==='fulfilled')cache.collections=results[1].value.collections || [];else cache.notice='收藏版本读取失败；请刷新后再固定技能。';
      if(results[2].status==='fulfilled'){cache.assets=results[2].value.items || [];cache.assetTotal=Number(results[2].value.total)||cache.assets.length;}else cache.notice='项目文件读取失败；请刷新后再选择输入。';
      if(!cache.tasks.some(t=>t.id===cache.selected))cache.selected=cache.tasks[0]?.id || '';
      paint();if(cache.selected)await readTask(cache.selected);
    }
    async function write(taskId,runId,operation) {
      if(!alive() || isBusy() || state.exitBusy)return;
      session.writes.add(projectId);session.writeCount++;state.aiCollaborationBusy=true;paint();
      const valid=()=>alive() && cache.selected===taskId && (!runId || currentRun()?.id===runId);
      try {await operation(valid);}catch(error){if(valid())report(error);}finally{session.writes.delete(projectId);session.writeCount--;state.aiCollaborationBusy=session.writeCount>0;cache.repaint?.();}
    }
    async function action(name,target) {
      if(!alive() || isBusy())return;
      const task=currentTask(),run=currentRun(),d=task?draft():null;
      if(name==='create'){cache.creating=true;paint();return;}
      if(name==='cancel-create'){cache.creating=false;paint();return;}
      if(name==='reload')return load();
      if(name==='tasks-prev' || name==='tasks-next'){cache.taskOffset=Math.max(0,cache.taskOffset+(name==='tasks-next'?48:-48));return load();}
      if(name==='select'){cache.creating=false;cache.selected=target.dataset.task;cache.task=null;paint();return readTask(cache.selected);}
      if(name==='assets-prev' || name==='assets-next')return loadAssets(Math.max(0,cache.assetOffset+(name==='assets-next'?48:-48)));
      if(name==='save-create'){
        const c={...cache.createDraft};if(!c.title.trim() || !c.goal.trim())return toast('请填写任务标题与目标。','info');
        const body=createPayload(projectId,c),signature=JSON.stringify(body);
        if(cache.createDraft.requestPayload!==signature){cache.createDraft.requestKey=key();cache.createDraft.requestPayload=signature;}
        body.idempotency_key=cache.createDraft.requestKey;
        return write(cache.selected,'',async valid=>{
          let result;try{result=await api('/api/ai-tasks',{method:'POST',body});}catch(error){if(error.unknownResult)throw unconfirmed('创建');throw error;}
          if(!valid())return;const t=result?.task || result;
          if(!object(t) || !entityId(t.id) || t.project_id!==projectId || typeof t.title!=='string' || !Object.hasOwn(kinds,t.kind) || !Number.isInteger(t.revision))throw unconfirmed('创建');
          cache.tasks=cache.tasks.filter(x=>x.id!==t.id);cache.tasks.unshift(t);
          if(JSON.stringify(createPayload(projectId,cache.createDraft))!==signature){cache.notice='任务已创建；你在等待时修改的草稿已保留。';return;}
          cache.creating=false;cache.createDraft={title:'',kind:'general',goal:'',acceptance:''};cache.selected=t.id;cache.task=t;await readTask(t.id);
        });
      }
      if(!task)return;
      if(name==='freeze'){
        if(env.hasPendingEdits?.() || (state.tabs || []).some(t=>t.dirty || t.propertiesDirty || t.saving || t.propertiesSaving))return toast('请先保存文稿，再固定本轮输入。','info');
        if(!d.goal.trim() || !d.conversation_id.trim())return toast('请填写本轮目标和会话名称。','info');
        const body={goal:d.goal,acceptance:lines(d.acceptance),input_item_ids:[...d.inputIds],skill_pins:[...d.pins].map(([collection_id,version])=>({collection_id,version})),client_id:d.client_id,conversation_id:d.conversation_id.trim(),expected_revision:task.revision};
        return write(task.id,'',async valid=>{const value=await api(taskUrl(task.id)+'/runs',{method:'POST',body});if(!valid())return;const r=value?.run || value;if(!object(r) || !entityId(r.id) || r.task_id!==task.id || !Number.isInteger(r.run_number) || r.run_number<1 || !object(r.input_snapshot) || !object(r.directories))throw unconfirmed('固定轮次');cache.runs.set(task.id,r.id);d.preparing=false;d.candidates=null;d.receipt=null;d.receivePaths.clear();d.receiveIds.clear();d.receiptKey=null;await readTask(task.id);});
      }
      if(name==='next'){d.preparing=true;paint();node.querySelector('[data-ai-field="goal"]')?.focus();return;}
      if(name==='complete')return write(task.id,'',async valid=>{await api(taskUrl(task.id)+'/complete',{method:'POST',body:{confirmed:true,expected_revision:task.revision}});if(valid())await readTask(task.id);});
      if(!run)return;
      if(name==='folder' || name==='references-folder'){const category=name==='folder'?'generated':'references',dir=run.directories?.[category];if(dir?.folder_id)await env.openFolder({project_id:projectId,category,folder_id:dir.folder_id});return;}
      if(name==='new-session'){d.conversation_id='';d.force_full=true;cache.handoffs.delete(run.id);paint();node.querySelector('[data-ai-field="conversation_id"]')?.focus();return;}
      if(name==='handoff'){
        if(env.hasPendingEdits?.())return toast('请先保存文稿，再生成交接。','info');
        if(!d.conversation_id.trim())return toast('请填写交接会话名称。','info');
        const body={client_id:d.client_id,conversation_id:d.conversation_id.trim(),force_full:d.force_full};
        return write(task.id,run.id,async valid=>{const value=await api(runUrl(task.id,run.id)+'/handoff',{method:'POST',body});if(!valid())return;if(!object(value) || !entityId(value.snapshot_id) || !entityId(value.id) || value.id!==value.snapshot_id || value.task_id!==task.id || value.run_id!==run.id || !['full','delta'].includes(value.mode) || typeof value.prompt!=='string' || !value.prompt.trim())throw unconfirmed('交接');if(d.client_id===body.client_id && d.conversation_id.trim()===body.conversation_id)cache.handoffs.set(run.id,{...value,client_id:body.client_id,conversation_id:body.conversation_id});});
      }
      if(name==='copy' || name==='ack'){
        const h=cache.handoffs.get(run.id);if(!h)return;
        if(h.client_id!==d.client_id || h.conversation_id!==d.conversation_id.trim())return toast('交接会话已改变，请重新生成交接。','info');
        if(name==='copy'){await env.copyText(h.prompt,'交接已复制。');return;}
        return write(task.id,run.id,async valid=>{await api(taskUrl(task.id)+'/handoffs/'+identity(h.snapshot_id || h.id)+'/ack',{method:'POST',body:{}});if(valid()){h.acknowledged=true;await readTask(task.id);}});
      }
      if(name==='candidates'){const result=await api(runUrl(task.id,run.id)+'/candidates');if(alive() && cache.selected===task.id && currentRun()?.id===run.id){d.candidates=result;if(result.truncated)cache.notice='本轮候选列表已达到检查上限；这里只列出已检查文件。';paint();}return;}
      if(name==='receive'){
        const files=[...d.receivePaths].map(relative_path=>({relative_path,role:d.receiveRole})).concat([...d.receiveIds].map(item_id=>({item_id,role:d.receiveRole})));
        if(!files.length)return toast('请先选择本轮目录文件或已有项目文件。','info');
        d.receiptKey ||= key();const body={idempotency_key:d.receiptKey,files};
        return write(task.id,run.id,async valid=>{const result=await api(runUrl(task.id,run.id)+'/receive',{method:'POST',body});if(valid()){if(!object(result) || !entityId(result.id) || result.task_id!==task.id || result.run_id!==run.id || !['completed','partial','interrupted'].includes(result.state) || !Array.isArray(result.results) || !result.results.every(x=>object(x) && ['verified','error'].includes(x.status)))throw unconfirmed('收件');d.receipt={...result,run_id:run.id};for(const file of result.results)if(file.status==='verified'){d.receivePaths.delete(file.relative_path);d.receiveIds.delete(file.item_id);}if(result.state==='completed'){d.receivePaths.clear();d.receiveIds.clear();}d.receiptKey=null;await readTask(task.id);}});
      }
      if(name==='receipt'){const receiptId=target.dataset.receipt;const value=await api(taskUrl(task.id)+'/receipts/'+identity(receiptId));if(alive() && cache.selected===task.id && currentRun()?.id===run.id && (!value.run_id || value.run_id===run.id)){d.receipt=value;paint();}return;}
      const artifact=(run.artifacts || task.artifacts)?.find(x=>x.id===target.dataset.artifact && x.run_id===run.id);
      if(name==='preview' && artifact?.item_id)return env.openItem(artifact.item_id);
      if(name==='review' && artifact)return write(task.id,run.id,async valid=>{await api(taskUrl(task.id)+'/artifacts/'+identity(artifact.id)+'/review',{method:'PATCH',body:{decision:target.dataset.decision,notes:d.reviewNotes.get(artifact.id) ?? artifact.review_notes ?? '',expected_revision:artifact.revision}});if(valid())await readTask(task.id);});
    }
    const navigateView=(name,id)=>action(name,{dataset:{task:id}});
    cache.navigate=navigateView;
    const dispose=()=>{if(disposed)return;disposed=true;loadSequence++;callView?.dispose();session.views.delete(busyView);if(cache.navigate===navigateView)cache.navigate=null;};
    const busyView={alive,dispose,changed:()=>{if(isBusy())lockControls();else paint();}};
    session.views.add(busyView);
    cache.repaint=paint;paint();void load().catch(error=>{if(alive())report(error);});
    return {isBusy:()=>!!session.writeCount || !!state.aiToolCallBusy,dispose,refresh:load};
  }
  return {mount,navigation,navigate};
})();
