'use strict';
window.YingXuAutomaticUpdates = (() => {
  function start({api,openSettings,document=window.document,setTimer=setTimeout,clearTimer=clearTimeout}) {
    let timer=null,closed=false,busy=false,notice=null,last='',hidden='';
    const stop=()=>{closed=true;if(timer!==null)clearTimer(timer);notice?.remove();};
    const schedule=ms=>{if(!closed)timer=setTimer(()=>{timer=null;poll();},ms);};
    const render=info=>{
      const update=info.update || info.plan || info;
      const phase=update.state || info.state;
      const manual=phase==='manual_required' && (update.update_kind || info.update_kind)==='manual';
      const available=phase==='ready' || phase==='planned' || manual ||
        ['available','manual_required'].includes(phase) && (info.update_available || update.update_available);
      if(!available){notice?.remove();notice=null;last='';return;}
      const key=`${update.latest_version || info.latest_version || ''}:${update.latest_build || info.latest_build || ''}:${phase}`;
      if(key===hidden){notice?.remove();notice=null;last='';return;}
      if(key===last)return;last=key;
      if(!notice){notice=document.createElement('div');notice.className='automatic-update-notice';notice.setAttribute('role','status');document.body.appendChild(notice);}
      notice.replaceChildren();
      const label=document.createElement('span');label.textContent=manual?'发布构建需手动核对，可在软件内查看详情。':phase==='ready'?'新版已下载并验证，可保存工作后安装。':(update.update_kind || info.update_kind)==='build'?'发现映序修补构建，可在软件内查看更新。':'发现映序新版本，可在软件内查看更新。';
      const action=document.createElement('button');action.className='button button-secondary button-small';action.textContent=phase==='ready'?'安装更新':'查看更新';action.onclick=()=>openSettings();
      const dismiss=document.createElement('button');dismiss.className='button button-ghost button-small';dismiss.textContent='稍后';dismiss.onclick=()=>{hidden=key;notice.remove();notice=null;};
      notice.append(label,action,dismiss);
    };
    async function poll(){
      if(closed || busy)return;busy=true;
      try{const info=await api('/api/updates/automatic/status');if(closed)return;render(info);const phase=(info.update || info.plan || info).state;schedule(['planning','downloading','checking'].includes(phase)||info.busy?5000:300000);}
      catch{schedule(300000);}finally{busy=false;}
    }
    const ready=api('/api/updates/automatic/start',{method:'POST',body:{}}).then(()=>{if(!closed)schedule(12000);}).catch(()=>{if(!closed)schedule(300000);});
    return {stop,poll,ready};
  }
  return {start};
})();
