(() => {
  'use strict';
  function mount(player,{tab,api,onDirty}) {
    const details=document.createElement('details');details.className='video-review';
    details.innerHTML=`<summary>审片与粗剪</summary><div class="video-review-content"><output class="review-clock">时间 —</output><button data-review="step-back">−40 ms</button><button data-review="step-forward">+40 ms</button><span>近似时间步进</span><button data-review="a">设 A</button><button data-review="b">设 B</button><button data-review="ab" aria-pressed="false">AB 循环</button><button data-review="in">设入点 I</button><button data-review="out">设出点 O</button><output class="review-range"></output><input class="video-marker-note" aria-label="标记备注" maxlength="500" placeholder="标记备注"><button data-review="mark">添加标记</button><button data-review="save">保存审片记录</button><div class="video-markers"></div><p>时间码为时:分:秒.毫秒；±40 ms 是时间 seek，不是逐帧。浏览器显示时间也不代表帧索引。下方“暂停帧检查”使用独立解码静帧；与视频时间 seek 分开。</p><select class="review-export-mode" aria-label="粗剪导出方式"><option value="encode">精确时间裁切 · 重编码 MP4</option><option value="copy">快速复制 · 关键帧约束 MKV</option></select><button data-review="export">导出入出点片段</button><button data-review="cancel" hidden>取消导出</button><p>保存标记与入出点不会修改视频。导出第一条视频和第一条音轨；不含其他音轨、字幕或附件。快速复制可能带入入点前关键帧内容；精确时间裁切重编码 H.264 / AAC（8-bit SDR），不用于保留 HDR / 高位深母版。</p><p class="review-status" role="status">展开后载入审片记录。</p><div class="video-job" role="status"></div></div>`;
    player.shell.appendChild(details);
    const $=q=>details.querySelector(q),controller=new AbortController();let loaded=false,loading=false,destroyed=false,pollTimer=0,abEnabled=false;
    const source=tab.source==='external'?'external':'file',url=`/api/video/review/${source}/${encodeURIComponent(tab.id)}`;
    const empty=()=>({markers:[],in:null,out:null,a:null,b:null});
    let review=tab.videoReviewDraft||empty(),revision=tab.videoReviewRevision||'',savedReview=tab.videoReviewSaved||empty();
    const status=text=>{$('.review-status').textContent=text;};
    const buttons=()=>{for(const node of details.querySelectorAll('button[data-review]:not([data-review="cancel"]),.video-marker-note,.review-export-mode'))node.disabled=!loaded;};
    function render(){
      $('.review-range').textContent=`入 ${player.time(review.in)} → 出 ${player.time(review.out)} · A ${player.time(review.a)} / B ${player.time(review.b)}`;
      const marks=$('.video-markers');marks.replaceChildren();
      review.markers.forEach((mark,index)=>{
        const button=document.createElement('button');button.type='button';button.textContent=`${player.time(mark.time)} ${mark.note}`;button.addEventListener('click',()=>player.seek(mark.time));marks.append(button);
        const remove=document.createElement('button');remove.type='button';remove.textContent='×';remove.setAttribute('aria-label',`删除标记 ${index+1}`);remove.addEventListener('click',()=>{review.markers.splice(index,1);changed();});marks.append(remove);
      });buttons();
    }
    function changed(){tab.videoReviewDraft=review;tab.videoReviewDirty=true;onDirty();status('审片记录尚未保存；原视频保持不变。');render();if(abEnabled)player.setAB(review.a,review.b);}
    async function save(){
      if(!loaded||tab.videoReviewSaving)return false;
      tab.videoReviewSaving=true;const raw=JSON.stringify(review);
      try {const result=await api(url,{method:'POST',body:{revision,review:JSON.parse(raw)}});revision=tab.videoReviewRevision=result.revision;savedReview=tab.videoReviewSaved=JSON.parse(raw);if(JSON.stringify(review)===raw)tab.videoReviewDirty=false;onDirty();status(tab.videoReviewDirty?'另有新修改尚未保存。':'审片记录已保存；原视频保持不变。');return !tab.videoReviewDirty;}
      catch(error){status(error.message);return false;}finally{tab.videoReviewSaving=false;}
    }
    tab.saveVideoReview=save;
    tab.discardVideoReview=()=>{
      review=tab.videoReviewDraft=JSON.parse(JSON.stringify(savedReview));tab.videoReviewDirty=false;onDirty();
      if(!destroyed){abEnabled=false;player.setAB(null,null);$('[data-review="ab"]').setAttribute('aria-pressed','false');render();status('已恢复保存的审片记录。');}
    };
    async function load(){
      if(loading||loaded)return;loading=true;buttons();
      try{const result=await api(url);if(destroyed)return;if(!tab.videoReviewDirty){revision=tab.videoReviewRevision=result.revision;review=tab.videoReviewDraft=result.review||empty();savedReview=tab.videoReviewSaved=JSON.parse(JSON.stringify(review));}loaded=true;render();status(result.source_changed?'源视频已更改；已有标记可能偏移，请核对后保存。':tab.videoReviewDirty?'审片记录尚未保存。':'审片记录已载入；原视频保持不变。');if(!tab.videoExportId){const history=await api(`/api/video/exports?source=${source}&id=${encodeURIComponent(tab.id)}`);if(destroyed)return;tab.videoExportId=history.jobs?.[0]?.id;}if(tab.videoExportId)poll();}
      catch(error){status(error.message+' 收起后展开可重试。');}finally{loading=false;}
    }
    async function poll(){
      clearTimeout(pollTimer);if(destroyed||!tab.videoExportId)return;
      try{const job=await api(`/api/video/exports/${tab.videoExportId}`);if(destroyed)return;
        const node=$('.video-job');node.replaceChildren();node.textContent=({queued:'排队中',running:'正在导出',cancelling:'正在取消并清理',cancelled:'已取消；原视频不变',done:'导出完成；原视频不变',error:'导出失败'})[job.state]||job.state;
        if(job.error)node.textContent+=' · '+job.error;
        const busy=['queued','running','cancelling'].includes(job.state);$('[data-review="cancel"]').hidden=!busy;$('[data-review="export"]').disabled=busy;
        if(job.download_url && (window.yingxuNativeStartup||window.yingxuMac)){const reveal=document.createElement('button');reveal.textContent='在文件夹中查看片段';reveal.onclick=()=>api(`/api/video/exports/${job.id}/reveal`,{method:'POST',body:{}}).catch(error=>status(error.message));node.append(reveal);}
        else if(job.download_url){const link=document.createElement('a');link.href=job.download_url;link.download=job.mode==='copy'?'clip.mkv':'clip.mp4';link.textContent=' 下载片段';node.append(link);}
        if(busy)pollTimer=setTimeout(poll,700);
      }catch(error){if(destroyed)return;$('.video-job').textContent='暂时无法读取任务状态；导出可能仍在运行。'+error.message;pollTimer=setTimeout(poll,2000);}
    }
    details.addEventListener('toggle',()=>{tab.videoReviewOpen=details.open;if(details.open)load();},{signal:controller.signal});
    details.addEventListener('click',async event=>{
      const command=event.target.closest('[data-review]')?.dataset.review;if(!command||!loaded)return;
      try{
        if(command==='save'){await save();return;}
        if(command==='step-back'||command==='step-forward'){player.pause();player.seek(player.video.currentTime+(command==='step-back'?-.04:.04));return;}
        if(command==='ab'){if(!(review.a!==null&&review.b!==null&&review.b>review.a))throw Error('先设置有效的 A、B 点。');abEnabled=!abEnabled;player.setAB(abEnabled?review.a:null,abEnabled?review.b:null);event.target.setAttribute('aria-pressed',String(abEnabled));if(abEnabled)player.seek(review.a);return;}
        if(['a','b','in','out'].includes(command)) {review[command]=player.video.currentTime;if((command==='a'||command==='in')&&review[command==='a'?'b':'out']<=review[command])review[command==='a'?'b':'out']=null;changed();return;}
        if(command==='mark'){if(review.markers.length>=500)throw Error('最多保存 500 个标记。');review.markers.push({time:player.video.currentTime,note:$('.video-marker-note').value});$('.video-marker-note').value='';changed();return;}
        if(command==='export'){
          if(review.in===null||review.out===null||review.out<=review.in||review.out>player.video.duration+.001)throw Error('请设置视频范围内有效的入点和出点。');
          event.target.disabled=true;
          try{const job=await api('/api/video/exports',{method:'POST',body:{source,id:tab.id,start:review.in,end:review.out,mode:$('.review-export-mode').value}});tab.videoExportId=job.id;poll();}
          catch(error){event.target.disabled=false;throw error;}return;
        }
        if(command==='cancel'){await api(`/api/video/exports/${tab.videoExportId}/cancel`,{method:'POST',body:{}});poll();}
      }catch(error){status(error.message);}
    },{signal:controller.signal});
    const timeUpdate=()=>{$('.review-clock').textContent=`时间 ${player.time(player.video.currentTime)}`;};player.video.addEventListener('timeupdate',timeUpdate,{signal:controller.signal});
    player.screen.addEventListener('keydown',event=>{if(!loaded||!details.open||event.defaultPrevented||event.isComposing||event.ctrlKey||event.metaKey||event.altKey||event.target.closest('input,textarea,select,button,[contenteditable]'))return;const action={i:'in',o:'out'}[event.key.toLowerCase()];if(action){event.preventDefault();event.stopPropagation();$(`[data-review="${action}"]`).click();}},{signal:controller.signal});
    const frames=window.YingXuVideoFrames?.mount(player,{tab,api,container:$('.video-review-content'),onEnter:()=>{abEnabled=false;player.setAB(null,null);$('[data-review="ab"]').setAttribute('aria-pressed','false');}});
    details.open=!!tab.videoReviewOpen;if(details.open)load();buttons();
    return {destroy(){destroyed=true;clearTimeout(pollTimer);frames?.destroy();controller.abort();}};
  }
  window.YingXuVideoReview={mount};
})();
