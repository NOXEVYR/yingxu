'use strict';
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path'), os=require('node:os'), vm=require('node:vm');
const {test}=require('node:test'), {execFile}=require('node:child_process'), {promisify}=require('node:util'), {pathToFileURL}=require('node:url');

test('XMind edit, branches, sheets, undo, composition and read-only behavior in real Chromium',async t=>{
  const browser=[process.env.YINGXU_TEST_BROWSER,'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(file=>file&&fs.existsSync(file));
  if(!browser){t.skip('Existing Chromium required; no download');return;}
  const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'yingxu-xmind-dom-'));
  t.after(()=>{const root=path.resolve(temporary);assert.ok(root.startsWith(path.resolve(os.tmpdir())+path.sep)&&path.basename(root).startsWith('yingxu-xmind-dom-'));fs.rmSync(root,{recursive:true,force:true,maxRetries:10,retryDelay:100});});
  for(const file of ['xmind-editor.js','xmind-editor.css','styles.css','appearance.css'])fs.copyFileSync(path.join(__dirname,'../frontend',file),path.join(temporary,file));
  fs.writeFileSync(path.join(temporary,'runner.js'),`(() => {
    const checks=[],check=(name,ok)=>checks.push({name,ok}),parent=document.querySelector('#mount');
    const model=[{title:'第一画布',rootTopic:{id:'root',title:'中心',notes:{plain:{content:'保留'}},children:{attached:[{id:'child',title:'分支'}],detached:[{id:'floating',title:'自由主题'}]}}},{title:'第二画布',rootTopic:{id:'root2',title:'<img src=x onerror=alert(1)>'}}];
    let draft=JSON.stringify(model),blocked=0,composition=false;
    const options={parent,value:draft,editable:true,notice:'保存前备份',onChange:value=>draft=value,onBlocked:()=>blocked++,onComposition:value=>composition=value};
    let editor=YingXuXmind.mount(options);
    const aliases={'添加子主题':'＋子主题','添加同级主题':'＋同级','删除主题及分支':'删除分支','确认删除此主题及所有分支':'确认删除整个分支','撤销':'↶','重做':'↷'};const button=text=>[...parent.querySelectorAll('button')].find(node=>node.textContent===(aliases[text]||text));
    const title=()=>parent.querySelector('[aria-label="主题文字"]'),input=value=>{title().value=value;title().dispatchEvent(new Event('input'));};
    check('three attached and detached topics displayed',parent.querySelectorAll('.xmind-topic').length===3);
    check('root cannot be deleted or given a sibling',button('删除主题及分支').disabled&&button('添加同级主题').disabled);
    input('中文中心');check('input immediately updates serialized draft',JSON.parse(draft)[0].rootTopic.title==='中文中心');
    check('notes survive title change',JSON.parse(draft)[0].rootTopic.notes.plain.content==='保留');
    button('添加子主题').click();input('新的子主题');check('child stored in standard XMind attached group',JSON.parse(draft)[0].rootTopic.children.attached.length===2);
    button('添加同级主题').click();input('同级主题');check('sibling keeps ordering',JSON.parse(draft)[0].rootTopic.children.attached[2].title==='同级主题');
    title().dispatchEvent(new CompositionEvent('compositionstart'));button('添加子主题').click();check('composition refuses structural operations',editor.isComposing()&&composition&&blocked===1);
    const picker=parent.querySelector('select');picker.value='1';picker.dispatchEvent(new Event('change'));check('composition keeps current sheet',picker.value==='0'&&blocked===2);
    title().dispatchEvent(new CompositionEvent('compositionend'));check('composition end restores operations',!editor.isComposing()&&!composition);
    button('删除主题及分支').click();check('one delete removes selected subtree',JSON.parse(draft)[0].rootTopic.children.attached.length===2);
    check('one delete creates a reversible history entry',!button('撤销').disabled);
    button('撤销').click();check('undo restores deleted topic',JSON.parse(draft)[0].rootTopic.children.attached.length===3);
    button('重做').click();check('redo restores deletion',JSON.parse(draft)[0].rootTopic.children.attached.length===2);
    picker.value='1';picker.dispatchEvent(new Event('change'));check('sheet switch displays literal title without HTML execution',parent.querySelectorAll('.xmind-topic').length===1&&!parent.querySelector('img')&&title().value===model[1].rootTopic.title);
    input('第二画布草稿');check('editing second sheet keeps first sheet',JSON.parse(draft)[0].rootTopic.title==='中文中心'&&JSON.parse(draft)[1].rootTopic.title==='第二画布草稿');
    editor.destroy();check('destroy clears DOM',!parent.children.length);
    editor=YingXuXmind.mount({...options,value:draft,editable:false});const before=draft;
    input('注入');button('添加子主题').click();check('read-only controls reject injected mutations',title().readOnly&&draft===before&&button('添加子主题').disabled);
    const card=parent.querySelector('.xmind-topic');check('real tree lays out horizontally with visible connectors',card.getBoundingClientRect().width>=100&&parent.querySelectorAll('.xmind-connections path').length>=2);
    document.querySelector('#result').textContent=JSON.stringify(checks);
  })();`);
  fs.writeFileSync(path.join(temporary,'fixture.html'),'<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="styles.css"><link rel="stylesheet" href="appearance.css"><link rel="stylesheet" href="xmind-editor.css"><style>body{display:block}#mount{width:900px;height:700px}</style><div id="mount"></div><pre id="result"></pre><script src="xmind-editor.js"></script><script src="runner.js"></script>');
  const {stdout}=await promisify(execFile)(browser,['--headless','--disable-gpu','--no-first-run','--disable-background-networking',`--user-data-dir=${path.join(temporary,'profile')}`,'--virtual-time-budget=3000','--dump-dom',pathToFileURL(path.join(temporary,'fixture.html')).href],{windowsHide:true,timeout:30000,maxBuffer:2*1024*1024});
  const match=stdout.match(/<pre id="result">([^<]+)<\/pre>/);assert.ok(match,stdout.slice(-1500));
  const result=JSON.parse(match[1].replace(/&quot;/g,'"').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>'));
  assert.equal(result.length,18);for(const row of result)assert.equal(row.ok,true,row.name);
});

