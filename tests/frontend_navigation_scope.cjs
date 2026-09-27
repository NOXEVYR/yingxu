'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8').replace(/boot\(\);\s*$/, '');

function setup(apiReply) {
  const nodes = new Map();
  const requests = [];
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      innerHTML: '', textContent: '', className: '', hidden: false, disabled: false,
      value: '', scrollTop: 0, placeholder: '',
      classList: {toggle() {}}, addEventListener() {}, replaceChildren() {},
    });
    return nodes.get(selector);
  };
  const document = {querySelector: node, querySelectorAll: () => []};
  const context = vm.createContext({
    console, document, window: {}, URLSearchParams, AbortController,
    localStorage: {getItem: () => null, setItem() {}}, requests,
  });
  vm.runInContext(source + `
    renderWorkspace=()=>{};renderNavigation=()=>{};renderHero=()=>{};renderInspector=()=>{};
    hideMenu=()=>{};updateSelection=()=>{};renderFolders=()=>{};updatePagination=()=>{};
    observeThumbnails=()=>{};renderResourceGroups=()=>{};groupController=()=>null;
    skillSourcesHtml=()=>'';acceptSkillSources=()=>{};
    api=async (url,options)=>{requests.push(url);return globalThis.apiReply(url,options);};
    globalThis.audit={state,loadItems,renderItems,selectSection,selectCategory,returnToWorkspace};
  `, context);
  context.apiReply = apiReply;
  return {...context.audit, nodes, requests, node};
}

function queryFor(requests, prefix) {
  const url = requests.find(value => value.startsWith(prefix));
  assert.ok(url, `expected a ${prefix} request`);
  return new URLSearchParams(url.slice(url.indexOf('?') + 1));
}

test('switching view during a slow project load never displays cards from the old project', async () => {
  let finish;
  const pending = new Promise(resolve => { finish = resolve; });
  const s = setup(url => url.startsWith('/api/items?') ? pending : {folders: []});
  Object.assign(s.state, {
    projectId: 'B', section: 'assets', category: 'all', view: 'grid',
    items: [{id: 'old-item', project_id: 'A', name: 'A only document', kind: 'file'}], total: 1,
  });

  const loading = s.loadItems();
  assert.match(s.nodes.get('#resourceItems').innerHTML, /skeleton/);
  s.state.view = 'list';
  s.renderItems(); // The view button calls this synchronously while the request is pending.
  assert.equal(/A only document|old-item/.test(s.nodes.get('#resourceItems').innerHTML), false,
    'loading project B must not render project A cards');

  finish({items: [{id: 'new-item', project_id: 'B', name: 'B only document', kind: 'file'}], total: 1, categories: []});
  await loading;
  assert.match(s.nodes.get('#resourceItems').innerHTML, /B only document/);
  assert.doesNotMatch(s.nodes.get('#resourceItems').innerHTML, /A only document/);
});

test('workspace switches start with an unfiltered search and return restores the asset query', async () => {
  const s = setup(url => {
    if (url.startsWith('/api/skills?')) return {skills: []};
    if (url.startsWith('/api/trash?')) return {entries: [], total: 0};
    if (url.startsWith('/api/items?')) return {items: [], total: 0, categories: []};
    return {folders: []};
  });
  Object.assign(s.state, {
    projects: [{id: 'p'}], projectId: 'p', section: 'assets',
    category: 'scripts', q: '夜景', items: [], total: 0,
  });
  s.node('#searchInput').value = '夜景';

  await s.selectSection('skills');
  assert.equal(queryFor(s.requests, '/api/skills?').get('q') || '', '');
  assert.equal(s.nodes.get('#searchInput').value, '');
  assert.match(s.nodes.get('#resourceItems').innerHTML, /empty-state/);
  assert.doesNotMatch(s.nodes.get('#resourceItems').innerHTML, /没有符合条件的 SKILL/);

  s.state.q = '规范';
  s.nodes.get('#searchInput').value = '规范';
  await s.selectSection('trash');
  assert.equal(queryFor(s.requests, '/api/trash?').get('q') || '', '');
  assert.equal(s.nodes.get('#searchInput').value, '');
  assert.match(s.nodes.get('#resourceItems').innerHTML, /empty-state/);

  await s.returnToWorkspace();
  assert.equal(queryFor(s.requests, '/api/items?').get('q'), '夜景');
  assert.equal(s.nodes.get('#searchInput').value, '夜景');
});

test('clicking an asset category from a workspace section clears its unrelated query', async () => {
  const s = setup(url => url.startsWith('/api/items?') ? {items: [], total: 0} : {folders: []});
  Object.assign(s.state, {projectId: 'p', section: 'skills', q: '规范'});
  s.node('#searchInput').value = '规范';
  await s.selectCategory('scripts');
  assert.equal(queryFor(s.requests, '/api/items?').get('q'), null);
  assert.equal(s.nodes.get('#searchInput').value, '');
});
