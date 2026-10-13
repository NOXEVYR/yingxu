(() => {
  'use strict';
  function mount(player,{tab,api,container,onEnter}) {
    const panel=document.createElement('section');panel.className='video-frame-inspector';
    panel.innerHTML=`<strong>暂停帧检查</strong><div class="frame-actions"><button data-frame="start">建立真实帧索引</button><button data-frame="previous" disabled>上一帧 ,</button><button data-frame="next" disabled>下一帧 .</button><input type="number" min="0" step="1" value="0" aria-label="展示帧序号（从0开始）" disabled><button data-frame="locate" disabled>按帧号定位</button><button data-frame="cancel" hidden>取消检查</button><button data-frame="resume" hidden>继续播放（近似定位）</button></div><p class="frame-status" role="status">检查工具能力…</p><p>帧号从 0 开始；静帧来自顺序解码，不用平均帧率推算。首次索引需读取全片，可取消。返回视频只做浏览器近似时间定位；本检查尚不提供按帧号导出。</p>`;
    container.append(panel);
    const $=q=>panel.querySelector(q),controller=new AbortController();
    const overlay=document.createElement('div');overlay.className='frame-overlay';overlay.hidden=true;
    const image=document.createElement('img');image.alt='解码静帧';
    const label=document.createElement('div');label.className='frame-overlay-label';
    const exit=document.createElement('button');exit.textContent='回到视频（近似定位）';exit.type='button';
    overlay.append(image,label,exit);player.screen.append(overlay);
    let disposed=false,session=null,job=null,count=0,current=null,generation=0,url=null,watch=0,available=false,busy=false;
    const status=text=>{$('.frame-status').textContent=text;};
    function controls(){
      $('[data-frame=start]').disabled=!available||!!session||busy;
      for(const action of ['previous','next','locate'])$(`[data-frame=${action}]`).disabled=busy||!count;
      $('input').disabled=busy||!count;
      $('[data-frame=previous]').disabled ||= current?.ordinal===0;
      $('[data-frame=next]').disabled ||= current?.ordinal===count-1;
      $('[data-frame=cancel]').hidden=!session&&!busy;
      $('[data-frame=resume]').hidden=!current;
    }
    async function poll(id,token){
      while(!disposed&&token===generation){
        const value=await api(`/api/video/frames/jobs/${id}`);
        if(disposed||token!==generation)return null;
        if(['ready','done','error','cancelled'].includes(value.state)){
          if(value.state==='error')throw Error(value.error||'帧检查失败。');
          return value;
        }
        status(`${value.phase} · 已索引 ${value.frames} 帧 · ${value.elapsed} 秒${value.bytes?' · 校验 '+(value.bytes/1048576).toFixed(1)+' MiB':''}`);
        await new Promise(resolve=>setTimeout(resolve,250));
      }
      return null;
    }
    function clearPicture(){overlay.hidden=true;image.removeAttribute('src');player.screen.classList.remove('frame-inspecting');if(url){URL.revokeObjectURL(url);url=null;}current=null;}
    async function cancel({keepMessage=false}={}){
      const token=++generation;clearTimeout(watch);const index=session,active=job;session=null;job=null;count=0;busy=false;
      clearPicture();controls();
      if(!keepMessage)status('帧检查已关闭；视频保持暂停。');
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
    async function locate(ordinal){
      if(busy||!session||!Number.isInteger(ordinal)||ordinal<0||ordinal>=count){status('请输入范围内的整数帧号。');return;}
      busy=true;const token=++generation;clearTimeout(watch);controls();player.pause();
      try{
        const request=await api(`/api/video/frames/index/${session}/frame`,{method:'POST',body:{ordinal}});
        if(disposed||token!==generation){await api(`/api/video/frames/jobs/${request.id}/cancel`,{method:'POST',body:{}}).catch(()=>{});return;}
        job=request.id;const result=await poll(job,token);if(!result||result.state!=='done')return;
        const response=await fetch(result.image_url,{signal:controller.signal});
        if(!response.ok)throw Error('静帧缓存已更新或源文件已改变，请重新建立索引。');
        const blob=await response.blob();if(disposed||token!==generation)return;
        const nextUrl=URL.createObjectURL(blob),decoded=new Image();decoded.src=nextUrl;
        try{await decoded.decode();}catch(error){URL.revokeObjectURL(nextUrl);throw error;}
        if(disposed||token!==generation){URL.revokeObjectURL(nextUrl);return;}
        if(url)URL.revokeObjectURL(url);url=nextUrl;image.src=url;current=result;overlay.hidden=false;
        player.screen.classList.add('frame-inspecting');$('input').value=String(ordinal);
        label.textContent=`解码静帧 ${ordinal} / ${count-1} · ${result.pts===null?'推算 PTS '+(result.best_effort_pts??'未知'):'原始 PTS '+result.pts} × ${result.time_base} · 源 ${result.source_sha256.slice(0,12)}`;
        status(label.textContent+'；这是静帧检查，视频仍暂停。');watchSource(token);
      }catch(error){if(!disposed&&token===generation){status(error.message);await cancel({keepMessage:true});}}
      finally{if(token===generation){busy=false;job=null;controls();}}
    }
    async function start(){
      if(busy||session||!available)return;
      const token=++generation;busy=true;player.pause();onEnter();controls();
      try{
        const result=await api('/api/video/frames/index',{method:'POST',body:{source:tab.source==='external'?'external':'file',id:tab.id}});
        if(disposed||token!==generation){await api(`/api/video/frames/index/${result.id}/cancel`,{method:'POST',body:{}}).catch(()=>{});return;}
        session=result.id;controls();const ready=await poll(session,token);
        if(!ready||ready.state!=='ready')return;
        count=ready.count;$('input').max=String(count-1);busy=false;controls();await locate(0);
      }catch(error){if(!disposed&&token===generation){status(error.message);await cancel({keepMessage:true});}}
      finally{if(token===generation){busy=false;controls();}}
    }
    function resume(play){
      const time=current?.resume_seconds_approximate;
      cancel({keepMessage:true});
      if(Number.isFinite(time))player.seek(Math.max(0,time));
      status('已返回视频；浏览器仅按相对时间近似定位，不能保证停在刚才的解码帧。');
      if(play)player.play();
    }
    panel.addEventListener('click',event=>{
      const action=event.target.closest('[data-frame]')?.dataset.frame;
      if(action==='start')start();else if(action==='cancel')cancel();else if(action==='resume')resume(true);
      else if(action==='previous')locate((current?.ordinal??0)-1);else if(action==='next')locate((current?.ordinal??-1)+1);
      else if(action==='locate')locate(Number($('input').value));
    },{signal:controller.signal});
    exit.addEventListener('click',()=>resume(false),{signal:controller.signal});
    player.video.addEventListener('play',()=>{if(session||busy)player.pause();},{signal:controller.signal});
    player.screen.addEventListener('keydown',event=>{
      if(!current||event.isComposing||event.ctrlKey||event.metaKey||event.altKey||event.target.closest('input,textarea,select,button,[contenteditable]'))return;
      const key=event.key.toLowerCase();
      if(['arrowleft','arrowright',',','.',' ','k','home','end','m','l','i','o'].includes(key)){
        event.preventDefault();event.stopImmediatePropagation();
        if(!busy&&[',','arrowleft'].includes(key))locate(current.ordinal-1);
        else if(!busy&&['.','arrowright'].includes(key))locate(current.ordinal+1);
        else if(key===' '||key==='k')status('静帧检查中；请使用“继续播放（近似定位）”返回视频。');
      }
    },{capture:true,signal:controller.signal});
    // Inspector records and video time-seek controls must not mutate the hidden
    // video's time while a different decoded frame is being displayed.
    container.addEventListener('click',event=>{if(current&&event.target.closest('[data-review],.video-markers')){event.preventDefault();event.stopImmediatePropagation();status('请先返回视频再设置基于播放时间的审片记录或导出。');}},{capture:true,signal:controller.signal});
    api('/api/video/frames/capability').then(value=>{if(!disposed){available=value.available;status(value.reason||'按需建立真实展示帧索引；不会修改源视频。');controls();}}).catch(error=>{if(!disposed){status(error.message);controls();}});
    controls();
    return {destroy(){disposed=true;cancel();controller.abort();overlay.remove();}};
  }
  window.YingXuVideoFrames={mount};
})();
