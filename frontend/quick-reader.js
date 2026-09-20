/* Only the host supplies text. This page has no API, disk or network access. */
(() => {
  'use strict';
  const body=document.querySelector('#document'),name=document.querySelector('#filename'),mode=document.querySelector('#mode'),notice=document.querySelector('#notice');
  let file=null,source=false;
  function render() {
    if(!file)return;
    body.classList.toggle('source',source);
    if(source)body.textContent=file.content;
    else body.innerHTML=window.YingXuPreview.render(file.content);
    mode.textContent=source?'排版阅览':'查看源码';
    notice.textContent=!source&&file.content.length>140000?'排版显示前 14 万字符，点击“查看源码”可阅读完整文本。':'';
  }
  window.chrome.webview.addEventListener('message',event=>{
    const value=event.data;
    if(!value||typeof value.name!=='string'||typeof value.content!=='string'||typeof value.markdown!=='boolean')return;
    file=value;source=!value.markdown;name.textContent=value.name;
    document.title=value.name+' · 映序快速阅览';mode.hidden=!value.markdown;render();
  });
  mode.addEventListener('click',()=>{source=!source;render();});
  document.addEventListener('click',event=>{if(event.target.closest('a'))event.preventDefault();});
})();
