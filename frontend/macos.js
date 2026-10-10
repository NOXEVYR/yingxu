/* The macOS host implements only close coordination; file actions use the HTTP API. */
(() => {
  'use strict';
  if (new URLSearchParams(location.search).get('desktop') !== 'macos') return;
  window.yingxuMac = true;
  window.yingxuDesktopOpenWebUrl = true;
  const listeners = [];
  const send = data => {
    if (data?.action === 'choose-external-files') {
      window.yingxuMacOpenExternal().catch(report); return;
    }
    if (data?.action === 'open-web-url') {
      if (Object.keys(data).length !== 2 || typeof data.url !== 'string') { report(new Error('网页链接请求无效。')); return; }
      let url;
      try {
        if (!data.url || data.url.length > 4096 || /[\s\\\u0000-\u001f\u007f-\u009f]/u.test(data.url) || !/^https?:\/\//i.test(data.url)) throw new Error();
        url = new URL(data.url);
        if (!['http:','https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.href.length > 4096) throw new Error();
      } catch { report(new Error('仅支持有效的 HTTP(S) 网页链接。')); return; }
      api('/api/macos/desktop',{method:'POST',body:{action:'open-web-url',url:url.href}}).then(result => {
        if (result?.ok !== true) throw new Error('macOS 未能打开此网页链接。');
      }).catch(report);
      return;
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
