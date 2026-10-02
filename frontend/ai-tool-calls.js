'use strict';
// Opt-in tool calls share the task/run ledger; this view owns no editor or media.
window.YingXuToolCalls = (() => {
  const sessions = new WeakMap();
  const labels = {accepted:'已接受',queued:'工具排队中',submitting:'正在提交',running:'工具运行中',succeeded:'工具已完成',failed:'工具失败',unknown:'结果待核对',interrupted:'中断待核对',cancel_requested:'已申请取消',cancelled:'工具已确认取消',blocked:'暂停待核对',checking:'正在核对',not_submitted:'未提交工具'};
  const object = value => value && typeof value === 'object' && !Array.isArray(value);
  const id = value => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);
  const encode = value => encodeURIComponent(String(value));
  const makeKey = () => window.crypto?.randomUUID?.() || 'call-'+Date.now()+'-'+Math.random().toString(36).slice(2);
  const unknown = () => {const error = Error('提交结果尚未确认，请核对同一次请求，避免重复调用。');error.unknownResult=true;return error;};

  function mount(root,env) {
    const {state,api,escapeHtml:e,report,toast,task,run} = env;
    let session = sessions.get(state);
    if(!session){session={drafts:new Map(),writes:0,views:new Set()};sessions.set(state,session);}
    const scope=[state.projectId,task.id,run.id].join('/');
    let d=session.drafts.get(scope);
    if(!d){d={open:false,loaded:false,connections:[],attempts:[],total:0,nextOffset:0,loadingMore:false,connectionId:'',operationId:'',parameters:{},request:null,uncertain:false,newConnection:false,connectionDraft:{name:'',base_url:'',credential_env:'',provider:'yingxu-http-v1'},notice:'',capabilityId:'',selections:[],selectionsLoaded:false,selectionRequest:null,selectionUncertain:false};session.drafts.set(scope,d);}
    let disposed=false,readSequence=0,localBusy=false,hubView=null;
    if(!d.receiptViews)d.receiptViews=new Map();
    const alive=()=>!disposed && root.isConnected && state.projectId===task.project_id && (!state.section || state.section==='context') && (!env.isCurrent || env.isCurrent());
    const prefix='/api/ai-tasks/'+encode(task.id)+'/runs/'+encode(run.id)+'/calls';
    const selectionPrefix=prefix.replace(/\/calls$/, '/capability-selections');
    const connection=()=>d.connections.find(c=>c.id===d.connectionId);
    const operation=()=>connection()?.operations?.find(o=>o.id===d.operationId);
    const busy=()=>localBusy || state.exitBusy || state.aiCollaborationBusy || state.migrationBusy || state.uploading || state.moveBusy || (!!state.aiToolCallBusy && !localBusy);
    const button=(action,title,disabled=false,extra='')=>`<button type="button" class="button button-secondary button-small" data-call="${action}" ${busy()||disabled?'disabled':''} ${extra}>${title}</button>`;
    const field=(name,title,value,extra='')=>`<label class="ai-field">${title}<input data-call-field="${name}" value="${e(value)}" ${busy()?'disabled':''} ${extra}></label>`;

    function parameterFields(op) {
      const properties=op?.input_schema?.properties || {},required=op?.input_schema?.required || [];
      return Object.entries(properties).map(([name,schema])=>{
        const title=e(schema.title || name)+(required.includes(name)?' *':''),value=d.parameters[name] ?? schema.default ?? '';
        if(Array.isArray(schema.enum))return `<label class="ai-field">${title}<select data-call-parameter="${e(name)}" ${busy()?'disabled':''}><option value="">请选择</option>${schema.enum.map((v,n)=>`<option value="${n}" ${value===v?'selected':''}>${e(v)}</option>`).join('')}</select></label>`;
        if(schema.type==='boolean')return `<label class="ai-call-checkbox"><input type="checkbox" data-call-parameter="${e(name)}" ${value===true?'checked':''} ${busy()?'disabled':''}>${title}</label>`;
        if(schema.type==='string')return `<label class="ai-field">${title}<textarea data-call-parameter="${e(name)}" maxlength="${Math.min(16000,schema.maxLength || 16000)}" ${busy()?'disabled':''}>${e(value)}</textarea></label>`;
        if(schema.type==='number' || schema.type==='integer')return `<label class="ai-field">${title}<input type="number" data-call-parameter="${e(name)}" value="${e(value)}" step="${schema.type==='integer'?'1':'any'}" ${busy()?'disabled':''}></label>`;
        return `<p class="field-hint">${title}：此输入类型暂需通过原 AI 交接方式处理。</p>`;
      }).join('');
    }
    function editable(op) {return !!op && Object.values(op.input_schema?.properties || {}).every(s=>['string','number','integer','boolean'].includes(s.type));}
    function connectionForm() {
      if(!d.newConnection)return '';
      return `<div class="ai-call-connection"><label class="ai-field">接口类型<select data-call-field="connection.provider" ${busy()?'disabled':''}><option value="yingxu-http-v1" ${d.connectionDraft.provider==='yingxu-http-v1'?'selected':''}>直接工具 · 试用协议</option><option value="aihub-interop/1" ${d.connectionDraft.provider==='aihub-interop/1'?'selected':''}>曜核 · 只读选型</option></select></label>${field('connection.name','连接名称',d.connectionDraft.name,'maxlength="80"')}${field('connection.base_url','本机接口地址',d.connectionDraft.base_url,'placeholder="http://127.0.0.1:端口" maxlength="500"')}${field('connection.credential_env','认证环境变量名称（可选）',d.connectionDraft.credential_env,'maxlength="120"')}<p class="field-hint">这里只填写变量名称，密钥保留在本机环境中。保存不会启动或扫描服务。</p>${button('save-connection','保存连接')}</div>`;
    }
    function hubFields(c) {
      if(!c?.read_only)return '';
      return `<div class="ai-call-connection"><h5>曜核能力选型</h5><p class="field-hint">这里只保存声明快照。真正派单需要单独的来源授权，由指定工作端领取执行。</p>${field('capability_id','从曜核复制的能力 ID',d.capabilityId,'maxlength="128"')}<div class="ai-actions">${button('save-selection','保存本轮选型',!c.checked || !c.enabled || !d.capabilityId || d.selectionUncertain)}${button('new-selection','建立新选型',d.selectionUncertain || !d.selectionRequest)}${button('load-selections','查看已存选型')}</div><p class="field-hint">当前按明确的能力 ID 读取，不扫描能力目录。</p></div>`;
    }
    function selectionFields() {
      return `${d.selectionUncertain&&d.selectionRequest?button('reconcile-selection','核对本次选型保存'):''}${d.selectionsLoaded?`<details open><summary>本轮选型 · ${d.selections.length}</summary><p class="field-hint">声明与真实执行分别记录；心跳不代表生成已验证。</p>${d.selections.map(s=>`<article><strong>${e(s.name || s.key)}</strong><small> ${e(s.connection_name)} · ${e(String(s.declaration_sha256).slice(0,12))}</small><div class="ai-actions">${button('verify-selection','核对声明变化',false,`data-selection="${e(s.id)}"`)}</div></article>`).join('') || '<p class="field-hint">本轮尚无选型记录。</p>'}</details>`:''}`;
    }
    const receiptView=attemptId=>{
      if(!d.receiptViews.has(attemptId))d.receiptViews.set(attemptId,{open:false,loaded:false,candidates:[],links:[],resultId:'',path:'',confirmed:false,request:null,uncertain:false});
      return d.receiptViews.get(attemptId);
    };
    const resultId=value=>typeof value==='string' && /^[a-f0-9]{64}$/.test(value);
    const receiptUnknown=()=>{const error=Error('关联回执尚未确认，请核对原请求，不要重新生成。');error.unknownResult=true;return error;};
    function receiptFields(a){
      const v=receiptView(a.id),extra=`data-attempt="${e(a.id)}"`;
      if(a.state!=='succeeded' && !v.open)return '';
      const selected=a.results?.find(r=>r.result_id===v.resultId);
      return `<div class="ai-call-receipt">${button('receipt-open',v.open?'收起成果关联':'关联本轮成果',false,extra)}${v.open?`<p class="field-hint">文件先放入本轮生成目录，再明确关联。不会下载、重新生成或自动审核。</p>${button('receipt-load','刷新候选与关联',false,extra)}${v.loaded?`<label class="ai-field">工具结果<select data-call-receipt-field="result" ${extra} ${busy()?'disabled':''}><option value="">选择结果</option>${(a.results||[]).filter(r=>resultId(r.result_id)).map(r=>`<option value="${e(r.result_id)}" ${v.resultId===r.result_id?'selected':''}>${e(r.name || r.resource_id || '工具结果')}</option>`).join('')}</select></label><label class="ai-field">本轮文件<select data-call-receipt-field="path" ${extra} ${busy()?'disabled':''}><option value="">选择已放入的文件</option>${v.candidates.map(f=>`<option value="${e(f.relative_path)}" ${v.path===f.relative_path?'selected':''}>${e(f.relative_path)}</option>`).join('')}</select></label>${selected&&!selected.sha256?`<label class="ai-field"><span><input type="checkbox" data-call-receipt-field="confirmed" ${extra} ${v.confirmed?'checked':''} ${busy()?'disabled':''}> 我确认手动关联；工具未提供摘要，来源仍未核验</span></label>`:''}<div class="ai-actions">${v.uncertain?button('receipt-reconcile','核对本次关联',!v.request,extra):button('receipt-submit','确认关联',!selected || !v.path || (!selected.sha256&&!v.confirmed),extra)}${button('receipt-new','建立新关联',v.uncertain || !v.request,extra)}</div>${v.links.map(link=>`<p><strong>${e(link.state==='completed'?'已关联':link.state==='pending'?'待核对':'需处理收件错误')}</strong> · ${e(link.relative_path)}${link.source_verification==='manual_source_unverified'?' · 手动关联，来源未核验':''}${link.state==='pending'?button('receipt-resume','恢复原关联',false,`${extra} data-delivery="${e(link.id)}"`):''}</p>`).join('')}`:'<p class="field-hint">点击刷新读取本轮文件与历史关联。</p>'}`:''}</div>`;
    }
    function render() {
      if(!alive())return;
      hubView?.dispose();hubView=null;
      const c=connection(),op=operation();
      root.innerHTML=`<section class="ai-section ai-tool-calls"><div class="ai-section-heading"><h4>工具调用</h4>${button('toggle',d.open?'收起':'展开')}</div>${d.open?`<p class="field-hint">在本轮记录调用。直接接口与曜核只读选型按连接分别处理；素材传输与自动收录尚未接入，原 AI 交接和收件继续可用。</p>${d.notice?`<p class="ai-notice" role="status">${e(d.notice)}</p>`:''}<div class="ai-actions">${d.uncertain&&d.request?button('reconcile','核对本次提交'):''}${button('refresh','刷新连接与请求')}${button('new-connection',d.newConnection?'关闭连接设置':'添加连接')}</div>${connectionForm()}${d.loaded?`<div class="ai-call-options"><label class="ai-field">工具连接<select data-call-field="connection_id" ${busy()?'disabled':''}><option value="">选择连接</option>${d.connections.map(item=>`<option value="${e(item.id)}" ${item.id===d.connectionId?'selected':''}>${e(item.name)}${item.read_only?' · 只读选型':''}${item.enabled?'':' · 已停用'}</option>`).join('')}</select></label><div class="ai-actions">${button('check','检查连接',!c || !c.enabled)}</div></div>${c&&!c.checked?'<p class="field-hint">先检查连接，读取这个工具实际支持的操作。</p>':''}${c?.checked&&!c.read_only?`<label class="ai-field">操作<select data-call-field="operation_id" ${busy()?'disabled':''}><option value="">选择操作</option>${(c.operations || []).map(item=>`<option value="${e(item.id)}" ${item.id===d.operationId?'selected':''}>${e(item.name || item.id)}</option>`).join('')}</select></label>${op?parameterFields(op):''}<div class="ai-actions">${button('submit','提交本次请求',d.uncertain || !editable(op) || !c.enabled)}${button('new-request','建立新尝试',d.uncertain || !d.request)}</div>`:''}${hubFields(c)}${selectionFields()}${!d.connections.length?'<p class="field-hint">还没有工具连接。添加后检查它支持的操作。</p>':''}<div class="ai-call-attempts">${d.attempts.map(a=>`<article><div><strong>${e(labels[a.state] || a.state)}</strong><small>请求 ${e(String(a.request_id || a.id).slice(0,12))}</small></div>${a.notice?`<p>${e(a.notice)}</p>`:''}${a.results?.length?`<p>${a.results.map(result=>e(result.name || result.description || result.kind || '结果说明')).join(' · ')}</p>`:''}<div class="ai-actions">${button('get','刷新记录',false,`data-attempt="${e(a.id)}"`)}${button('query','核对工具状态',!(a.supports?.query || a.supports?.lookup) || ['accepted','submitting','succeeded','failed','cancelled','not_submitted'].includes(a.state),`data-attempt="${e(a.id)}"`)}${a.supports?.cancel?button('cancel','申请取消',['succeeded','failed','cancelled'].includes(a.state),`data-attempt="${e(a.id)}"`):''}</div>${receiptFields(a)}</article>`).join('') || '<p class="field-hint">本轮还没有工具请求。</p>'}</div>${d.total>d.attempts.length?`<div class="ai-actions"><span class="field-hint">已显示 ${d.attempts.length} / ${d.total} 条</span>${button('more','加载更早记录',d.loadingMore)}</div>`:''}`:'<p class="field-hint">正在读取连接与请求…</p>'}`:''}</section>`;
      root.onclick=event=>{const target=event.target.closest('[data-call]');if(!target || !root.contains(target) || target.disabled)return;event.stopPropagation();void action(target.dataset.call,target).catch(error=>{if(alive())report(error);});};
      if(d.open && state.bootstrap?.capabilities?.ai_hub_source_execution && window.YingXuHubCalls){
        const slot=document.createElement('div');root.querySelector('.ai-tool-calls').append(slot);
        hubView=window.YingXuHubCalls.mount(slot,{...env,isCurrent:alive,onBusyChanged:()=>{
          if(state.aiToolCallBusy){if(alive())root.querySelectorAll('[data-call], [data-call-field], [data-call-parameter], [data-call-receipt-field]').forEach(control=>{control.disabled=true;});}
          else for(const view of Array.from(session.views))view();
          env.onBusyChanged?.();
        }});
      }
      root.oninput=root.onchange=event=>{
        if(busy())return;
        const target=event.target;
        if(target.dataset.callReceiptField){
          const v=receiptView(target.dataset.attempt),name=target.dataset.callReceiptField;
          if(name==='result'){v.resultId=target.value;v.confirmed=false;render();}
          else if(name==='path'){v.path=target.value;render();}
          else if(name==='confirmed'){v.confirmed=target.checked;render();}
          return;
        }
        if(target.dataset.callField){
          const name=target.dataset.callField;
          if(name.startsWith('connection.'))d.connectionDraft[name.slice(11)]=target.value;
          else if(name==='connection_id'){d.connectionId=target.value;d.operationId='';d.parameters={};render();}
          else if(name==='operation_id'){d.operationId=target.value;d.parameters={};render();}
          else if(name==='capability_id'){d.capabilityId=target.value;const save=root.querySelector('[data-call="save-selection"]'),c=connection();if(save)save.disabled=busy() || !c?.checked || !c.enabled || !d.capabilityId || d.selectionUncertain;}
        }
        if(target.dataset.callParameter){
          const name=target.dataset.callParameter,schema=operation()?.input_schema?.properties?.[name];if(!schema)return;
          if(Array.isArray(schema.enum)) {if(target.value==='')delete d.parameters[name];else d.parameters[name]=schema.enum[Number(target.value)];}
          else if(schema.type==='boolean')d.parameters[name]=target.checked;
          else if(schema.type==='number' || schema.type==='integer'){if(target.value==='')delete d.parameters[name];else d.parameters[name]=Number(target.value);}
          else d.parameters[name]=target.value;
        }
      };
    }
    function remember(value) {
      if(!object(value) || !id(value.id) || value.task_id!==task.id || value.run_id!==run.id)throw unknown();
      const index=d.attempts.findIndex(a=>a.id===value.id);if(index<0){d.attempts.unshift(value);d.total++;}else d.attempts[index]=value;
      return value;
    }
    async function load() {
      const sequence=++readSequence;
      const results=await Promise.allSettled([api('/api/ai-connections'),api(prefix)]);
      if(!alive() || sequence!==readSequence || !d.open)return;
      if(results[0].status==='fulfilled' && Array.isArray(results[0].value?.items))d.connections=results[0].value.items;
      else d.notice='连接读取失败，请刷新重试。';
      if(results[1].status==='fulfilled' && Array.isArray(results[1].value?.items)){const page=results[1].value;d.attempts=page.items.filter(a=>a.task_id===task.id && a.run_id===run.id);d.total=Number.isInteger(page.total)?page.total:d.attempts.length;d.nextOffset=page.items.length;}
      else d.notice='请求记录读取失败，请刷新重试。';
      d.loaded=true;render();
    }
    async function write(operation) {
      if(!alive() || busy())return;
      localBusy=true;session.writes++;state.aiToolCallBusy=true;render();env.onBusyChanged?.();
      try {await operation();} catch(error) {if(alive())report(error);} finally {session.writes--;localBusy=false;state.aiToolCallBusy=session.writes>0;for(const view of session.views)view();env.onBusyChanged?.();}
    }
    async function action(name,target) {
      if(!alive() || busy())return;
      if(name==='toggle'){d.open=!d.open;readSequence++;render();if(d.open && !d.loaded)await load();return;}
      if(name==='refresh'){d.notice='';return load();}
      if(name==='more'){
        if(d.loadingMore || d.nextOffset>=d.total)return;
        const sequence=++readSequence,offset=d.nextOffset;d.loadingMore=true;render();
        try {const page=await api(prefix+'?limit=48&offset='+offset);if(!alive() || sequence!==readSequence || !d.open)return;if(!Array.isArray(page?.items))throw Error('请求记录读取失败，请刷新重试。');const known=new Set(d.attempts.map(a=>a.id));d.attempts.push(...page.items.filter(a=>a.task_id===task.id && a.run_id===run.id && !known.has(a.id)));d.nextOffset=offset+page.items.length;if(Number.isInteger(page.total))d.total=page.total;}
        finally {d.loadingMore=false;render();}return;
      }
      if(name==='new-connection'){d.newConnection=!d.newConnection;render();return;}
      if(name==='new-request'){if(d.uncertain)return;d.request=null;d.notice='已准备新尝试。提交前请核对工具和参数。';render();return;}
      if(name==='new-selection'){if(d.selectionUncertain)return;d.selectionRequest=null;d.notice='已准备新选型，原快照继续保留。保存时读取当前声明。';render();return;}
      if(name.startsWith('receipt-')){
        const a=d.attempts.find(a=>a.id===target.dataset.attempt);if(!a)return;
        const v=receiptView(a.id),url=prefix+'/'+encode(a.id)+'/receipts';
        if(name==='receipt-open'){v.open=!v.open;render();return;}
        if(name==='receipt-new'){if(v.uncertain)return;v.request=null;d.notice='已准备新的明确关联请求；原回执继续保留。';render();return;}
        if(name==='receipt-load')return write(async()=>{
          const pages=await Promise.allSettled([api(url),api(prefix.replace(/\/calls$/,'/candidates'))]);
          if(!alive())return;
          if(pages[0].status==='fulfilled' && Array.isArray(pages[0].value?.items))v.links=pages[0].value.items.filter(l=>l.attempt_id===a.id&&l.task_id===task.id&&l.run_id===run.id);
          else throw Error('关联记录读取失败，请重试。');
          if(pages[1].status==='fulfilled' && Array.isArray(pages[1].value?.files))v.candidates=pages[1].value.files;
          else {v.candidates=[];d.notice='本轮候选暂不可读取，历史关联仍可核对。';}
          v.loaded=true;
        });
        const rememberLink=(value,expectedResult,expectedLink)=>{
          if(!object(value)||!id(value.id)||expectedLink&&value.id!==expectedLink||value.task_id!==task.id||value.run_id!==run.id||value.attempt_id!==a.id||!resultId(value.result_id)||value.result_id!==expectedResult||!['pending','completed','needs_attention'].includes(value.state))throw receiptUnknown();
          if(value.state==='completed' && (!id(value.receipt_id)||!id(value.artifact_id)||!resultId(value.observed_sha256)))throw receiptUnknown();
          const index=v.links.findIndex(l=>l.id===value.id);if(index<0)v.links.push(value);else v.links[index]=value;
          if(alive())d.notice=value.state==='completed'?'已关联原成果；审核采用仍需确认。':'原关联记录已保留，请核对收件情况。';
        };
        const refreshReceived=async value=>{
          if(value.state!=='completed' || !alive() || !env.onReceipt)return;
          try {await env.onReceipt(value);}
          catch(error){if(alive()){d.notice='成果已关联；列表刷新失败，请刷新任务核对。';report(error);}}
        };
        if(name==='receipt-resume'){
          if(env.hasPendingEdits?.())return toast('请先保存文稿，再恢复成果关联。','info');
          const link=v.links.find(l=>l.id===target.dataset.delivery);if(!link || link.state!=='pending')return;
          return write(async()=>{const value=await api(url+'/'+encode(link.id)+'/resume',{method:'POST',body:{}});rememberLink(value,link.result_id,link.id);await refreshReceived(value);});
        }
        if(name==='receipt-submit' || name==='receipt-reconcile'){
          if(env.hasPendingEdits?.())return toast('请先保存文稿，再关联成果。','info');
          if(name==='receipt-submit'){
            if(v.uncertain)return;
            const selected=a.results?.find(r=>r.result_id===v.resultId);
            if(!selected || !v.path || (!selected.sha256&&!v.confirmed))return;
            const payload={result_id:v.resultId,relative_path:v.path,confirmed_unverified_source:!selected.sha256&&v.confirmed},signature=JSON.stringify(payload);
            if(!v.request || v.request.signature!==signature)v.request={signature,body:{...payload,idempotency_key:makeKey()}};
          }
          const request=v.request;if(!request)return;
          return write(async()=>{try{const value=await api(url,{method:'POST',body:request.body});rememberLink(value,request.body.result_id);v.uncertain=false;await refreshReceived(value);}
            catch(error){if(error.unknownResult || !error.status){v.uncertain=true;throw receiptUnknown();}throw error;}});
        }
        return;
      }
      if(name==='save-connection')return write(async()=>{
        const body={...d.connectionDraft,enabled:true,expected_revision:0};
        const value=await api('/api/ai-connections',{method:'POST',body});
        if(!object(value)||!id(value.id))throw unknown();
        if(!alive())return;d.connectionId=value.id;d.newConnection=false;d.notice='连接已保存；请检查它实际支持的操作。';await load();
      });
      if(name==='check'){
        const selected=d.connectionId;if(!selected)return;
        return write(async()=>{const value=await api('/api/ai-connections/'+encode(selected)+'/check',{method:'POST',body:{}});if(!alive() || d.connectionId!==selected)return;if(!object(value) || value.id!==selected)throw unknown();d.connections=d.connections.map(c=>c.id===selected?value:c);d.notice=value.read_only?'已读取曜核连接；当前只支持保存能力选型。':'已读取工具声明；这不会执行生成。';});
      }
      if(name==='load-selections')return write(async()=>{const page=await api(selectionPrefix);if(!alive())return;if(!Array.isArray(page?.items))throw Error('选型记录读取失败，请重试。');d.selections=page.items.filter(s=>s.task_id===task.id && s.run_id===run.id);d.selectionsLoaded=true;});
      if(name==='save-selection' || name==='reconcile-selection'){
        let request=d.selectionRequest;
        if(name==='save-selection'){
          const c=connection();if(!c?.read_only || !c.checked || !c.enabled || !d.capabilityId || d.selectionUncertain)return;
          const body={connection_id:c.id,connection_revision:c.revision,source_connection_revision:c.connection_revision,capability_id:d.capabilityId};
          const signature=JSON.stringify(body);if(!request || request.signature!==signature)request={signature,body:{...body,idempotency_key:makeKey()}};
          d.selectionRequest=request;
        }
        if(!request)return;
        return write(async()=>{
          try{const value=await api(selectionPrefix,{method:'POST',body:request.body});if(!object(value) || !id(value.id) || value.task_id!==task.id || value.run_id!==run.id || value.execution_allowed!==false)throw unknown();
            if(!d.selections.some(s=>s.id===value.id))d.selections.unshift(value);d.selectionsLoaded=true;d.selectionUncertain=false;if(alive())d.notice='本轮选型已保存；未执行生成，仍需工作端和执行版本适配。';
          }catch(error){if(error.unknownResult || !error.status){d.selectionUncertain=true;throw unknown();}throw error;}
        });
      }
      if(name==='verify-selection'){
        const s=d.selections.find(s=>s.id===target.dataset.selection);if(!s)return;
        return write(async()=>{const value=await api(selectionPrefix+'/'+encode(s.id)+'/verify',{method:'POST',body:{}});if(!alive())return;if(value?.id!==s.id || value.task_id!==task.id || value.run_id!==run.id || value.declaration_matches!==true || value.execution_allowed!==false)throw Error('选型核对响应无效。');d.notice='声明仍匹配原选型；这不表示执行版本已锁定或生成已验证。';});
      }
      if(name==='submit' || name==='reconcile'){
        if(env.hasPendingEdits?.())return toast('请先保存文稿，再提交工具请求。','info');
        let request=d.request;
        if(!d.uncertain){
          const op=operation(),c=connection();if(!editable(op) || !c?.enabled || c.read_only)return;
          const parameters={};for(const [name,schema] of Object.entries(op.input_schema.properties || {})){if(Object.hasOwn(d.parameters,name))parameters[name]=d.parameters[name];else if(Object.hasOwn(schema,'default'))parameters[name]=schema.default;else if(schema.type==='boolean' && op.input_schema.required?.includes(name))parameters[name]=false;}
          const payload={connection_id:c.id,operation_id:op.id,parameters},signature=JSON.stringify(payload);
          if(!request || request.signature!==signature)request={signature,body:{...payload,idempotency_key:makeKey()}};
          d.request=request;
        }
        if(!request)return;
        return write(async()=>{
          try {const value=await api(prefix,{method:'POST',body:request.body});remember(value);if(d.request===request)d.uncertain=false;if(alive())d.notice='本次请求已记录。工具完成后仍需收件与审核。';}
          catch(error){if(error.unknownResult || !error.status){d.uncertain=true;d.notice='提交结果尚未确认。核对会使用原连接、操作和参数，不创建新生成请求。';throw unknown();}throw error;}
        });
      }
      if(['get','query','cancel'].includes(name)){
        const attempt=d.attempts.find(a=>a.id===target.dataset.attempt);if(!attempt)return;
        const url=prefix+'/'+encode(attempt.id);
        return write(async()=>{const value=await api(url+(name==='get'?'':'/'+name),name==='get'?{}:{method:'POST',body:{}});remember(value);if(alive())d.notice=name==='cancel'?'取消请求已记录；以工具确认的状态为准。':'已读取请求记录，未重新生成。';});
      }
    }
    session.views.add(render);render();
    return {dispose(){disposed=true;readSequence++;hubView?.dispose();hubView=null;session.views.delete(render);root.onclick=root.oninput=root.onchange=null;},isBusy:()=>localBusy};
  }
  return {mount};
})();
