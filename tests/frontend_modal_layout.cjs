'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const {test} = require('node:test'), {execFile} = require('node:child_process'), {promisify} = require('node:util'), {pathToFileURL} = require('node:url');

test('real Chromium keeps shared dialog close control outside its scrolling content', async t => {
  const browser = [process.env.YINGXU_TEST_BROWSER, 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(p => p && fs.existsSync(p));
  if (!browser) { t.skip('Requires an existing Chromium browser; does not download'); return; }
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'yingxu-modal-layout-'));
  t.after(() => {
    const resolved = path.resolve(temporary);
    assert.ok(resolved.startsWith(path.resolve(os.tmpdir()) + path.sep) && path.basename(resolved).startsWith('yingxu-modal-layout-'));
    fs.rmSync(resolved, {recursive:true, force:true, maxRetries:10, retryDelay:100});
  });
  const root = path.join(__dirname, '../frontend');
  const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
  const dialogMarkup = html.match(/<dialog id="appDialog"[\s\S]*?<\/dialog>/)?.[0];
  assert.ok(dialogMarkup, 'Use the actual shared dialog structure');
  const styles = [...html.matchAll(/<link rel="stylesheet" href="\/([^"]+)"/g)].map(match => fs.readFileSync(path.join(root, match[1]), 'utf8')).join('\n');
  const source = fs.readFileSync(path.join(root, 'app.js'), 'utf8').replace(/boot\(\);\s*$/, '');
  fs.writeFileSync(path.join(temporary, 'app.js'), source);
  fs.writeFileSync(path.join(temporary, 'styles.css'), styles);
  fs.writeFileSync(path.join(temporary, 'runner.js'), `
    const results=[], check=(name,ok)=>results.push({name,ok});
    try {
      const dialog=document.querySelector('#appDialog'), form=document.querySelector('#dialogForm'), close=document.querySelector('#closeDialog');
      close.innerHTML='×'; close.addEventListener('click',()=>dialog.close());
      const body=Array.from({length:35},(_,i)=>'<div class="field"><label>合成字段 '+i+'</label><input id="field'+i+'" value="测试内容"></div>').join('');
      showDialog({title:'SKILL 扫描位置',subtitle:'查看来源、登记自定义目录。外部 SKILL 保持只读；关闭扫描或移除登记不会删除原文件。',wide:true,body});
      const rect=()=>close.getBoundingClientRect(), before=rect();
      const visible=element=>{const r=element.getBoundingClientRect(),d=dialog.getBoundingClientRect();return r.top>=d.top&&r.bottom<=d.bottom&&r.left>=d.left&&r.right<=d.right;};
      check('long content scrolls inside form',form.scrollHeight>form.clientHeight+500&&getComputedStyle(form).overflowY==='auto');
      check('heading is outside scrollable form',!form.contains(close)&&getComputedStyle(dialog).overflowY==='hidden');
      form.scrollTop=600;
      check('close position remains fixed after content scroll',form.scrollTop===600&&rect().top===before.top&&rect().left===before.left&&visible(close));
      check('dialog stays within viewport',dialog.getBoundingClientRect().bottom<=innerHeight&&dialog.getBoundingClientRect().top>=0);
      const last=document.querySelector('#field34');last.focus();
      check('last input is reachable by focus',document.activeElement===last&&visible(last));
      form.scrollTop=form.scrollHeight;
      check('footer actions are reachable',visible(document.querySelector('#dialogActions button[type="submit"]')));
      check('close is still visible at bottom',rect().top===before.top&&visible(close));
      close.click();check('close control closes long dialog',!dialog.open);
      showDialog({title:'短弹窗',body:'<div class="field"><label>名称</label><input id="shortInput"></div>'});
      check('short content has no needless scroll',form.scrollHeight===form.clientHeight&&visible(document.querySelector('#shortInput'))&&visible(close));
      check('short dialog sizes to content',dialog.getBoundingClientRect().height<innerHeight*.86);
      dialog.close();
      showDialog({title:'窄窗口中的来源设置',body,wide:true});
      dialog.style.width='280px';dialog.style.maxHeight='360px';
      const narrow=rect();form.scrollTop=form.scrollHeight;
      check('narrow dialog keeps close reachable',rect().top===narrow.top&&visible(close));
      check('narrow content does not overflow horizontally',form.scrollWidth===form.clientWidth&&dialog.scrollWidth===dialog.clientWidth);
      check('narrow footer is reachable',visible(document.querySelector('#dialogActions button[type="submit"]')));
      close.click();check('narrow close remains usable',!dialog.open);
      showDialog({title:'重新打开长弹窗',body,wide:true});
      check('reopened long dialog starts at top',form.scrollTop===0);
      dialog.close();check('closed dialog stays hidden',getComputedStyle(dialog).display==='none');
      document.querySelector('#result').textContent=JSON.stringify(results);
    } catch(error) {document.querySelector('#result').textContent=JSON.stringify({error:String(error),stack:error.stack});}
  `);
  fs.writeFileSync(path.join(temporary, 'fixture.html'), `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="styles.css">${dialogMarkup}<pre id="result"></pre><script src="app.js"></script><script src="runner.js"></script>`);
  for (const size of ['1000,800', '500,600']) {
    const {stdout} = await promisify(execFile)(browser, ['--headless','--disable-gpu','--no-first-run','--disable-background-networking',`--user-data-dir=${path.join(temporary,'profile-'+size.replace(',','-'))}`,`--window-size=${size}`,'--virtual-time-budget=1000','--dump-dom',pathToFileURL(path.join(temporary,'fixture.html')).href], {windowsHide:true,timeout:30000,maxBuffer:2*1024*1024});
    const match = stdout.match(/<pre id="result">([^<]+)<\/pre>/); assert.ok(match, stdout.slice(-1500));
    const results = JSON.parse(match[1].replace(/&quot;/g,'"').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>'));
    assert.ok(Array.isArray(results), JSON.stringify(results));
    assert.equal(results.length, 16);
    for (const row of results) assert.equal(row.ok, true, `${size}: ${row.name}`);
  }
});
