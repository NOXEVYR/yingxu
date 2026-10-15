(() => {
  'use strict';

  function mount(player,{tab,api,container,onEnter=()=>{},onStateChange=()=>{}}) {
    const panel=document.createElement('section');panel.className='video-frame-inspector';
    panel.innerHTML=`
      <div class="frame-tool-heading"><strong>逐帧检查</strong><span class="frame-status" role="status" aria-live="polite">正在检查能力…</span></div>
      <div class="frame-index-actions"><button type="button" data-frame="start" hidden>建立真实帧索引</button><button type="button" data-frame="retry-capability" hidden>重试能力检查</button><button type="button" data-frame="enter" hidden>从第 0 帧进入检查</button></div>
      <div class="frame-locate-row" hidden><label>帧号<input type="number" min="0" step="1" value="0" aria-label="展示帧序号（从 0 开始）"></label><button type="button" data-frame="locate">暂停并定位</button></div>
      <div class="frame-current-actions" hidden><button type="button" data-frame="previous">上一帧</button><output class="frame-position">等待定位</output><button type="button" data-frame="next">下一帧</button><button type="button" data-frame="resume">继续播放（近似定位）</button></div>
      <button type="button" class="frame-cancel" data-frame="cancel" hidden>取消索引</button>
      <section class="frame-export">
        <div class="frame-export-title"><strong>严格源帧导出</strong><span>FFV1 · 仅视频 · 不含音频</span></div>
        <div class="frame-actions"><button type="button" data-frame="set-first" disabled>当前帧设为首帧</button><button type="button" data-frame="set-last" disabled>当前帧设为尾帧</button><output class="frame-range">尚未设定范围</output><button type="button" data-frame="export" disabled>导出所选源帧</button></div>
        <div class="frame-export-result" role="status" aria-live="polite"></div>
        <details class="frame-technical-details"><summary>导出与定位说明</summary><p>后台索引逐帧解码并核对完整顺序像素；期间可继续播放，完成不会自动暂停。进入静帧检查后，关键帧定位和邻帧缓存会加速，校验失败时回退起点。返回视频只按相对时间近似定位，并保留索引。关闭检查或标签会清理本次索引。</p><p>首尾两帧均包含，每次最多 10000 帧。输出 MKV 或 NUT；时间基无法精确保留时会拒绝导出。FFV1 文件面向支持该编码的编辑工具，普通播放器可能无法播放。结果包含源帧清单，不含音频、字幕或附件；末帧持续时长由容器处理，不承诺精确结束时刻。</p></details>
      </section>`;
    container.append(panel);
    const $=query=>panel.querySelector(query),controller=new AbortController(),signal=controller.signal;
    const overlay=document.createElement('div');overlay.className='frame-overlay';overlay.hidden=true;
    const image=document.createElement('img');image.alt='解码静帧';
    const label=document.createElement('div');label.className='frame-overlay-label';
    const exit=document.createElement('button');exit.type='button';exit.textContent='返回视频（近似定位）';
    overlay.append(image,label,exit);player.screen.append(overlay);
    let disposed=false,session=null,job=null,count=0,current=null,generation=0,url=null,watch=0;
    let available=false,capabilityLoading=false,busy=false,inspecting=false,exporting=false,first=null,last=null;
    const status=text=>{$('.frame-status').textContent=text;};
    const snapshot=()=>({available,capabilityLoading,session:!!session,busy,inspecting,exporting,current:current?{ordinal:current.ordinal}:null,count});
    function controls(){
      const currentOrdinal=current?.ordinal;
      $('[data-frame=start]').hidden=!available||!!session;
      $('[data-frame=start]').disabled=!available||capabilityLoading||busy||!!session;
      $('[data-frame=retry-capability]').hidden=capabilityLoading||available;
      $('[data-frame=retry-capability]').disabled=capabilityLoading||busy;
      $('[data-frame=enter]').hidden=!session||inspecting;
      $('[data-frame=enter]').disabled=busy||!count;
      $('.frame-locate-row').hidden=!session;
      $('input').disabled=busy||!count;
      $('[data-frame=locate]').disabled=busy||!count;
      $('.frame-current-actions').hidden=!inspecting;
      $('[data-frame=previous]').disabled=busy||!current||currentOrdinal===0;
      $('[data-frame=next]').disabled=busy||!current||currentOrdinal===count-1;
      $('.frame-position').textContent=current?`第 ${current.ordinal} / ${count-1} 帧`:'等待定位';
      $('[data-frame=resume]').disabled=busy&&!exporting;
      $('[data-frame=cancel]').hidden=!session&&!busy;
      $('[data-frame=cancel]').disabled=false;
      $('[data-frame=cancel]').textContent=exporting?'取消严格导出并关闭检查':busy&&!session?'取消索引':'关闭索引与检查';
      for(const action of ['set-first','set-last'])$(`[data-frame=${action}]`).disabled=busy||!current||!inspecting;
      $('[data-frame=export]').disabled=busy||!session||!inspecting||first===null||last===null||first>last;
      $('.frame-range').textContent=first===null&&last===null?'尚未设定范围':`[${first??'—'}, ${last??'—'}] · 两端包含${first!==null&&last!==null&&last>=first?` · ${last-first+1} 帧`:''}`;
      onStateChange(snapshot());
    }
    async function poll(id,token){
      while(!disposed&&token===generation){
        const value=await api(`/api/video/frames/jobs/${id}`);
        if(disposed||token!==generation)return null;
        if(['ready','done','error','cancelled'].includes(value.state)){
          if(value.state==='error')throw Error(value.error||'帧检查失败。');
          return value;
        }
        status(value.kind==='index'?`${value.phase} · 已索引 ${value.frames} 帧 · 像素核验 ${value.verified_frames??0} 帧`:value.phase);
        if(value.elapsed!==undefined)status(`${$('.frame-status').textContent} · ${value.elapsed} 秒${value.bytes?` · 校验 ${(value.bytes/1048576).toFixed(1)} MiB`:''}`);
        await new Promise(resolve=>setTimeout(resolve,250));
      }
      return null;
    }
    function clearPicture(){
      inspecting=false;overlay.hidden=true;image.removeAttribute('src');player.screen.classList.remove('frame-inspecting');
      if(url){URL.revokeObjectURL(url);url=null;}current=null;
    }
    async function cancel({keepMessage=false}={}){
      const token=++generation;clearTimeout(watch);const index=session,active=job;session=null;job=null;count=0;busy=false;exporting=false;first=null;last=null;
      clearPicture();controls();
      if(!keepMessage)status('索引与静帧检查已关闭；视频播放状态保持不变。');
      let failure=null;
      for(const [kind,id] of [['jobs',active],['index',index]])if(id){
        try{const result=await api(`/api/video/frames/${kind}/${id}/cancel`,{method:'POST',body:{}});if(result.error)failure=result.error;}
        catch(error){failure=error.message;}
      }
      if(failure&&!disposed&&token===generation)status('界面已退出，后台取消或清理尚未确认：'+failure);
    }
    function watchSource(token){
      clearTimeout(watch);
      watch=setTimeout(async()=>{
        if(disposed||token!==generation||!session)return;
        try{await api(`/api/video/frames/jobs/${session}`);if(!disposed&&token===generation)watchSource(token);}
        catch(error){if(!disposed&&token===generation){status(error.message);cancel({keepMessage:true});}}
      },1500);
    }
    async function checkCapability(){
      if(disposed||capabilityLoading)return;
      capabilityLoading=true;controls();status('正在检查本机逐帧能力…');
      try{
        const value=await api('/api/video/frames/capability');if(disposed)return;
        available=!!value.available;status(available?'可建立真实帧索引；后台处理中视频仍可播放。':value.reason||'当前环境暂不可用逐帧检查。');
      }catch(error){if(disposed)return;available=false;status(`能力检查失败：${error.message} 可重试。`);}
      finally{capabilityLoading=false;if(!disposed)controls();}
    }
    async function locate(ordinal){
      if(busy||!session||!Number.isInteger(ordinal)||ordinal<0||ordinal>=count){status('请输入范围内的整数帧号。');return;}
      busy=true;inspecting=true;const token=++generation;clearTimeout(watch);controls();player.screen.classList.add('frame-inspecting');player.pause();onEnter();status(`正在核对第 ${ordinal} 帧；视频已暂停。`);
      try{
        const request=await api(`/api/video/frames/index/${session}/frame`,{method:'POST',body:{ordinal}});
        if(disposed||token!==generation){await api(`/api/video/frames/jobs/${request.id}/cancel`,{method:'POST',body:{}}).catch(()=>{});return;}
        job=request.id;controls();const result=await poll(job,token);if(!result||result.state!=='done')return;
        const response=await fetch(result.image_url,{signal});
        if(!response.ok)throw Error('静帧缓存已更新或源文件已改变，请重新建立索引。');
        const blob=await response.blob();if(disposed||token!==generation)return;
        const nextUrl=URL.createObjectURL(blob),decoded=new Image();decoded.src=nextUrl;
        try{await decoded.decode();}catch(error){URL.revokeObjectURL(nextUrl);throw error;}
        if(disposed||token!==generation){URL.revokeObjectURL(nextUrl);return;}
        if(url)URL.revokeObjectURL(url);url=nextUrl;image.src=url;current=result;overlay.hidden=false;
        player.screen.classList.add('frame-inspecting');$('input').value=String(ordinal);
        label.textContent=`第 ${ordinal} 帧 · ${result.pts===null?'推算 PTS '+(result.best_effort_pts??'未知'):'源 PTS '+result.pts} × ${result.time_base} · ${result.decode_mode==='verified-keyframe'?'关键帧核验':result.decode_mode==='verified-prefix'?'起点核验':'缓存核验'}`;
        status(`已显示第 ${ordinal} 帧；源像素核验通过。视频仍暂停。`);controls();watchSource(token);
      }catch(error){if(!disposed&&token===generation){status(`定位失败：${error.message}。可重试定位；源文件变化时请重建索引。`);busy=false;job=null;clearPicture();player.screen.classList.remove('frame-inspecting');controls();watchSource(token);}}
      finally{if(token===generation){busy=false;job=null;controls();}}
    }
    async function start(){
      if(busy||session||!available)return;
      const token=++generation;busy=true;controls();status('正在建立后台帧索引；可以继续播放和审片。');
      try{
        const result=await api('/api/video/frames/index',{method:'POST',body:{source:tab.source==='external'?'external':'file',id:tab.id}});
        if(disposed||token!==generation){await api(`/api/video/frames/index/${result.id}/cancel`,{method:'POST',body:{}}).catch(()=>{});return;}
        session=result.id;controls();const ready=await poll(session,token);if(!ready||ready.state!=='ready')return;
        count=ready.count;busy=false;$('input').max=String(Math.max(0,count-1));controls();
        status(`索引已就绪 · ${count} 帧。选择帧号定位，或从第 0 帧进入检查。`);watchSource(token);
      }catch(error){if(!disposed&&token===generation){status(`索引失败：${error.message}。可再次建立索引。`);await cancel({keepMessage:true});}}
      finally{if(token===generation){busy=false;controls();}}
    }
    async function exportRange(){
      if(busy||!session||!inspecting||first===null||last===null||first>last)return;
      busy=true;exporting=true;const token=++generation;clearTimeout(watch);controls();$('.frame-export-result').replaceChildren();
      try{
        const request=await api(`/api/video/frames/index/${session}/export`,{method:'POST',body:{first,last,audio:'none'}});
        if(disposed||token!==generation){await api(`/api/video/frames/jobs/${request.id}/cancel`,{method:'POST',body:{}}).catch(()=>{});return;}
        job=request.id;const result=await poll(job,token);if(!result||result.state!=='done')return;
        const output=$('.frame-export-result');output.textContent=`已核验 ${result.count} 帧 · 两端包含 · FFV1 ${result.format.toUpperCase()} · 不含音频。 `;
        for(const [text,href,name] of [['下载源帧视频',result.download_url,`clip.${result.format}`],['下载源帧清单',result.manifest_url,'frames.json']]){
          const link=document.createElement('a');link.textContent=text;link.href=href;link.download=name;output.append(link,document.createTextNode(' '));
        }
        status('严格源帧视频已生成；原片保留。');
      }catch(error){if(!disposed&&token===generation)status(`严格导出失败：${error.message}`);}
      finally{if(token===generation){busy=false;exporting=false;job=null;controls();watchSource(token);}}
    }
    function resume(play){
      const time=current?.resume_seconds_approximate;
      if(exporting){clearPicture();controls();if(Number.isFinite(time))player.seek(Math.max(0,time));status('已返回视频（近似定位）；严格导出会继续在后台运行。关闭检查会请求取消导出。');if(play)player.play();return;}
      const active=job,token=++generation;clearTimeout(watch);job=null;busy=false;clearPicture();controls();
      if(Number.isFinite(time))player.seek(Math.max(0,time));
      status('已返回视频；浏览器按相对时间近似定位，不能保证停在刚才的解码帧。本次索引保留。');
      if(play)player.play();watchSource(token);
      if(active)api(`/api/video/frames/jobs/${active}/cancel`,{method:'POST',body:{}}).catch(error=>{if(!disposed&&token===generation)status('已返回视频；上一静帧任务取消尚未确认：'+error.message);});
    }
    panel.addEventListener('click',event=>{
      const action=event.target.closest('[data-frame]')?.dataset.frame;
      if(action==='set-first'){first=current?.ordinal??null;controls();}
      else if(action==='set-last'){last=current?.ordinal??null;controls();}
      else if(action==='export')exportRange();
      else if(action==='enter')locate(0);
      else if(action==='start')start();
      else if(action==='retry-capability')checkCapability();
      else if(action==='cancel')cancel();
      else if(action==='resume')resume(true);
      else if(action==='previous')locate((current?.ordinal??0)-1);
      else if(action==='next')locate((current?.ordinal??-1)+1);
      else if(action==='locate')locate(Number($('input').value));
    },{signal});
    exit.addEventListener('click',()=>resume(false),{signal});
    player.video.addEventListener('play',()=>{if(inspecting)player.pause();},{signal});
    player.screen.addEventListener('keydown',event=>{
      if(!inspecting||event.isComposing||event.ctrlKey||event.metaKey||event.altKey||event.target.closest('input,textarea,select,button,[contenteditable]'))return;
      const key=event.key.toLowerCase();
      if(['arrowleft','arrowright',',','.',' ','k','home','end','m','l','i','o'].includes(key)){
        event.preventDefault();event.stopImmediatePropagation();
        if(!busy&&current&&[',','arrowleft'].includes(key))locate(current.ordinal-1);
        else if(!busy&&current&&['.','arrowright'].includes(key))locate(current.ordinal+1);
        else if(key===' '||key==='k')status('静帧检查中；使用“返回视频（近似定位）”继续播放。');
      }
    },{capture:true,signal});
    container.addEventListener('click',event=>{
      if(inspecting&&event.target.closest('[data-review],.video-markers')){event.preventDefault();event.stopImmediatePropagation();status('请先返回视频，再设置基于播放时间的审片记录或导出。');}
    },{capture:true,signal});
    controls();checkCapability();
    return {
      isInspecting:()=>inspecting,
      getState:snapshot,
      destroy(){disposed=true;cancel();controller.abort();overlay.remove();panel.remove();}
    };
  }

  window.YingXuVideoFrames={mount};
})();
