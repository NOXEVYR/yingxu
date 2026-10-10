'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');

const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');

class MockNode {
  constructor(){this.value='';this.open=false;this.dataset={};this.handlers={};this.attributes={};this.innerHTML='';this.textContent='';}
  addEventListener(type,handler){(this.handlers[type] ||= []).push(handler);}
  fire(type,event={}){for(const handler of this.handlers[type] || [])handler(event);}
  setAttribute(key,value){this.attributes[key]=value;}
  contains(){return true;}
  querySelector(selector){return this.children?.get(selector) || null;}
  replaceChildren(){this.replaced=true;}
  close(){this.open=false;this.fire('close');}
}
class MockDialog extends MockNode {
  constructor(){super();this.children=new Map();for(const selector of ['#xmindLinkSearch','#xmindLinkResults','#xmindLinkStatus','#xmindLinkPrev','#xmindLinkNext','#xmindLinkPage','#dialogActions [data-xmind-link-cancel]'])this.children.set(selector,new MockNode());this.open=true;}
}
function harness(){
  const nodes=new Map(),opened=[],toasts=[],dialogs=[];
  const document={querySelector(selector){if(!nodes.has(selector))nodes.set(selector,new MockNode());return nodes.get(selector);}};
  const window={location:{href:'http://127.0.0.1/'},open:(...args)=>{opened.push(args);return {};}};
  const context=vm.createContext({console,window,document,localStorage:{getItem:()=>null,setItem(){}},URL,AbortController,setTimeout,clearTimeout});
  vm.runInContext(source+`\nglobalThis.appTest={state,openXmindLink,pickXmindLink,xmindLinkResultHtml,renderEditorBody,unmountXmindEditor,setTab(tab,mount){state.tabs=[tab];state.activeKey=tab.key;activeXmindEditorTab=tab;tab.xmindLinkMount=mount;state.projectId='project-a';state.section='assets';state.category='all';state.folderId=null;state.folderScope='current';state.q='';state.status='';state.kind='';state.sort='updated';state.offset=0;state.listSequence=1;},setStubs(values){if(values.api)api=values.api;if(values.toast)toast=values.toast;if(values.showDialog)showDialog=values.showDialog;if(values.guardProperties)guardProperties=values.guardProperties;if(values.refreshProjects)refreshProjects=values.refreshProjects;if(values.recordProjectVisit)recordProjectVisit=values.recordProjectVisit;if(values.renderNavigation)renderNavigation=values.renderNavigation;if(values.renderHero)renderHero=values.renderHero;if(values.configureSection)configureSection=values.configureSection;if(values.renderWorkspace)renderWorkspace=values.renderWorkspace;if(values.loadItems)loadItems=values.loadItems;if(values.openItem)openItem=values.openItem;if(values.unmountDocxEditor)unmountDocxEditor=values.unmountDocxEditor;if(values.unmountDocxPreview)unmountDocxPreview=values.unmountDocxPreview;if(values.stopImageZoomTracking)stopImageZoomTracking=values.stopImageZoomTracking;if(values.stopPreviewMedia)stopPreviewMedia=values.stopPreviewMedia;}};`,context);
  const app=context.appTest;
  const sourceTab={key:'file:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',source:'file',item:{id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',project_id:'project-a',kind:'xmind',name:'路线图'},dirty:true,draft:'未保存的 XMind 草稿'};
  const mount={};
  app.setTab(sourceTab,mount);
  context.testOpened=opened;context.testToasts=toasts;context.testDialogs=dialogs;
  app.setStubs({toast:(message,type)=>toasts.push({message,type}),showDialog:options=>{const dialog=new MockDialog();dialog.options=options;dialogs.push(dialog);return dialog;}});
  return {app,context,nodes,opened,toasts,dialogs,sourceTab,mount};
}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));

