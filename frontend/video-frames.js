(() => {
  'use strict';
  function mount(player,{tab,api,container,onEnter}) {
    const panel=document.createElement('section');panel.className='video-frame-inspector';
    panel.innerHTML=`<strong>暂停帧检查</strong><div class="frame-actions"><button data-frame="start">后台建立真实帧索引</button><button data-frame="enter" disabled>暂停并检查第 0 帧</button><button data-frame="previous" disabled>上一帧 ,</button><button data-frame="next" disabled>下一帧 .</button><input type="number" min="0" step="1" value="0" aria-label="展示帧序号（从0开始）" disabled><button data-frame="locate" disabled>暂停并按帧号定位</button><button data-frame="cancel" hidden>取消检查</button><button data-frame="resume" hidden>继续播放（近似定位）</button></div><p class="frame-status" role="status">检查工具能力…</p><p>帧号从 0 开始；静帧逐张核对完整顺序解码像素，不用平均帧率推算。后台索引需全片解码两遍，可取消；期间可以继续播放和审片，完成不会自动暂停。点击进入检查才暂停；此后关键帧定位与邻帧缓存加速，校验失败回退起点。返回视频只做浏览器近似定位，并保留本次索引；关闭检查或标签后释放。</p><div class="frame-export"><strong>严格源帧范围导出</strong><div class="frame-actions"><button data-frame="set-first" disabled>当前帧设为首帧</button><button data-frame="set-last" disabled>当前帧设为尾帧</button><span class="frame-range">尚未设定范围</span><button data-frame="export" disabled>仅视频 FFV1 · 不含音频</button></div><p>首尾两帧均包含，单次最多 10000 帧。按源展示顺序选择，保留每帧相对 PTS 与重复 PTS，逐帧复验像素。自动输出 MKV 或 NUT（时间基无法精确保留则拒绝）；仅供支持 FFV1 的编辑工具，快速播放器不保证可播。不含音频、字幕或附件；需要声音请用上方按时间裁切。末帧持续时长由容器处理，不承诺精确结束时刻。</p><div class="frame-export-result" role="status"></div></div>`;
    container.append(panel);
    const $=q=>panel.querySelector(q),controller=new AbortController();
    const overlay=document.createElement('div');overlay.className='frame-overlay';overlay.hidden=true;
    const image=document.createElement('img');image.alt='解码静帧';
    const label=document.createElement('div');label.className='frame-overlay-label';
    const exit=document.createElement('button');exit.textContent='回到视频（近似定位）';exit.type='button';
    overlay.append(image,label,exit);player.screen.append(overlay);
    let disposed=false,session=null,job=null,count=0,current=null,generation=0,url=null,watch=0,available=false,busy=false,inspecting=false,exporting=false,first=null,last=null;
    const status=text=>{$('.frame-status').textContent=text;};
    function controls(){
      $('[data-frame=start]').disabled=!available||!!session||busy;
      for(const action of ['previous','next'])$(`[data-frame=${action}]`).disabled=busy||!current;
      for(const action of ['enter','locate'])$(`[data-frame=${action}]`).disabled=busy||!count;
      $('[data-frame=enter]').hidden=inspecting;
      $('input').disabled=busy||!count;
      $('[data-frame=previous]').disabled ||= current?.ordinal===0;
      $('[data-frame=next]').disabled ||= current?.ordinal===count-1;
      $('[data-frame=cancel]').hidden=!session&&!busy;
      $('[data-frame=cancel]').textContent=busy&&!count?'取消后台索引':'关闭索引与检查';
      $('[data-frame=resume]').hidden=!current;
      for(const action of ['set-first','set-last'])$(`[data-frame=${action}]`).disabled=busy||!current;
      $('[data-frame=export]').disabled=busy||!session||first===null||last===null||first>last;
      $('.frame-range').textContent=first===null&&last===null?'尚未设定范围':`[${first??'—'}, ${last??'—'}] · 两端包含${first!==null&&last!==null&&last>=first?' · '+(last-first+1)+' 帧':''}`;
    }
    async function poll(id,token){
      while(!disposed&&token===generation){
        const value=await api(`/api/video/frames/jobs/${id}`);
        if(disposed||token!==generation)return null;
        if(['ready','done','error','cancelled'].includes(value.state)){
          if(value.state==='error')throw Error(value.error||'帧检查失败。');
          return value;
        }
        status(`${value.phase}${value.kind==='index'?' · 已索引 '+value.frames+' 帧 · 像素核验 '+(value.verified_frames??0)+' 帧':''} · ${value.elapsed} 秒${value.bytes?' · 校验 '+(value.bytes/1048576).toFixed(1)+' MiB':''}`);
        await new Promise(resolve=>setTimeout(resolve,250));
      }
      return null;
    }
    function clearPicture(){inspecting=false;overlay.hidden=true;image.removeAttribute('src');player.screen.classList.remove('frame-inspecting');if(url){URL.revokeObjectURL(url);url=null;}current=null;}
    async function cancel({keepMessage=false}={}){
      const token=++generation;clearTimeout(watch);const index=session,active=job;session=null;job=null;count=0;busy=false;exporting=false;first=null;last=null;
      clearPicture();controls();
      if(!keepMessage)status('后台索引与帧检查已关闭；视频播放状态保持不变。');
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
      busy=true;inspecting=true;const token=++generation;clearTimeout(watch);controls();player.screen.classList.add('frame-inspecting');player.pause();onEnter();status('正在核对目标静帧；视频已暂停。');
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
        status(label.textContent+'；'+({'verified-keyframe':'关键帧定位 · 像素核验通过','verified-prefix':'起点解码 · 像素核验通过','verified-cache':'邻帧缓存 · 已核验像素'}[result.decode_mode]||'像素已核验')+'；视频仍暂停。');watchSource(token);
      }catch(error){if(!disposed&&token===generation){status(error.message);await cancel({keepMessage:true});}}
      finally{if(token===generation){busy=false;job=null;controls();}}
    }
    async function start(){
      if(busy||session||!available)return;
      const token=++generation;busy=true;controls();status('正在后台建立真实帧索引；可以继续播放和审片。');
      try{
        const result=await api('/api/video/frames/index',{method:'POST',body:{source:tab.source==='external'?'external':'file',id:tab.id}});
        if(disposed||token!==generation){await api(`/api/video/frames/index/${result.id}/cancel`,{method:'POST',body:{}}).catch(()=>{});return;}
        session=result.id;controls();const ready=await poll(session,token);
        if(!ready||ready.state!=='ready')return;
        count=ready.count;$('input').max=String(count-1);busy=false;controls();
        status(`后台索引已就绪 · ${count} 帧。视频播放状态保持不变；点击“暂停并检查第 0 帧”或按帧号定位进入检查。`);watchSource(token);
      }catch(error){if(!disposed&&token===generation){status(error.message);await cancel({keepMessage:true});}}
      finally{if(token===generation){busy=false;controls();}}
    }
    async function exportRange(){
      if(busy||!session||first===null||last===null||first>last)return;
      busy=true;exporting=true;const token=++generation;clearTimeout(watch);controls();
      const output=$('.frame-export-result');output.replaceChildren();
      try{
        const request=await api(`/api/video/frames/index/${session}/export`,{method:'POST',body:{first,last,audio:'none'}});
        if(disposed||token!==generation){await api(`/api/video/frames/jobs/${request.id}/cancel`,{method:'POST',body:{}}).catch(()=>{});return;}
        job=request.id;const result=await poll(job,token);
        if(!result||result.state!=='done')return;
        output.textContent=`已核验 ${result.count} 帧 · [${result.first}, ${result.last}] 两端包含 · FFV1 ${result.format.toUpperCase()} · 不含音频。 `;
        for(const [text,href,name] of [['下载视频',result.download_url,'clip.'+result.format],['下载源帧清单',result.manifest_url,'frames.json']]){
          const link=document.createElement('a');link.textContent=text;link.href=href;link.download=name;output.append(link,document.createTextNode(' '));
        }
        status('严格源帧视频已生成；原片保留。关闭检查后仍可从审片导出历史下载视频。');
      }catch(error){if(!disposed&&token===generation)status(error.message);}
      finally{if(token===generation){busy=false;exporting=false;job=null;controls();watchSource(token);}}
    }
    function resume(play){
      const time=current?.resume_seconds_approximate;
      if(exporting){
        clearPicture();controls();
        if(Number.isFinite(time))player.seek(Math.max(0,time));
        status('已返回视频（近似定位）；严格导出继续在后台运行，完成后提供下载。关闭索引与检查才会取消导出。');
        if(play)player.play();
        return;
      }
      const active=job,token=++generation;
      clearTimeout(watch);job=null;busy=false;clearPicture();controls();
      if(Number.isFinite(time))player.seek(Math.max(0,time));
      status('已返回视频；浏览器仅按相对时间近似定位，不能保证停在刚才的解码帧。本次索引保留，可再次点击进入检查。');
      if(play)player.play();
      watchSource(token);
      if(active)api(`/api/video/frames/jobs/${active}/cancel`,{method:'POST',body:{}}).catch(error=>{
        if(!disposed&&token===generation)status('已返回视频；上一静帧任务取消尚未确认：'+error.message);
      });
    }
    panel.addEventListener('click',event=>{
      const action=event.target.closest('[data-frame]')?.dataset.frame;
      if(action==='set-first'){first=current?.ordinal??null;controls();}else if(action==='set-last'){last=current?.ordinal??null;controls();}else if(action==='export')exportRange();
      else if(action==='enter')locate(0);else if(action==='start')start();else if(action==='cancel')cancel();else if(action==='resume')resume(true);
      else if(action==='previous')locate((current?.ordinal??0)-1);else if(action==='next')locate((current?.ordinal??-1)+1);
      else if(action==='locate')locate(Number($('input').value));
    },{signal:controller.signal});
    exit.addEventListener('click',()=>resume(false),{signal:controller.signal});
    player.video.addEventListener('play',()=>{if(inspecting)player.pause();},{signal:controller.signal});
    player.screen.addEventListener('keydown',event=>{
      if(!inspecting||event.isComposing||event.ctrlKey||event.metaKey||event.altKey||event.target.closest('input,textarea,select,button,[contenteditable]'))return;
      const key=event.key.toLowerCase();
      if(['arrowleft','arrowright',',','.',' ','k','home','end','m','l','i','o'].includes(key)){
        event.preventDefault();event.stopImmediatePropagation();
        if(!busy&&current&&[',','arrowleft'].includes(key))locate(current.ordinal-1);
        else if(!busy&&current&&['.','arrowright'].includes(key))locate(current.ordinal+1);
        else if(key===' '||key==='k')status('静帧检查中；请使用“继续播放（近似定位）”返回视频。');
      }
    },{capture:true,signal:controller.signal});
    // Inspector records and video time-seek controls must not mutate the hidden
    // video's time while a different decoded frame is being displayed.
    container.addEventListener('click',event=>{if(inspecting&&event.target.closest('[data-review],.video-markers')){event.preventDefault();event.stopImmediatePropagation();status('请先返回视频再设置基于播放时间的审片记录或导出。');}},{capture:true,signal:controller.signal});
    api('/api/video/frames/capability').then(value=>{if(!disposed){available=value.available;status(value.reason||'按需建立真实展示帧索引；不会修改源视频。');controls();}}).catch(error=>{if(!disposed){status(error.message);controls();}});
    controls();
    return {destroy(){disposed=true;cancel();controller.abort();overlay.remove();}};
  }
  window.YingXuVideoFrames={mount};
})();
