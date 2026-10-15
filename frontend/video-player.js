/* Shared, single-decoder player. Seeking is time-based, never advertised as frame exact. */
(() => {
  'use strict';
  const clamp=(n,a,b)=>Math.max(a,Math.min(b,n));
  function time(value) {
    if (!Number.isFinite(value)) return '—';
    const ms=Math.max(0,Math.round(value*1000));
    return `${String(Math.floor(ms/3600000)).padStart(2,'0')}:${String(Math.floor(ms/60000)%60).padStart(2,'0')}:${String(Math.floor(ms/1000)%60).padStart(2,'0')}.${String(ms%1000).padStart(3,'0')}`;
  }
  function mount(parent, options) {
    const controller=new AbortController(), signal=controller.signal;
    const shell=document.createElement('section'); shell.className='video-player';
    shell.innerHTML=`<div class="video-screen" tabindex="0" aria-label="视频播放器"><video playsinline preload="auto"></video><div class="video-message" role="status"></div><div class="video-control-hit-area" aria-hidden="true"></div><div class="video-controls"><input class="video-seek" type="range" min="0" max="1" step="0.001" value="0" aria-label="播放位置（方向键跳转 5 秒，Shift 加方向键跳转 30 秒；Home 和 End 跳到开头或结尾）" aria-keyshortcuts="ArrowLeft ArrowRight Shift+ArrowLeft Shift+ArrowRight Home End PageUp PageDown"><div class="video-buttons"><div class="video-transport"><button type="button" data-command="play" title="播放 / 暂停 · 空格">播放</button><button type="button" data-command="back" title="后退 5 秒 · ←">−5s</button><button type="button" data-command="forward" title="前进 5 秒 · →">+5s</button><output class="video-time">00:00:00.000 / —</output></div><span class="video-spacer"></span><div class="video-utility"><button type="button" data-command="mute" title="静音 · M">声音</button><input class="video-volume" type="range" min="0" max="1" step="0.05" value="1" aria-label="音量"><select class="video-rate" aria-label="播放倍速">${[.25,.5,.75,1,1.25,1.5,2,3].map(n=>`<option value="${n}" ${n===1?'selected':''}>${n}×</option>`).join('')}</select><button type="button" data-command="loop" aria-pressed="false" title="循环播放 · L">循环</button><button type="button" data-command="fullscreen" title="全屏 · F / 双击；Esc 退出">全屏</button></div></div></div></div>`;
    parent.replaceChildren(shell);
    const $=q=>shell.querySelector(q), screen=$('.video-screen'),video=$('video'), controls=$('.video-controls'),hitArea=$('.video-control-hit-area'),message=$('.video-message'),seekbar=$('.video-seek');
    video.setAttribute('aria-label',options.name||'视频');
    let destroyed=false, hideTimer=0, scrubbing=false, wasPlaying=false, started=false, frameCallback=0, ab=null, lastPTS=null;
    const on=(node,event,fn,extra={})=>node.addEventListener(event,fn,{signal,...extra});
    function status(text) { message.textContent=text; message.hidden=!text; }
    function reveal() {
      screen.classList.remove('controls-hidden'); clearTimeout(hideTimer);
      if (!video.paused && !scrubbing) hideTimer=setTimeout(()=>{
        if(!controls.matches(':hover') && !controls.contains(document.activeElement)) screen.classList.add('controls-hidden');
      },2200);
    }
    function update() {
      const duration=video.duration,available=Number.isFinite(duration)&&duration>0;
      seekbar.disabled=!available; seekbar.max=available?duration:1;
      if(!scrubbing)seekbar.value=Number.isFinite(video.currentTime)?video.currentTime:0;
      const position=scrubbing?Number(seekbar.value):video.currentTime;
      $('.video-time').textContent=`${time(position)} / ${time(duration)}`;
      seekbar.setAttribute('aria-valuetext',available?`播放位置 ${time(position)}，总时长 ${time(duration)}`:'视频时长未知');
      $('[data-command="play"]').textContent=video.paused?'播放':'暂停';
      $('[data-command="mute"]').textContent=video.muted||!video.volume?'静音':'声音';
      $('.video-volume').value=video.muted?0:video.volume;
      $('.video-rate').value=String(video.playbackRate);
      $('[data-command="loop"]').setAttribute('aria-pressed',String(video.loop));
      options.onTime?.(video.currentTime,lastPTS);
    }
    async function play() { try { await video.play(); status(''); } catch(error) { if(!destroyed && error.name!=='AbortError')status('点击播放继续；若仍无法播放，当前环境可能不支持此编码。'); } }
    function toggle() { if(video.paused)play();else video.pause(); }
    function seek(seconds) { if(Number.isFinite(video.duration))video.currentTime=clamp(seconds,0,video.duration); reveal(); }
    async function fullscreen() {
      try { if(document.fullscreenElement)await document.exitFullscreen(); else await screen.requestFullscreen(); }
      catch(_) { status('无法进入全屏，请重新点击全屏按钮。'); }
      reveal();
    }
    const commands={play:toggle,back:()=>seek(video.currentTime-5),forward:()=>seek(video.currentTime+5),mute:()=>{video.muted=!video.muted;},loop:()=>{video.loop=!video.loop;update();},fullscreen};
    on(hitArea,'pointerenter',reveal);on(hitArea,'pointermove',reveal);
    on(controls,'pointerenter',reveal);
    on(controls,'click',event=>{const button=event.target.closest('[data-command]');if(button){commands[button.dataset.command]?.();reveal();}});
    on(video,'click',toggle);on(video,'dblclick',event=>{event.preventDefault();fullscreen();});
    on(screen,'pointermove',reveal);on(screen,'pointerleave',reveal);on(screen,'focusin',reveal);on(screen,'focusout',reveal);
    function beginScrub(){if(scrubbing)return;wasPlaying=!video.paused;scrubbing=true;video.pause();reveal();}
    function finishScrub(cancel=false){if(!scrubbing)return; if(!cancel)seek(Number(seekbar.value));scrubbing=false;if(wasPlaying)play();reveal();}
    on(seekbar,'pointerdown',beginScrub);on(seekbar,'input',()=>{beginScrub();update();});on(seekbar,'change',()=>finishScrub());
    on(window,'pointerup',()=>finishScrub());on(window,'pointercancel',()=>finishScrub(true));on(seekbar,'blur',()=>finishScrub());
    on(seekbar,'keydown',event=>{
      if(event.defaultPrevented||event.isComposing||event.keyCode===229||event.ctrlKey||event.metaKey||event.altKey||seekbar.disabled)return;
      let target=Number(seekbar.value),handled=true;
      if(event.key==='ArrowLeft')target-=event.shiftKey?30:5;
      else if(event.key==='ArrowRight')target+=event.shiftKey?30:5;
      else if(event.key==='PageDown')target-=30;
      else if(event.key==='PageUp')target+=30;
      else if(event.key==='Home')target=0;
      else if(event.key==='End')target=video.duration;
      else handled=false;
      if(!handled)return;
      event.preventDefault();event.stopPropagation();seek(clamp(target,0,video.duration));update();
    });
    on($('.video-volume'),'input',event=>{video.volume=Number(event.target.value);video.muted=false;});
    on($('.video-rate'),'change',event=>{video.playbackRate=Number(event.target.value);});
    on(screen,'keydown',event=>{
      if(event.defaultPrevented||event.isComposing||event.keyCode===229||event.ctrlKey||event.metaKey||event.altKey||event.target.closest('input,textarea,select,button,[contenteditable="true"]'))return;
      const key=event.key.toLowerCase();let handled=true;
      if(key===' '||key==='k')toggle();else if(key==='arrowleft')seek(video.currentTime-(event.shiftKey?30:5));else if(key==='arrowright')seek(video.currentTime+(event.shiftKey?30:5));
      else if(key==='arrowup')video.volume=clamp(video.volume+.05,0,1);else if(key==='arrowdown')video.volume=clamp(video.volume-.05,0,1);
      else if(key==='m')commands.mute();else if(key==='l')commands.loop();else if(key==='f')fullscreen();else if(key==='home')seek(0);else if(key==='end')seek(video.duration);
      else if(key==='escape'&&document.fullscreenElement)document.exitFullscreen().catch(()=>{});else handled=false;
      if(handled){event.preventDefault();event.stopPropagation();reveal();update();}
    });
    on(video,'loadedmetadata',()=>{update();status('正在准备画面…');});
    on(video,'loadeddata',()=>{status('');if(!started){started=true;if(options.autoplay)play();}});
    on(video,'waiting',()=>status('正在缓冲…'));on(video,'seeking',()=>status('正在定位…'));
    on(video,'seeked',()=>{status('');update();});on(video,'playing',()=>{status('');reveal();});
    on(video,'error',()=>{status(`无法播放此视频（${video.error?.code||'未知'}）。文件容器或编码可能不受当前播放环境支持；可在映序中查看文件信息。`);reveal();});
    for(const event of ['timeupdate','durationchange','play','pause','ended','volumechange','ratechange'])on(video,event,()=>{
      if(ab && !video.paused && video.currentTime>=ab[1])video.currentTime=ab[0]; update();
      if(event==='pause'||event==='ended')reveal();
    });
    on(document,'fullscreenchange',()=>{$('[data-command="fullscreen"]').textContent=document.fullscreenElement?'退出全屏':'全屏';reveal();});
    on(document,'visibilitychange',()=>{if(document.hidden)video.pause();});
    on(window,'pagehide',()=>destroy());
    // Observed presentation timestamp is diagnostic only, not a frame index.
    if(video.requestVideoFrameCallback){const frame=(_,metadata)=>{lastPTS=metadata.mediaTime;if(ab && !video.paused && !video.seeking && video.currentTime>=ab[1])video.currentTime=ab[0];options.onTime?.(video.currentTime,lastPTS);if(!destroyed)frameCallback=video.requestVideoFrameCallback(frame);};frameCallback=video.requestVideoFrameCallback(frame);}
    function destroy(){if(destroyed)return;destroyed=true;controller.abort();clearTimeout(hideTimer);if(frameCallback)video.cancelVideoFrameCallback(frameCallback);video.pause();video.removeAttribute('src');video.load();}
    status('正在打开视频…');video.src=options.src;update();screen.focus({preventScroll:true});
    return {video,shell,screen,seek,play,pause:()=>video.pause(),destroy,setAB:(a,b)=>{ab=Number.isFinite(a)&&Number.isFinite(b)&&b>a?[a,b]:null;if(ab)video.loop=false;},time};
  }
  window.YingXuVideo={mount,time};
})();