test('XMind item links navigate across projects and preserve the source draft',async()=>{
  const h=harness(),target='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',calls=[];
  h.app.setStubs({
    api:async path=>{calls.push(path);return {id:target,project_id:'project-b',category:'scripts',folder_id:'folder-b',name:'linked note'};},
    guardProperties:async()=>true,refreshProjects:async()=>{},recordProjectVisit:async()=>{},
    renderNavigation:()=>{},renderHero:()=>{},configureSection:()=>{},renderWorkspace:()=>{},loadItems:async()=>{},openItem:async id=>{h.context.openedId=id;h.app.state.activeKey=`file:${id}`;}
  });
  const opened=await h.app.openXmindLink(h.sourceTab,h.mount,`yingxu://item/${target}`);
  assert.equal(opened,true);assert.deepEqual(calls,[`/api/items/${target}`]);assert.equal(h.context.openedId,target);
  assert.equal(h.app.state.projectId,'project-b');assert.equal(h.app.state.category,'scripts');assert.equal(h.app.state.folderId,'folder-b');
  assert.ok(h.app.state.tabs.includes(h.sourceTab));assert.equal(h.sourceTab.draft,'未保存的 XMind 草稿');assert.equal(h.sourceTab.dirty,true);
});

test('malformed and unsafe XMind links are refused, while HTTP(S) uses the safe external opener',async()=>{
  const h=harness();
  for(const href of ['javascript:alert(1)','file:///C:/secret.txt','yingxu://item/../aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','yingxu://item/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?x=1','https://user:pass@example.com/','https://example.com/a b','https://example.com/a\\b','https://example.com/a\npath',`https://example.com/${'a'.repeat(4100)}`])
    assert.equal(await h.app.openXmindLink(h.sourceTab,h.mount,href),false,href);
  assert.equal(h.opened.length,0);assert.equal(h.toasts.length,9);
  assert.equal(await h.app.openXmindLink(h.sourceTab,h.mount,'HTTPS://example.com/story'),true);
  assert.deepEqual(h.opened,[['https://example.com/story','_blank','noopener,noreferrer']]);
});

test('HTTP(S) desktop links use only the advertised native URL bridge',async()=>{
  const h=harness(),messages=[];
  h.context.window.yingxuDesktopOpenWebUrl=true;
  h.context.window.chrome={webview:{postMessage:message=>messages.push(message)}};
  assert.equal(await h.app.openXmindLink(h.sourceTab,h.mount,'https://example.com/story'),true);
  assert.equal(JSON.stringify(messages),JSON.stringify([{action:'open-web-url',url:'https://example.com/story'}]));
  assert.equal(h.opened.length,0);
});

test('desktop bridge dispatch errors become a visible link failure',async()=>{
  const h=harness();
  h.context.window.yingxuDesktopOpenWebUrl=true;
  h.context.window.chrome={webview:{postMessage(){throw new Error('native unavailable');}}};
  assert.equal(await h.app.openXmindLink(h.sourceTab,h.mount,'https://example.com/story'),false);
  assert.match(h.toasts.at(-1).message,/native unavailable/);
});

test('removed link targets show the server error and leave the current workspace alone',async()=>{
  const h=harness(),target='cccccccccccccccccccccccccccccccc';
  h.app.setStubs({api:async()=>{const error=new Error('这个文件已移入回收站。');error.status=404;throw error;}});
  assert.equal(await h.app.openXmindLink(h.sourceTab,h.mount,`yingxu://item/${target}`),false);
  assert.equal(h.app.state.projectId,'project-a');assert.equal(h.app.state.tabs.length,1);
  assert.equal(h.sourceTab.draft,'未保存的 XMind 草稿');assert.match(h.toasts.at(-1).message,/回收站/);
});