test('right-click creation captures project and folder and retries only display after commit',async()=>{
  const dialogs=[],calls=[],nodes=new Map();let failRefresh=true;
  const node=key=>{if(!nodes.has(key))nodes.set(key,{value:'',dataset:{},open:false,addEventListener(){}});return nodes.get(key);};
  const context=vm.createContext({console,setTimeout,clearTimeout,window:{},document:{querySelector:node},localStorage:{getItem:()=>null,setItem(){}},FormData:class{constructor(form){this.form=form;}get(key){return this.form[key];}}});
  const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
  vm.runInContext(source+'\nglobalThis.app={state,menuCommands,newXmindDialog};',context);
  context.capture=options=>dialogs.push(options);context.call=async(url,options)=>{calls.push({url,options});return {id:'new-xmind'};};
  context.refresh=async()=>{if(failRefresh){failRefresh=false;throw Error('refresh unavailable');}};
  vm.runInContext('guardProperties=async()=>true;showDialog=capture;api=call;refreshProjects=refresh;renderNavigation=()=>{};renderHero=()=>{};loadItems=async()=>{};openItem=async id=>globalThis.opened=id;',context);
  for(const kind of ['location','folder','project'])assert.ok(context.app.menuCommands(kind,null,0,false).some(command=>command?.[0]==='new-xmind'));
  const target={project_id:'captured',category:'scripts',folder_id:'nested'};await context.app.newXmindDialog(target);
  target.project_id='changed';context.app.state.projectId='another';
  await assert.rejects(dialogs[0].onSubmit({name:'故事'}),/refresh unavailable/);
  await dialogs[0].onSubmit({name:'故事'});
  assert.equal(calls.length,1);assert.equal(calls[0].options.body.project_id,'captured');assert.equal(calls[0].options.body.folder_id,'nested');assert.equal(calls[0].options.body.format,'xmind');
  assert.equal(context.opened,'new-xmind');assert.equal(context.app.state.folderId,'nested');
});
