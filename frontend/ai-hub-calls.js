'use strict';
// Optional source role. No native worker, model credentials, timers or media download.
window.YingXuHubCalls=(()=>{
  const sessions=new WeakMap(),encode=value=>encodeURIComponent(String(value));
  const validId=value=>typeof value==='string' && /^[a-f0-9]{32}$/.test(value);
  const key=()=>window.crypto.randomUUID();
  const labels={prepared:'已保存，尚未派单',submitting:'提交结果待核对',unknown:'提交结果待核对',observed:'已取得原执行回执',not_started:'尚未提交提供方',submitting_provider:'工作端报告提交中',running:'工作端报告运行中',uncertain:'提供方结果未知',succeeded:'工作端报告完成',failed:'工作端报告失败',cancelled:'已报告取消'};
  function mount(root,env){
    const {state,task,run,api,escapeHtml:e,report}=env;
    let session=sessions.get(state);if(!session){session={drafts:new Map(),writes:0,views:new Set()};sessions.set(state,session);}
    const scope=[task.project_id,task.id,run.id].join('/');
    let d=session.drafts.get(scope);
    if(!d){d={open:false,loaded:false,sources:[],identity:null,selections:[],bindings:[],incomplete:[],sourceId:'',selectionId:'',input:'{"prompt":""}',grantName:'曜核本机',grantPath:'',configure:false,prepareRequest:null,prepareUnknown:false,notice:'',pendingActions:new Set()};session.drafts.set(scope,d);}
    let disposed=false,busyLocal=false,sequence=0,receiptViews=[];
    const prefix='/api/ai-tasks/'+encode(task.id)+'/runs/'+encode(run.id)+'/hub-calls';
    const alive=()=>!disposed && root.isConnected && state.projectId===task.project_id && (!env.isCurrent || env.isCurrent());
    const busy=()=>busyLocal || state.exitBusy || state.aiCollaborationBusy || state.migrationBusy || state.uploading || state.moveBusy || (!!state.aiToolCallBusy && !busyLocal);
    const button=(action,title,disabled=false,extra='')=>`<button type="button" class="button button-secondary button-small" data-hub="${action}" ${busy()||disabled?'disabled':''} ${extra}>${title}</button>`;
    const source=()=>d.sources.find(s=>s.id===d.sourceId);
    function paint(){
      if(!alive())return;
      receiptViews.forEach(v=>v.dispose());receiptViews=[];
      root.innerHTML=`<section class="ai-section ai-hub-calls"><div class="ai-section-heading"><h5>通过曜核调用 · 可选</h5>${button('toggle',d.open?'收起':'展开')}</div>${d.open?`${d.notice?`<p class="ai-notice" role="status">${e(d.notice)}</p>`:''}<p class="field-hint">映序保存原轮任务与收件，曜核管理授权、队列与工作端。每次派单需明确确认；关闭页面不会重新生成。</p><div class="ai-actions">${button('refresh','刷新原记录')}${button('configure',d.configure?'收起来源设置':'配置来源授权')}</div>${d.configure?`<div class="ai-call-connection"><h5>本机来源身份</h5><code>${e(d.identity?.source_authority || '请刷新读取')}</code>${button('copy-identity','复制身份',!d.identity)}<p class="field-hint">在曜核“协作接入”签发 source 授权并保存私有 JSON，然后在此填文件位置。只读授权可以核对历史；不接收 owner 或 worker 授权。</p><div class="ai-form-two"><label class="ai-field">来源名称<input data-hub-field="grantName" maxlength="80" value="${e(d.grantName)}" ${busy()?'disabled':''}></label><label class="ai-field">私有接入 JSON 文件路径<input data-hub-field="grantPath" value="${e(d.grantPath)}" ${busy()?'disabled':''}></label></div>${button('save-source','保存文件引用',!d.grantPath || !d.grantName)}<p class="field-hint">授权值留在私有文件，界面与任务不显示它。保存不连接服务，下一步需检查来源。</p></div>`:''}${d.loaded?`<div class="ai-form-two"><label class="ai-field">来源授权<select data-hub-field="sourceId" ${busy()?'disabled':''}><option value="">选择来源</option>${d.sources.map(s=>`<option value="${e(s.id)}" ${s.id===d.sourceId?'selected':''}>${e(s.name)} · ${s.read_only?'历史回查':'来源执行'}${s.checked?' · 已检查':''}${s.enabled?'':' · 已停用'}</option>`).join('')}</select></label><label class="ai-field">本轮已保存的能力选型<select data-hub-field="selectionId" ${busy()?'disabled':''}><option value="">选择固定选型</option>${d.selections.map(s=>`<option value="${e(s.id)}" ${s.id===d.selectionId?'selected':''}>${e(s.name || s.key)} · ${e(String(s.declaration_sha256).slice(0,12))}</option>`).join('')}</select></label></div><div class="ai-actions">${button('check-source','检查来源',!source()?.enabled)}${button('load-selections','刷新能力选型')}</div><p class="field-hint">选型沿用上方曜核只读连接保存的快照；只读选型本身不获得执行权限。</p><label class="ai-field">本次输入 JSON<textarea data-hub-field="input" maxlength="16000" rows="3" ${busy()||d.prepareRequest?'disabled':''}>${e(d.input)}</textarea></label><div class="ai-actions">${button(d.prepareUnknown?'reconcile-prepare':'prepare',d.prepareUnknown?'核对原准备':'保存本次执行意图',!d.prepareUnknown && (!source()?.execution_allowed || !d.selectionId || !!d.prepareRequest))}${button('new-prepare','准备另一次输入',d.prepareUnknown || !d.prepareRequest)}</div><p class="field-hint">保存意图不会生成；记录出现后再点击“明确派单”。输入必须符合工作端已发布声明，最多16000 UTF-8字节。</p>${d.incomplete.map(i=>`<p class="ai-notice">原准备记录尚未完成 ${button('restore','恢复原准备',false,`data-intent="${e(i.intent_id)}"`)}</p>`).join('')}<div class="ai-call-attempts">${d.bindings.map(b=>`<article><strong>${e(labels[b.local_state] || b.local_state)}${b.provider_state?' · '+e(labels[b.provider_state==='submitting'?'submitting_provider':b.provider_state] || b.provider_state):''}</strong><small>原请求 ${e(String(b.request_id).slice(0,12))} · ${e(b.dispatch_state || '本机准备')}</small><div class="ai-actions">${button('accept','明确派单',b.local_state!=='prepared' || d.pendingActions.has(b.binding_id),`data-binding="${e(b.binding_id)}"`)}${button('query','核对原请求',false,`data-binding="${e(b.binding_id)}"`)}${button('cancel','申请取消',!b.execution_id || b.cancel_requested || ['succeeded','failed','cancelled'].includes(b.provider_state),`data-binding="${e(b.binding_id)}"`)}</div>${b.cancel_requested?'<p class="field-hint">取消申请已记录，是否真正取消以工作端后续证据为准。</p>':''}${b.results?.length?`<ul>${b.results.map(r=>`<li>${e(r.remote_identity?.result_id || '')} · ${Number(r.size_bytes)||0}字节${r.sha256?' · SHA '+e(r.sha256.slice(0,12)):''}</li>`).join('')}</ul><p class="field-hint">请先将文件放入本轮生成目录，再明确关联并审核；不会自动下载或采用。</p>`:''}</article>`).join('') || '<p class="field-hint">本轮尚无来源执行记录。</p>'}</div>`:'<p class="field-hint">请刷新读取来源与原轮记录。</p>'}`:''}</section>`;
      if(d.open && d.loaded && window.YingXuHubReceipts){
        const articles=root.querySelectorAll('.ai-call-attempts > article');
        d.bindings.forEach((binding,index)=>{
          if(binding.provider_state!=='succeeded' || !binding.results?.length || !/^[a-f0-9]{64}$/.test(binding.results_manifest_sha256 || ''))return;
          const article=articles[index];if(!article)return;
          const slot=document.createElement('div');article.append(slot);
          receiptViews.push(window.YingXuHubReceipts.mount(slot,{...env,binding,isCurrent:alive,onBusyChanged:()=>{
            if(state.aiToolCallBusy){sequence++;root.querySelectorAll('[data-hub], [data-hub-field]').forEach(control=>{control.disabled=true;});}
            else paint();
            env.onBusyChanged?.();
          }}));
        });
      }
      root.onclick=event=>{const target=event.target.closest('[data-hub]');if(!target || !root.contains(target) || target.disabled)return;event.stopPropagation();void action(target.dataset.hub,target).catch(error=>{if(alive())report(error);});};
      root.oninput=root.onchange=event=>{const target=event.target,name=target.dataset.hubField;if(!name || busy())return;event.stopPropagation();if(['grantName','grantPath','sourceId','selectionId','input'].includes(name)){d[name]=target.value;if(['sourceId','selectionId'].includes(name))paint();else if(['grantName','grantPath'].includes(name)){const save=root.querySelector('[data-hub="save-source"]');if(save)save.disabled=busy() || !d.grantPath.trim() || !d.grantName.trim();}}};
    }
    function remember(value,expected){
      if(!value || !validId(value.binding_id) || value.project_id!==task.project_id || value.task_id!==task.id || value.run_id!==run.id)throw Error('执行回执不属于当前原轮。');
      if(expected && (value.binding_id!==expected.binding_id || value.request_id!==expected.request_id))throw Error('执行回执未对应原绑定或请求。');
      const index=d.bindings.findIndex(x=>x.binding_id===value.binding_id);if(index<0)d.bindings.unshift(value);else d.bindings[index]=value;
      return value;
    }
    async function load(){
      const current=++sequence;
      const results=await Promise.allSettled([api('/api/ai-hub-sources'),api(prefix),api(prefix.replace('/hub-calls','/capability-selections'))]);
      if(!alive() || current!==sequence || !d.open)return;
      if(results[0].status==='fulfilled'){d.sources=results[0].value.items || [];d.identity=results[0].value.identity;}else d.notice='来源读取失败，请刷新重试。';
      if(results[1].status==='fulfilled'){d.bindings=[];(results[1].value.items || []).forEach(remember);d.incomplete=results[1].value.incomplete || [];}else d.notice='执行记录读取失败，请刷新核对。';
      if(results[2].status==='fulfilled')d.selections=results[2].value.items || [];else d.notice='能力选型读取失败。';
      d.loaded=true;paint();
    }
    async function write(fn){
      if(!alive() || busy())return;
      if(env.hasPendingEdits?.()){report(Error('请先保存文稿，再操作来源执行。'));return;}
      sequence++;busyLocal=true;session.writes++;state.aiToolCallBusy=true;paint();
      env.onBusyChanged?.();
      try{await fn();}finally{busyLocal=false;session.writes--;state.aiToolCallBusy=session.writes>0;for(const view of session.views)view();env.onBusyChanged?.();}
    }
    async function action(name,target){
      if(!alive() || busy())return;
      if(name==='toggle'){d.open=!d.open;sequence++;paint();if(d.open)await load();return;}
      if(name==='configure'){d.configure=!d.configure;paint();return;}
      if(name==='refresh' || name==='load-selections'){d.notice='';return load();}
      if(name==='copy-identity'){await navigator.clipboard.writeText(d.identity.source_authority);return;}
      if(name==='new-prepare'){if(d.prepareUnknown)return;d.prepareRequest=null;d.notice='';paint();return;}
      if(name==='save-source')return write(async()=>{const value=await api('/api/ai-hub-sources',{method:'POST',body:{name:d.grantName,grant_path:d.grantPath,expected_revision:0}});d.sources=d.sources.filter(s=>s.id!==value.id).concat(value);d.sourceId=value.id;d.grantPath='';d.notice='已保存文件引用，尚未检查服务。';});
      if(name==='check-source')return write(async()=>{const value=await api('/api/ai-hub-sources/'+encode(d.sourceId)+'/check',{method:'POST',body:{}});d.sources=d.sources.filter(s=>s.id!==value.id).concat(value);d.notice='来源描述已核对；它不代表已经生成。';});
      if(name==='prepare' || name==='reconcile-prepare'){
        if(env.hasPendingEdits?.()){report(Error('请先保存文稿，再准备来源执行。'));return;}
        if(!d.prepareRequest){const s=source();if(!s?.execution_allowed || !d.selectionId)return;d.prepareRequest={source_id:s.id,source_revision:s.revision,selection_id:d.selectionId,input_json:d.input,idempotency_key:key()};}
        const original=d.prepareRequest;
        return write(async()=>{try{remember(await api(prefix,{method:'POST',body:original}));d.prepareUnknown=false;d.notice='原执行意图已保存，尚未派单。';}
          catch(error){if(error.unknownResult || !error.status || error.status>=500){d.prepareUnknown=true;d.notice='准备结果尚未确认，请核对原准备；不会派单。';}else if(!d.prepareUnknown)d.prepareRequest=null;throw error;}});
      }
      if(name==='restore')return write(async()=>{const intent=target.dataset.intent;remember(await api(prefix+'/'+encode(intent)+'/restore',{method:'POST',body:{}}));d.incomplete=d.incomplete.filter(i=>i.intent_id!==intent);});
      if(['accept','query','cancel'].includes(name))return write(async()=>{const id=target.dataset.binding;
        if(!validId(id))return;
        const original=d.bindings.find(b=>b.binding_id===id);if(!original)return;
        if(name==='accept')d.pendingActions.add(id);
        try{remember(await api(prefix+'/'+encode(id)+'/'+name,{method:'POST',body:{}}),original);d.pendingActions.delete(id);d.notice=name==='cancel'?'已申请取消，请核对后续状态。':'已记录原请求状态；不会自动收件或审核。';}
        catch(error){d.notice='原请求结果尚未确认，请点击核对原请求。';throw error;}
      });
    }
    session.views.add(paint);paint();if(d.open && !d.loaded)void load().catch(report);
    return {dispose(){disposed=true;sequence++;receiptViews.forEach(v=>v.dispose());receiptViews=[];session.views.delete(paint);root.onclick=root.oninput=root.onchange=null;},isBusy:()=>busyLocal};
  }
  return {mount};
})();
