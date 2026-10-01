'use strict';
window.YingXuMCP = (() => {
  function mount(root,{api,state,escapeHtml:e,toast,report}) {
    if(!state.bootstrap?.capabilities?.mcp_project_read || !state.projectId)return;
    root.querySelector('.mcp-connection')?.remove();
    const projectId=state.projectId,section=document.createElement('section');
    section.className='mcp-connection';
    section.innerHTML='<h3>开启 MCP 实时读取</h3><p class="field-hint">AI 调用时读取当前授权项目已保存的内容；不包含未保存草稿。文本及绑定 SKILL 正文可读；Word 和媒体暂提供信息。仅提供只读访问，不自动发送消息。</p><p data-mcp-status role="status" aria-live="polite">正在读取 MCP 状态…</p><div class="settings-buttons"><button type="button" class="button button-secondary" data-mcp-enable disabled>启用本项目 MCP</button><button type="button" class="button button-ghost" data-mcp-disable disabled>关闭 MCP</button><button type="button" class="button button-ghost" data-mcp-copy disabled>复制 MCP 配置</button></div><p class="field-hint">在支持 HTTP MCP 的本机 AI 客户端中添加复制的配置后，由该客户端调用。映序只显示本地授权状态，客户端是否已连接请在客户端确认。</p>';
    root.querySelector('.handoff-card')?.after(section);
    if(!section.isConnected)root.append(section);
    const q=selector=>section.querySelector(selector),alive=()=>section.isConnected && state.section==='context' && state.projectId===projectId;
    let status=null,busy=true;
    const current=()=>!!status?.enabled && status.project_id===projectId;
    function controls() {
      q('[data-mcp-enable]').disabled=busy || !status || current();
      q('[data-mcp-disable]').disabled=busy || !status?.enabled;
      q('[data-mcp-copy]').disabled=busy || !current();
    }
    function display(result) {
      // Only render the public status fields; connection credentials stay in memory.
      status={enabled:!!result.enabled,project_id:result.project_id,project_name:result.project_name};
      q('[data-mcp-status]').innerHTML=!status.enabled?'MCP 已关闭':`MCP 已开启 · 当前授权项目：${e(status.project_name || status.project_id || '')}${current()?'':'。当前项目尚未授权，请点击“启用本项目 MCP”切换。'}`;
    }
    async function configure(enabled) {
      if(busy || !alive() || !status || (enabled && current()) || (!enabled && !status.enabled))return;
      busy=true;controls();
      try {
        const result=await api('/api/mcp/configure',{method:'POST',body:enabled?{enabled:true,project_id:projectId}:{enabled:false}});
        if(!alive())return;display(result);toast(enabled?'已启用本项目的只读 MCP。':'MCP 已关闭。');
      }catch {
        if(alive()){status=null;q('[data-mcp-status]').textContent='MCP 操作结果暂时无法确认，请重新打开 AI 协作查看状态。';report(new Error('无法确认 MCP 授权状态，请重新打开 AI 协作。'));}
      }finally {busy=false;if(alive())controls();}
    }
    q('[data-mcp-enable]').onclick=()=>configure(true);
    q('[data-mcp-disable]').onclick=()=>configure(false);
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
