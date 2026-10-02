'use strict';
// Explicit local files only. No URL download, polling, worker grant or approval.
window.YingXuHubReceipts=(()=>{
  const sessions=new WeakMap(),id=v=>typeof v==='string'&&/^[a-f0-9]{32}$/.test(v),sha=v=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v),enc=v=>encodeURIComponent(String(v));
  const unknown=()=>Object.assign(Error('关联回执尚未确认，请核对原关联。'),{unknownResult:true});
  function mount(root,env){
    const {state,task,run,binding:b,api,escapeHtml:e,report}=env;
    let session=sessions.get(state);if(!session){session={drafts:new Map(),views:new Set(),refreshers:new Set(),writes:0};sessions.set(state,session);}
    const scope=[task.project_id,task.id,run.id,b.binding_id].join('/');
    let d=session.drafts.get(scope);if(!d){d={open:false,loaded:false,candidates:[],links:[],resultId:'',path:'',request:null,uncertain:false,notice:'',readRevision:0};session.drafts.set(scope,d);}
    const prefix='/api/ai-tasks/'+enc(task.id)+'/runs/'+enc(run.id),url=prefix+'/hub-calls/'+enc(b.binding_id)+'/receipts';
    let disposed=false,sequence=0;
    const alive=()=>!disposed&&root.isConnected&&state.projectId===task.project_id&&(!env.isCurrent||env.isCurrent());
    const refresher={scope,alive,onReceipt:env.onReceipt,report};
    const busy=()=>!!(state.aiToolCallBusy||state.exitBusy||state.aiCollaborationBusy||state.migrationBusy||state.uploading||state.moveBusy);
    const selected=()=>b.results.find(r=>r.remote_identity?.result_id===d.resultId);
    const button=(action,label,disabled=false,extra='')=>`<button type="button" class="button button-secondary button-small" data-hub-receipt="${action}" ${busy()||disabled?'disabled':''} ${extra}>${label}</button>`;
    function paint(){
      if(!alive())return;
      const r=selected();
      root.innerHTML=`<div class="ai-call-receipt">${button('toggle',d.open?'收起成果关联':'关联本轮成果')}${d.open?`<p class="field-hint">选择已放入本轮生成目录的文件，核对原成果摘要后收件。不会下载、重新生成或自动审核。</p>${d.notice?`<p class="ai-notice" role="status">${e(d.notice)}</p>`:''}${button('load','刷新本轮文件与关联')}${d.loaded?`<div class="ai-form-two"><label class="ai-field">原执行成果<select data-hub-receipt-field="result" ${busy()||d.request?'disabled':''}><option value="">选择成果</option>${b.results.map(r=>`<option value="${e(r.remote_identity.result_id)}" ${d.resultId===r.remote_identity.result_id?'selected':''}>${e(r.remote_identity.result_id)} · ${Number(r.size_bytes)||0}字节${sha(r.sha256)?'':' · 缺少摘要，不能关联'}</option>`).join('')}</select></label><label class="ai-field">本轮文件<select data-hub-receipt-field="path" ${busy()||d.request?'disabled':''}><option value="">选择本轮已有文件</option>${d.candidates.map(f=>`<option value="${e(f.relative_path)}" ${d.path===f.relative_path?'selected':''}>${e(f.relative_path)}</option>`).join('')}</select></label></div><div class="ai-actions">${button(d.uncertain?'reconcile':'submit',d.uncertain?'核对原关联':'确认关联',!d.uncertain&&(!r||!sha(r.sha256)||!d.path||!!d.request))}${button('new','建立新关联',d.uncertain||!d.request)}</div>${d.links.map(link=>`<p><strong>${e(link.state==='completed'?'已关联 · 待审核':link.state==='pending'?'待核对':'收件需要处理')}</strong> · ${e(link.relative_path)}${link.state==='pending'?button('resume','恢复原关联',false,`data-link="${e(link.id)}"`):''}</p>`).join('')}`:'<p class="field-hint">点击刷新读取本轮受控文件。</p>'}`:''}</div>`;
      root.onclick=event=>{const target=event.target.closest('[data-hub-receipt]');if(!target||!root.contains(target)||target.disabled)return;event.stopPropagation();void action(target.dataset.hubReceipt,target).catch(error=>{if(alive())report(error);});};
      root.oninput=root.onchange=event=>{const target=event.target,name=target.dataset.hubReceiptField;if(!name||busy()||d.request)return;event.stopPropagation();if(name==='result')d.resultId=target.value;if(name==='path')d.path=target.value;paint();};
    }
    function validate(value,request,linkId){
      const r=b.results.find(item=>item.remote_identity?.result_id===request.result_id);
      if(!value||!id(value.id)||linkId&&value.id!==linkId||value.task_id!==task.id||value.run_id!==run.id||value.binding_id!==b.binding_id||value.execution_id!==b.execution_id||value.result_id!==request.result_id||value.results_manifest_sha256!==request.results_manifest_sha256||value.relative_path!==request.relative_path||value.source_verification!=='matches_declared_sha256'||!['pending','completed','needs_attention'].includes(value.state)||!r)throw unknown();
      if(value.state==='completed'&&(!id(value.receipt_id)||!id(value.artifact_id)||value.observed_sha256!==r.sha256))throw unknown();
      return value;
    }
    function remember(value){const n=d.links.findIndex(x=>x.id===value.id);if(n<0)d.links.unshift(value);else d.links[n]=value;d.uncertain=false;d.notice=value.state==='completed'?'已关联原成果，仍需人工审核。':'原关联已记录，请核对收件状态。';}
    async function refreshParent(value){
      if(value.state!=='completed')return;
      const current=Array.from(session.refreshers).reverse().find(view=>view.scope===scope&&view.alive()&&view.onReceipt);
      if(!current)return;
      try{await current.onReceipt(value);}catch(error){d.notice='成果已关联；列表刷新失败，请稍后刷新。';if(current.alive())current.report(error);}
    }
    async function write(fn){
      if(!alive()||busy())return;
      if(env.hasPendingEdits?.()){report(Error('请先保存文稿，再关联成果。'));return;}
      sequence++;d.readRevision++;session.writes++;state.aiToolCallBusy=true;for(const view of session.views)view();env.onBusyChanged?.();
      try{await fn();}finally{session.writes--;state.aiToolCallBusy=session.writes>0;for(const view of session.views)view();env.onBusyChanged?.();}
    }
    async function load(){
      const current=++sequence;
      const revision=++d.readRevision;
      const response=await Promise.allSettled([api(prefix+'/candidates'),api(url)]);
      if(!alive()||current!==sequence||revision!==d.readRevision)return;
      if(response[0].status==='fulfilled')d.candidates=response[0].value.files||[];else d.notice='本轮文件读取失败，请刷新。';
      if(response[1].status==='fulfilled'){
        const links=[];
        for(const value of response[1].value.items||[]){validate(value,{result_id:value.result_id,results_manifest_sha256:b.results_manifest_sha256,relative_path:value.relative_path});links.push(value);}
        d.links=links;
      }else d.notice='关联记录读取失败，请刷新。';
      d.loaded=true;paint();
    }
    async function action(name,target){
      if(!alive()||busy())return;
      if(name==='toggle'){d.open=!d.open;sequence++;paint();return;}
      if(name==='load')return load();
      if(name==='new'){if(d.uncertain)return;d.request=null;d.notice='';paint();return;}
      if(name==='submit'||name==='reconcile'){
        if(env.hasPendingEdits?.()){report(Error('请先保存文稿，再关联成果。'));return;}
        if(!d.request){const r=selected();if(!r||!sha(r.sha256)||!sha(b.results_manifest_sha256)||!d.path)return;d.request={idempotency_key:window.crypto.randomUUID(),result_id:r.remote_identity.result_id,results_manifest_sha256:b.results_manifest_sha256,relative_path:d.path};}
        const original=d.request;
        return write(async()=>{try{const value=validate(await api(url,{method:'POST',body:original}),original);remember(value);await refreshParent(value);}
          catch(error){if(error.unknownResult||!error.status||error.status>=500){d.uncertain=true;d.notice='关联结果尚未确认，请核对原关联；不会建立新请求。';throw unknown();}throw error;}});
      }
      if(name==='resume')return write(async()=>{
        const link=d.links.find(x=>x.id===target.dataset.link);if(!link)return;
        const original={result_id:link.result_id,results_manifest_sha256:link.results_manifest_sha256,relative_path:link.relative_path};
        try{const value=validate(await api(url+'/'+enc(link.id)+'/resume',{method:'POST',body:{}}),original,link.id);remember(value);await refreshParent(value);}catch(error){d.notice='原关联尚待核对，请刷新原记录。';throw error;}
      });
    }
    session.views.add(paint);session.refreshers.add(refresher);paint();
    return {dispose(){disposed=true;sequence++;session.views.delete(paint);session.refreshers.delete(refresher);root.onclick=root.oninput=root.onchange=null;}};
  }
  return {mount};
})();
