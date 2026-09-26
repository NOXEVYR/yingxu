/* Kept independent of app.js so a loading failure still has a way out. */
(() => {
  'use strict';
  if (window.yingxuNativeStartup) document.documentElement.classList.add('native-startup');
  let timer = null, finished = false;
  function finish() {
    finished = true;
    if (timer !== null) { clearTimeout(timer); timer = null; }
    document.removeEventListener('DOMContentLoaded', mount);
    document.getElementById('startupScreen')?.remove();
  }
  function mount() {
    if (finished || window.yingxuNativeStartup) { finish(); return; }
    const dismiss = document.getElementById('startupDismiss');
    dismiss?.addEventListener('click', finish, {once:true});
    timer = setTimeout(() => {
      timer = null;
      const message = document.getElementById('startupMessage');
      if (message) { message.hidden = false; message.textContent = '工作台尚未就绪，可查看连接提示。'; }
      if (dismiss) dismiss.hidden = false;
    }, 12000);
  }
  window.YingXuStartup = {finish};
  window.addEventListener('pagehide', finish, {once:true});
  document.addEventListener('DOMContentLoaded', mount, {once:true});
})();
