(function(root) {
  'use strict';
  function combination(event) {
    if (event.isComposing || event.key === 'Process' || event.metaKey || event.getModifierState?.('AltGraph')) return null;
    const modifiers = [event.ctrlKey && 'Ctrl',event.altKey && 'Alt',event.shiftKey && 'Shift'].filter(Boolean);
    const code = String(event.code || '');
    let key = /^Key[A-Z]$/.test(code) ? code.slice(3) : /^Digit[0-9]$/.test(code) ? code.slice(5) : /^F(?:[1-9]|10|11|1[3-9]|2[0-4])$/.test(event.key) ? event.key : '';
    // Physical keys keep Chinese input mode and Shift+number unambiguous.
    if (!key || modifiers.length < 2) return null;
    return [...modifiers,key].join('+');
  }
  function install({dialog, input, button, status, enabled, native, send}) {
    let recording = false, awaiting = false, disposed = false, lastStatus = null, timer = null, confirmationTimer = null;
    let savedValue = input.value, savedEnabled = enabled.checked;
    const listeners = [];
    const on = (node,name,callback,options) => { node.addEventListener(name,callback,options); listeners.push(() => node.removeEventListener(name,callback,options)); };
    const display = value => value.replace(/\+/g,' + ');
    const hint = (text,kind='info') => { status.textContent = text; status.dataset.state = kind; };
    const changed = () => input.value !== savedValue || enabled.checked !== savedEnabled;
    function waitForStatus() {
      clearTimeout(confirmationTimer);
      if (native && savedEnabled) confirmationTimer=setTimeout(() => {
        if (!disposed && !recording && !awaiting && !changed() && (!lastStatus || lastStatus.shortcut !== savedValue || lastStatus.enabled !== savedEnabled)) hint('设置已保存，但尚未收到系统确认；请重新打开设置检查。','pending');
      },3500);
    }
    function renderStatus() {
      if (disposed || recording || awaiting) return;
      button.textContent = display(input.value);
      button.setAttribute('aria-pressed','false');
      button.dataset.recording = 'false';
      if (changed()) return hint('已录入，尚未保存。点击“保存设置”后生效。','pending');
      if (!enabled.checked) return hint('截图快捷键已关闭。');
      if (!native) return hint('已保存：' + display(savedValue) + '。请在新版映序桌面端确认系统热键状态。');
      if (!lastStatus || lastStatus.shortcut !== savedValue || lastStatus.enabled !== savedEnabled) return hint('已保存，等待系统确认…','pending');
      if (lastStatus.error) return hint('未生效：' + lastStatus.error,'error');
      hint(lastStatus.registered ? '已生效：' + display(savedValue) : '未生效，请重新保存设置或换一个组合。',lastStatus.registered ? 'success' : 'error');
    }
    function finish(message) {
      const wasRecording = recording || awaiting;
      recording = awaiting = false; clearTimeout(timer);
      if (wasRecording && native) send({action:'capture-hotkey-recording',active:false});
      renderStatus();
      if (message) hint(message);
    }
    function ready() {
      clearTimeout(timer);awaiting=false;recording=true;
      button.textContent='请按快捷键…';button.dataset.recording='true';button.setAttribute('aria-pressed','true');
      hint('按至少两个 Ctrl / Alt / Shift，再按字母、数字或功能键；Esc 取消。');
    }
    on(button,'click',() => {
      if (disposed) return;
      if (recording || awaiting) return finish('已取消录入，原设置保持不变。');
      button.focus();
      clearTimeout(confirmationTimer);
      if (!native) { ready(); return; }
      awaiting=true;button.textContent='正在准备录入…';hint('正在暂时停用映序截图热键…');
      timer=setTimeout(() => { if (awaiting) finish('未收到桌面端响应，请重新点击录入。'); },2500);
      send({action:'capture-hotkey-recording',active:true});
    });
    on(button,'keydown',event => {
      if (!recording && !awaiting) return;
      if (event.key === 'Tab') { finish();return; }
      event.preventDefault();event.stopImmediatePropagation();
      if (event.key === 'Escape') { finish('已取消录入，原设置保持不变。');return; }
      if (awaiting || event.repeat) return;
      const value=combination(event);
      if (value) { input.value=value;finish();return; }
      const parts=[event.ctrlKey&&'Ctrl',event.altKey&&'Alt',event.shiftKey&&'Shift'].filter(Boolean);
      button.textContent=parts.length ? parts.join(' + ')+' + …' : '请按快捷键…';
      if (!['Control','Alt','Shift'].includes(event.key)) hint('组合未接受：至少两个修饰键，加字母、数字或 F1–F24（不含 F12）；不支持 Win 键。','error');
    });
    on(button,'keyup',event => {
      if (!recording) return;
      event.preventDefault();event.stopImmediatePropagation();
      const parts=[event.ctrlKey&&'Ctrl',event.altKey&&'Alt',event.shiftKey&&'Shift'].filter(Boolean);
      button.textContent=parts.length ? parts.join(' + ')+' + …' : '请按快捷键…';
    });
    on(button,'blur',() => finish());
    on(enabled,'change',() => { finish();renderStatus(); });
    on(dialog,'cancel',() => finish());
    on(dialog,'close',() => dispose());
    on(root,'blur',() => finish());
    on(root,'pagehide',() => dispose());
    function handle(data) {
      if (disposed || data?.action !== 'capture-hotkey-status') return;
      if (data.recording) { if (awaiting) ready();return; }
      lastStatus=data;
      if (data.shortcut === savedValue && data.enabled === savedEnabled) clearTimeout(confirmationTimer);
      // Native deactivation can end recording even if the DOM blur was missed.
      if (recording) { recording=false;renderStatus();hint('录入已结束，请重新点击按钮录入。');return; }
      renderStatus();
    }
    function saved(settings) {
      finish();savedValue=input.value=settings.capture_hotkey;savedEnabled=enabled.checked=settings.capture_enabled;
      lastStatus=null;renderStatus();
      waitForStatus();
    }
    function dispose() { if (disposed) return;finish();disposed=true;clearTimeout(timer);clearTimeout(confirmationTimer);listeners.forEach(remove => remove()); }
    renderStatus();waitForStatus();
    return {handle,saved,dispose,finish,isRecording:() => recording || awaiting};
  }
  root.YingXuHotkeyRecorder={combination,install};
})(typeof window === 'undefined' ? globalThis : window);
