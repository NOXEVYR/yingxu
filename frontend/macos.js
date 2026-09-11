/* The macOS host implements only close coordination; file actions use the HTTP API. */
(() => {
  'use strict';
  if (new URLSearchParams(location.search).get('desktop') !== 'macos') return;
  window.yingxuMac = true;
  const listeners = [];
  const send = data => {
    if (data?.action === 'choose-external-files') {
      window.yingxuMacOpenExternal().catch(report); return;
    }
    if (!['desktop-ready','exit-response'].includes(data?.action)) return;
    api('/api/macos/desktop',{method:'POST',body:data}).catch(report);
  };
  window.chrome ||= {};
  window.chrome.webview = {postMessage:send, addEventListener:(type,handler) => {
    if (type === 'message') listeners.push(handler);
  }};
  window.yingxuMacReceive = data => listeners.forEach(handler => handler({data}));
  const style = document.createElement('style');
  style.textContent = '[data-action="capture-screen"],[data-drag-file]{display:none!important}';
  document.head.appendChild(style);
  document.addEventListener('DOMContentLoaded', () => {
    // Keep the host handshake after clicking the brand/home link.
    const home = document.querySelector('.brand');
    if (home) home.setAttribute('href','/?desktop=macos');
  }, {once:true});

  window.yingxuMacOpenExternal = async () => {
    if (!state.bootstrap || state.modalBusy || $('#appDialog').open) return;
    state.modalBusy = true;
    let entries = [];
    try {
      const result = await api('/api/pick',{method:'POST',body:{kind:'files'}});
      if (result.paths?.length) entries = (await api('/api/external-open',{method:'POST',body:{paths:result.paths}})).entries || [];
    } finally { state.modalBusy = false; }
    if (entries.length) await queueExternalFiles(entries);
  };

})();
