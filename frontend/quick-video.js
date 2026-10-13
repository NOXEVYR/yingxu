(() => {
  'use strict';
  let player=null;
  window.chrome.webview.addEventListener('message',event=>{
    const data=event.data;
    if(data?.action==='pause'){player?.pause();return;}
    if(data?.action==='exit-fullscreen'){if(document.fullscreenElement)document.exitFullscreen().catch(()=>{});return;}
    if(data?.action!=='open-video'||typeof data.name!=='string'||data.src!==new URL('media',location.href).href)return;
    if(player)return; // Duplicate host readiness must never restart playback.
    document.title=data.name+' · 映序快速播放';
    player=window.YingXuVideo.mount(document.querySelector('#player'),{name:data.name,src:data.src,autoplay:true});
  });
})();
