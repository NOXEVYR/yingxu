'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8').replace(/boot\(\);\s*$/, '');

function setup(folder = 'personal') {
  const calls = [], notices = [], dialogs = [], nodes = new Map(), saved = new Map();
  const projects = [{id:'existing', name:'原项目', counts:{}}];
  const library = {folders:[{id:'personal', name:'个人作品'}, {id:'child', name:'子分类', parent_id:'personal'}, {id:'other', name:'其他'}], projects:[{id:'existing', folder_id:null}], recent_ids:[]};
  const fakeApi = async (url, options = {}) => {
    calls.push({url, options});
    if (url === '/api/projects' && options.method === 'POST') {
      const body = options.body;
      if (body.folder_id && !library.folders.some(item => item.id === body.folder_id)) throw new Error('项目分类不存在，请刷新后重试。');
      const project = {id:'created', name:body.name, description:body.description, counts:{}};
      projects.push(project); library.projects.push({...project, folder_id:body.folder_id});
      return project;
    }
    if (url === '/api/projects') return {projects:[...projects]};
    if (url === '/api/project-library') return structuredClone(library);
    if (/^\/api\/project-library\/[^/]+\/visit$/.test(url)) return {};
    throw new Error(`Unexpected request: ${url}`);
  };
  const context = vm.createContext({setTimeout, clearTimeout, URLSearchParams, console, window:{}, dialogs, notices, fakeApi,
    FormData:class {constructor(values) {this.values = values;} get(key) {return this.values[key];}},
    document:{querySelector:id => {if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id);}},
    localStorage:{getItem:key => saved.get(key) ?? null, setItem:(key,value) => saved.set(key,String(value))}});
  vm.runInContext(source + `\napi=fakeApi;showDialog=options=>{dialogs.push(options);return {};};guardProperties=async()=>true;renderHero=()=>{};renderInspector=()=>{};loadSection=async()=>{};toast=message=>notices.push(message);globalThis.app={state,newProjectDialog,sidebarProjects,sidebarProjectFolder,renderNavigation};`, context);
  Object.assign(context.app.state, {projects:[...projects], projectId:'existing', projectLibrary:structuredClone(library), projectLibraryFolder:folder,
    bootstrap:{capabilities:{project_library:true}}, tabs:[{key:'draft',dirty:true,draft:'未保存正文'}], activeKey:'draft'});
  return {...context.app, context, calls, notices, dialogs, nodes, saved, projects, library,
    submit:() => dialogs.at(-1).onSubmit({name:' 新项目 ', description:' 合成介绍 '}),
    visible:() => Array.from(context.app.sidebarProjects(), project => project.id)};
}

for (const folder of ['personal','child']) test(`creation belongs to selected direct library folder ${folder} and appears immediately`, async () => {
  const s = setup(folder); s.renderNavigation(); assert.match(s.nodes.get('#projectList').innerHTML, /此分类暂无项目/);
  await s.newProjectDialog(); await s.submit();
  const writes = s.calls.filter(call => call.options.method === 'POST');
  assert.equal(writes[0].url, '/api/projects'); assert.equal(writes[0].options.body.folder_id, folder);
  assert.equal(writes[0].options.body.name, '新项目'); assert.equal(writes[0].options.body.description, '合成介绍');
  assert.deepEqual(s.visible(), ['created']); assert.match(s.nodes.get('#projectList').innerHTML, /data-project="created"/);
  assert.equal(s.state.projectLibraryFolder, folder); assert.equal(s.state.projectId, 'created');
  assert.equal(s.saved.get('yingxu:project'), 'created'); assert.equal(s.state.tabs[0].draft, '未保存正文'); assert.equal(s.state.tabs[0].dirty, true);
  assert.equal(writes.filter(call => call.url === '/api/projects').length, 1);
  assert.ok(!writes.some(call => call.url.includes('/assign')), 'creation must not use a separate assignment request');
});

for (const folder of ['*','']) test(`creation from ${folder || 'unclassified'} stays unclassified and preserves sidebar filter`, async () => {
  const s = setup(folder); await s.newProjectDialog(); await s.submit();
  assert.equal(s.calls.find(call => call.options.method === 'POST').options.body.folder_id, null);
  assert.equal(s.library.projects.find(project => project.id === 'created').folder_id, null);
  assert.equal(s.state.projectLibraryFolder, folder); assert.ok(s.visible().includes('created'));
});

test('dialog captures the chosen destination without redirecting to later sidebar state', async () => {
  const s = setup(); await s.newProjectDialog(); s.state.projectLibraryFolder = 'other'; await s.submit();
  assert.equal(s.calls.find(call => call.options.method === 'POST').options.body.folder_id, 'personal');
  assert.equal(s.state.projectLibraryFolder, 'other'); assert.deepEqual(s.visible(), []);
});

test('deleted destination after dialog opens fails without creating an unclassified project or success message', async () => {
  const s = setup(); await s.newProjectDialog(); s.library.folders = s.library.folders.filter(folder => folder.id !== 'personal');
  // A refreshed UI snapshot must not silently change the dialog's captured target.
  s.state.projectLibrary.folders = structuredClone(s.library.folders);
  await assert.rejects(s.submit(), /项目分类不存在/);
  assert.equal(s.projects.length, 1); assert.equal(s.state.projectId, 'existing'); assert.equal(s.state.activeKey, 'draft');
  assert.equal(s.state.tabs[0].draft, '未保存正文'); assert.equal(s.notices.length, 0); assert.equal(s.calls.length, 1);
});

test('stale saved category already absent when dialog opens uses the existing all-projects fallback', async () => {
  const s = setup('removed'); await s.newProjectDialog(); await s.submit();
  assert.equal(s.calls.find(call => call.options.method === 'POST').options.body.folder_id, null);
  assert.equal(s.state.projectLibraryFolder, '*'); assert.ok(s.visible().includes('created'));
});

test('cancelled property guard never opens the create dialog or sends a request', async () => {
  const s = setup(); vm.runInContext('guardProperties=async()=>false;', s.context); await s.newProjectDialog();
  assert.equal(s.dialogs.length, 0); assert.equal(s.calls.length, 0); assert.equal(s.state.projectId, 'existing');
});

test('retry after a refresh failure displays the created project without creating a duplicate', async () => {
  const s = setup();
  vm.runInContext(`let failRefresh=true; api=async(url,options={})=>{
    if(url==='/api/projects' && !options.method && failRefresh) { failRefresh=false; throw new Error('刷新失败'); }
    return fakeApi(url,options);
  };`, s.context);
  await s.newProjectDialog();
  await assert.rejects(s.submit(), /项目已创建.*刷新失败/);
  assert.equal(s.projects.length, 2); assert.equal(s.notices.length, 0);
  assert.equal(s.nodes.get('#projectName').readOnly, true);
  assert.equal(s.nodes.get('#dialogActions button[type="submit"]').textContent, '重试显示项目');
  await s.submit();
  assert.equal(s.calls.filter(call => call.url === '/api/projects' && call.options.method === 'POST').length, 1);
  assert.equal(s.projects.length, 2); assert.deepEqual(s.visible(), ['created']);
  assert.equal(s.state.projectId, 'created'); assert.equal(s.notices.length, 1);
});
