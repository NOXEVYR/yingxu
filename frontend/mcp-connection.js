'use strict';
window.YingXuMCP = (() => {
  function mount(root,{api,state,escapeHtml:e,toast,report}) {
    if(!state.bootstrap?.capabilities?.mcp_project_read || !state.projectId)return;
    root.querySelector('.mcp-connection')?.remove();
    const projectId=state.projectId,section=document.createElement('section');
    section.className='mcp-connection';
    section.innerHTML='<h3>开启 MCP 实时读取</h3><p class="field-hint">AI 调用时读取当前授权项目已保存的内容；不包含未保存草稿。文本及绑定 SKILL 正文可读；Word 和媒体暂提供信息。仅提供只读访问，不自动发送消息。</p><p data-mcp-status role="status" aria-live="polite">正在读取 MCP 状态…</p><div class="settings-buttons"><button type="button" class="button button-secondary" data-mcp-enable disabled>启用本项目 MCP</button><button type="button" class="button button-ghost" data-mcp-disable disabled>关闭 MCP</button><button type="button" class="button button-ghost" data-mcp-copy disabled>复制 MCP 配置</button></div><details class="mcp-address"><summary>连接地址</summary><p class="field-hint">已保存地址：<code data-mcp-endpoint>正在读取…</code></p><label>完整本机 HTTP 地址<input type="url" data-mcp-address maxlength="256" placeholder="http://127.0.0.1:8791/mcp" autocomplete="off" spellcheck="false" disabled></label><p class="field-hint" data-mcp-default>清空后应用，可恢复默认地址。</p><div class="settings-buttons"><button type="button" class="button button-secondary" data-mcp-apply disabled>应用地址</button><button type="button" class="button button-ghost" data-mcp-default-button disabled>恢复默认</button></div><p class="field-hint" data-mcp-address-state role="status" aria-live="polite">正在读取已保存设置…</p></details><p class="field-hint">在支持 HTTP MCP 的本机 AI 客户端中添加复制的配置后，由该客户端调用。映序只显示本地授权状态，客户端是否已连接请在客户端确认。</p>';
    root.querySelector('.handoff-card')?.after(section);
    if(!section.isConnected)root.append(section);
    const q=selector=>section.querySelector(selector),alive=()=>section.isConnected && state.section==='context' && state.projectId===projectId;
    let status=null,busy=true,savedDraft='',addressUnconfirmed=false;
    const current=()=>!!status?.enabled && status.project_id===projectId;
    const localAddress=value=>{try{const url=new URL(value);return url.protocol==='http:' && ['127.0.0.1','localhost'].includes(url.hostname) && !url.username && !url.password && !url.search && !url.hash;}catch{return false;}};
    const normalized=value=>{const url=new URL(value);if(url.hostname==='localhost')url.hostname='127.0.0.1';return url.href;};
    const addressChanged=()=>q('[data-mcp-address]').value.trim()!==savedDraft;
    function addressMessage() {
      q('[data-mcp-address-state]').textContent=addressUnconfirmed?'地址未应用；已保留你的输入，请核对当前状态后重试。':status?.address_error?'保存的连接地址暂不可用，请检查地址或端口冲突。':status?.enabled && !current()?'另一个项目正在使用 MCP。请先明确启用本项目，再应用地址。':addressChanged()?'输入尚未应用。':status?.custom_endpoint?'已保存自定义地址。':'使用默认地址。';
    }
    function controls() {
      q('[data-mcp-enable]').disabled=busy || !status || current();
      q('[data-mcp-disable]').disabled=busy || !status?.enabled;
      q('[data-mcp-copy]').disabled=busy || !current();
      q('[data-mcp-address]').disabled=busy || !status;
      q('[data-mcp-apply]').disabled=busy || !status || (!!status.enabled && !current()) || !addressChanged();
      q('[data-mcp-default-button]').disabled=busy || !status || (!status.custom_endpoint && !q('[data-mcp-address]').value);
    }
    function display(result,keepDraft=false) {
      // Only render the public status fields; connection credentials stay in memory.
      if(!result || typeof result.enabled!=='boolean' || !localAddress(result.endpoint) || result.enabled && typeof result.project_id!=='string')throw new Error('Invalid MCP status');
      const defaultEndpoint=localAddress(result.default_endpoint)?result.default_endpoint:result.endpoint;
      status={enabled:result.enabled,project_id:result.project_id,project_name:result.project_name,endpoint:result.endpoint,default_endpoint:defaultEndpoint,custom_endpoint:result.custom_endpoint===true,address_error:typeof result.address_error==='string' && !!result.address_error};
      savedDraft=status.custom_endpoint?status.endpoint:'';
      if(!keepDraft)q('[data-mcp-address]').value=savedDraft;
      q('[data-mcp-address]').placeholder=defaultEndpoint;
      q('[data-mcp-endpoint]').textContent=status.endpoint;
      q('[data-mcp-default]').textContent='清空后应用，可恢复默认地址：'+defaultEndpoint;
      q('[data-mcp-status]').innerHTML=!status.enabled?'MCP 已关闭':`MCP 已开启 · 当前授权项目：${e(status.project_name || status.project_id || '')}${current()?'':'。当前项目尚未授权，请点击“启用本项目 MCP”切换。'}`;
      addressMessage();
    }
    async function configure(enabled,endpoint) {
      const applying=endpoint!==undefined;
      if(busy || !alive() || !status || applying && (status.enabled && !current() || !addressChanged()) || !applying && (enabled && current() || !enabled && !status.enabled))return;
      if(applying && endpoint && (!localAddress(endpoint) || endpoint.length>256))return toast('请输入本机 HTTP 地址，例如 '+status.default_endpoint+'；清空可恢复默认。','info');
      const body=enabled?{enabled:true,project_id:projectId}:{enabled:false};if(applying)body.endpoint=endpoint;
      const inputBefore=q('[data-mcp-address]').value,expectedEndpoint=applying?normalized(endpoint || status.default_endpoint):null;
      busy=true;controls();
      try {
        const result=await api('/api/mcp/configure',{method:'POST',body});
        if(!alive())return;
        if(!result || result.enabled!==enabled || enabled && result.project_id!==projectId || applying && (!localAddress(result.endpoint) || normalized(result.endpoint)!==expectedEndpoint || !!result.address_error))throw new Error('Unconfirmed MCP operation');
        display(result,!applying || q('[data-mcp-address]').value!==inputBefore);addressUnconfirmed=false;addressMessage();toast(applying?'MCP 地址已应用。':enabled?'已启用本项目的只读 MCP。':'MCP 已关闭。');
      }catch {
        if(!alive())return;
        addressUnconfirmed=applying;
        try{const result=await api('/api/mcp/status');if(!alive())return;display(result,true);addressMessage();report(new Error(applying?'MCP 地址未确认应用，已重新读取当前状态；你的输入已保留。':'MCP 操作未确认完成，已重新读取当前授权状态。'));}
        catch{if(alive()){status=null;q('[data-mcp-status]').textContent='MCP 操作结果暂时无法确认，请重新打开 AI 协作查看状态。';addressMessage();report(new Error('无法确认 MCP 授权和地址状态，请重新打开 AI 协作。'));}}
      }finally {busy=false;if(alive())controls();}
    }
    q('[data-mcp-enable]').onclick=()=>configure(true);
    q('[data-mcp-disable]').onclick=()=>configure(false);
    q('[data-mcp-address]').oninput=()=>{if(!alive())return;addressUnconfirmed=false;addressMessage();controls();};
    q('[data-mcp-apply]').onclick=()=>configure(!!status?.enabled,q('[data-mcp-address]').value.trim());
    q('[data-mcp-default-button]').onclick=()=>{if(busy || !alive() || !status)return;q('[data-mcp-address]').value='';addressUnconfirmed=false;addressMessage();controls();};
    q('[data-mcp-copy]').onclick=async()=>{
      if(busy || !alive() || !current())return;
      busy=true;controls();let result=null,configText='';
      try {
        if(!navigator.clipboard?.writeText)throw new Error('Clipboard unavailable');
        result=await api('/api/mcp/connection',{method:'POST',body:{}});
        if(!alive() || result.project_id!==projectId || result.read_only!==true || !result.config)return;
        configText=JSON.stringify(result.config,null,2);
        // The shared copy helper has a textarea fallback; credentials must never use it.
        await navigator.clipboard.writeText(configText);
        if(alive())toast('已复制 MCP 配置；请在 AI 客户端中添加。');
      }catch {
        if(alive())report(new Error('复制 MCP 配置失败，请检查系统剪贴板权限和 MCP 授权状态。'));
      }finally {result=null;configText='';busy=false;if(alive())controls();}
    };
    api('/api/mcp/status').then(result=>{if(alive())display(result);}).catch(()=>{
      if(alive()){q('[data-mcp-status]').textContent='暂时无法读取 MCP 状态，请重新打开 AI 协作。';report(new Error('暂时无法读取 MCP 状态。'));}
    }).finally(()=>{busy=false;if(alive())controls();});
  }
  return {mount};
})();