test('file picker searches across projects, returns the item URL, and cancellation returns null',async()=>{
  const h=harness(),id='dddddddddddddddddddddddddddddddd';
  h.app.setStubs({api:async(path)=>{
    assert.match(path,/^\/api\/search\?q=hero&limit=20&offset=0$/);
    return {total:1,results:[{type:'item',id,name:'角色设定',project_name:'远方项目',category:'characters',snippet:'主角'}]};
  }});
  let pending=h.app.pickXmindLink(h.sourceTab,h.mount),dialog=h.dialogs.at(-1),input=dialog.querySelector('#xmindLinkSearch'),results=dialog.querySelector('#xmindLinkResults');
  input.value='hero';input.fire('input');await pause(280);
  assert.match(results.innerHTML,/远方项目/);assert.match(results.innerHTML,/角色设定/);assert.match(results.innerHTML,/角色/);
  const resultButton={dataset:{xmindLinkId:id}};
  results.fire('click',{target:{closest:selector=>selector==='[data-xmind-link-id]'?resultButton:null}});
  assert.equal(await pending,`yingxu://item/${id}`);

  pending=h.app.pickXmindLink(h.sourceTab,h.mount);dialog=h.dialogs.at(-1);
  dialog.querySelector('#dialogActions [data-xmind-link-cancel]').fire('click');
  assert.equal(await pending,null);
});

test('file picker advances through mixed global search pages',async()=>{
  const h=harness(),id='ffffffffffffffffffffffffffffffff',requests=[];
  h.app.setStubs({api:async path=>{
    requests.push(path);
    if(path.endsWith('offset=0'))return {total:21,has_more:true,results:Array.from({length:20},(_,index)=>({type:index===0?'project':'skill',id:`other-${index}`,name:`其他 ${index}`}))};
    return {total:21,has_more:false,results:[{type:'item',id,name:'跨页目标',project_name:'远方项目',category:'previs'}]};
  }});
  const pending=h.app.pickXmindLink(h.sourceTab,h.mount),dialog=h.dialogs.at(-1),input=dialog.querySelector('#xmindLinkSearch'),next=dialog.querySelector('#xmindLinkNext'),results=dialog.querySelector('#xmindLinkResults');
  input.value='scene';input.fire('input');await pause(280);
  assert.equal(next.disabled,false);next.fire('click');await pause(0);
  assert.deepEqual(requests,['/api/search?q=scene&limit=20&offset=0','/api/search?q=scene&limit=20&offset=20']);
  assert.match(results.innerHTML,/跨页目标/);assert.match(results.innerHTML,/远方项目/);
  const resultButton={dataset:{xmindLinkId:id}};
  results.fire('click',{target:{closest:selector=>selector==='[data-xmind-link-id]'?resultButton:null}});
  assert.equal(await pending,`yingxu://item/${id}`);
});

test('a stale picker cannot return a target after its XMind tab changes',async()=>{
  const h=harness(),id='eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee';
  h.app.setStubs({api:async()=>({total:1,results:[{type:'item',id,name:'目标',project_name:'另一项目'}]})});
  const pending=h.app.pickXmindLink(h.sourceTab,h.mount),dialog=h.dialogs.at(-1),input=dialog.querySelector('#xmindLinkSearch'),results=dialog.querySelector('#xmindLinkResults');
  input.value='target';input.fire('input');await pause(280);
  h.app.state.activeKey='file:another';
  const resultButton={dataset:{xmindLinkId:id}};
  results.fire('click',{target:{closest:selector=>selector==='[data-xmind-link-id]'?resultButton:null}});
  assert.equal(await pending,null);
});

test('XMind editor mount receives the picker and open-link callbacks',async()=>{
  const h=harness(),captured=[];
  h.app.setStubs({unmountDocxEditor:()=>{},unmountDocxPreview:()=>{},stopImageZoomTracking:()=>{},stopPreviewMedia:()=>{}});
  h.context.captureMount=options=>{captured.push(options);return {destroy(){}};};
  vm.runInContext('window.YingXuXmind={mount:globalThis.captureMount};',h.context);
  h.sourceTab.mode='edit';h.sourceTab.loading=false;h.app.renderEditorBody(h.sourceTab);
  assert.equal(captured.length,1);assert.equal(typeof captured[0].onPickLink,'function');assert.equal(typeof captured[0].onOpenLink,'function');
  const pending=captured[0].onPickLink(),dialog=h.dialogs.at(-1);
  dialog.querySelector('#dialogActions [data-xmind-link-cancel]').fire('click');
  assert.equal(await pending,null);
  h.app.unmountXmindEditor();assert.equal(h.sourceTab.xmindLinkMount,undefined);
});
