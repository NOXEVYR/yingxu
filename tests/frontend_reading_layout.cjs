'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
function setup() {
  const nodes=new Map(),calls=[],created=[];
  const context=vm.createContext({setTimeout,clearTimeout,console,localStorage:{setItem:()=>{},getItem:()=>null},window:{YingXuMarkdown:{supports:value=>value.length<=500000&&!/\r\n.*(?<!\r)\n/s.test(value),create:options=>{const item={options,value:options.value,destroyed:false,setValue(value){this.value=value;options.onChange(value,{origin:'setValue'});},setMode(mode){this.mode=mode;},mount(parent){this.parent=parent;},destroy(){this.destroyed=true;},isComposing(){return !!this.composing;}};created.push(item);return item;}}},document:{querySelector:key=>{if(!nodes.has(key))nodes.set(key,{innerHTML:'',addEventListener(){},querySelector(){return null;},focus(){},classList:{values:new Set(),toggle(name,on){if(on)this.values.add(name);else this.values.delete(name);}}});return nodes.get(key);},querySelectorAll:()=>[]}});
  const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
  vm.runInContext(source+`\nrenderTabs=()=>{};renderEditorToolbar=()=>{};renderEditorStatus=()=>{};renderInspector=()=>{};updatePreview=()=>{};toast=()=>{};report=()=>{};refreshProjects=async()=>{};loadItems=()=>{};globalThis.app={readingDocument,applyReadingLayout,toggleReadingLibrary,state,preserveTextNewlines,editableMarkdown,canUseMarkdownEditor,mountMarkdownEditor,discardUnusedMarkdownEditors,markdownInputReady,saveTab,prepareTabs,persistDrafts,draftStateLabel,markdown,markdownDocumentHeading,markdownToolbarHtml,renderEditorBody,applyMarkdownFormat,filenameParts,renameTabFile,renameDialog};`,context);
  return {...context.app,context,nodes,calls,created};
}
function tab(value='# 标题\n\n正文\n') {return {key:'file:1',id:'1',source:'file',item:{kind:'markdown',name:'笔记'},content:{editable:true,etag:'old'},draft:value,mode:'live',dirty:false};}

test('reading layout defaults to a large document and restores the list without remounting a dirty composing editor',()=>{
  const s=setup(),t=tab('未保存的正文');s.state.tabs=[t];s.state.activeKey=t.key;s.mountMarkdownEditor(t,{});
  const editor=t.markdownEditor;editor.composing=true;t.dirty=true;const history={};editor.history=history;
  s.applyReadingLayout(t);const classes=s.nodes.get('#workspace').classList.values;
  assert.ok(classes.has('reading-focused'));
  s.toggleReadingLibrary();assert.equal(t.showLibrary,true);assert.ok(!classes.has('reading-focused'));
  s.toggleReadingLibrary();assert.ok(classes.has('reading-focused'));
  assert.equal(t.markdownEditor,editor);assert.equal(editor.history,history);assert.equal(editor.composing,true);assert.equal(t.draft,'未保存的正文');assert.equal(t.dirty,true);
  for(const kind of ['markdown','text','skill','docx','pdf','html'])assert.equal(s.readingDocument({item:{kind}}),true);
  for(const kind of ['image','svg','video','audio','excalidraw']){s.applyReadingLayout({item:{kind}});assert.ok(!classes.has('reading-focused'));assert.ok(!classes.has('reading-document'));}
  s.applyReadingLayout(null);assert.ok(!classes.has('reading-focused'));
});
