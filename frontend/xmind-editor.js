/* Offline XMind workspace. Preserve file data; keep camera/history outside the archive. */
(function () {
  'use strict';
  const children = t => Object.entries(t.children || {}).flatMap(([group, list]) => list.map(topic => ({topic, group})));
  const uid = () => crypto.randomUUID().replaceAll('-', '');
  const clamp = (n, a, b) => Math.max(a, Math.min(b, n));
  function mount({parent, value, editable, selectedId, sheetIndex = 0, notice, viewState = {}, onChange, onSelection, onViewChange, onComposition, onBlocked, onCopy}) {
    let sheets = JSON.parse(value), current = clamp(sheetIndex, 0, sheets.length - 1), selected = selectedId;
    let composing = false, destroyed = false, editing = null, fieldSession = null, deleting = false, pan = null, space = false, dragId = null;
    let entries = [], byId = new Map(), visible = [], positions = new Map(), cards = new Map(), matches = [], bounds;
    let zoom = 1, x = 0, y = 0, initialized = false, layout = viewState.layout === 'right' ? 'right' : 'balanced';
    const folded = new Set(viewState.folded || []), cameras = {...viewState.cameras}, undo = [...(viewState.undo || [])], redo = [...(viewState.redo || [])];
    const abort = new AbortController(), signal = abort.signal;
    const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
    // Recycled node listeners are collected with their nodes; only global listeners need abort.
    const listen = (n, type, fn, options = {}) => n.addEventListener(type, fn, {...options, ...(n === document || n === window ? {signal} : {})});
    const root = el('section', undefined, 'xmind-editor'); root.setAttribute('aria-label', '思维导图工作区');
    function button(host, text, label, action, cls = '') { const b = el('button', text, `xmind-button ${cls}`); b.type = 'button'; b.title = label; b.setAttribute('aria-label', label); listen(b, 'click', action); host.appendChild(b); return b; }
    const header = el('div', undefined, 'xmind-header'), tools = el('div', undefined, 'xmind-tools');
    const picker = el('select'); picker.setAttribute('aria-label', '思维导图画布'); header.append(picker, el('span', editable ? '可编辑' : '只读预览', 'xmind-mode'));
    const outlineButton = button(header, '大纲', '显示或收起大纲', () => panel('outline'));
    const propertiesButton = button(header, '主题属性', '显示或收起主题属性', () => panel('properties'));
    const sheetButton = button(header, '＋画布', '新建画布', () => mutate(() => {
      if (sheets.length >= 100 || totalTopics() >= 5000) return blocked('已达到画布或主题数量上限。');
      saveCamera(); const sheet = {id:uid(), class:'sheet', title:`画布 ${sheets.length + 1}`, rootTopic:{id:uid(), class:'topic', title:'中心主题', structureClass:'org.xmind.ui.map.unbalanced'}};
      sheets.push(sheet); current = sheets.length - 1; selected = sheet.rootTopic.id; initialized = false; return true;
    }));
    const searchBox = el('div', undefined, 'xmind-search'), search = el('input'), matchLabel = el('span', '', 'xmind-search-count');
    search.type = 'search'; search.placeholder = '搜索主题'; search.setAttribute('aria-label', '搜索导图主题'); searchBox.append(search, matchLabel);
    button(searchBox, '↑', '上一个匹配主题', () => stepMatch(-1)); button(searchBox, '↓', '下一个匹配主题', () => stepMatch(1)); header.appendChild(searchBox);
    const childButton = button(tools, '＋子主题', '添加子主题 · Tab', () => add(false));
    const siblingButton = button(tools, '＋同级', '添加同级主题 · Enter', () => add(true));
    const editButton = button(tools, '编辑文字', '编辑主题 · 双击 / F2', () => startEdit());
    const deleteButton = button(tools, '删除分支', '删除主题及分支 · Delete', requestDelete);
    tools.appendChild(el('span', undefined, 'xmind-divider'));
    const undoButton = button(tools, '↶', '撤销 · Ctrl / ⌘ Z', () => history(false)), redoButton = button(tools, '↷', '重做 · Ctrl / ⌘ Shift Z', () => history(true));
    button(tools, '复制主题', '复制主题文字 · Ctrl / ⌘ C', () => copy(false)); button(tools, '复制大纲', '复制此分支大纲 · Ctrl / ⌘ Shift C', () => copy(true));
    tools.appendChild(el('span', undefined, 'xmind-divider'));
    const foldButton = button(tools, '折叠分支', '折叠或展开当前分支 · Ctrl / ⌘ Space', () => fold(selected));
    button(tools, '全部展开', '展开所有主题', () => { if (!ready()) return; finishEdit(); folded.clear(); render(); });
    const direction = el('select'); direction.setAttribute('aria-label', '导图布局');
    for (const [key, text] of [['balanced', '左右展开'], ['right', '向右展开']]) { const o = el('option', text); o.value = key; direction.appendChild(o); } direction.value = layout; tools.appendChild(direction);
    const help = el('details', undefined, 'xmind-help'); help.append(el('summary', '操作提示'), el('p', '双击主题编辑；拖选文字后 Ctrl+C 复制。Tab 添加子主题，Enter 添加同级，F2 编辑，Delete 删除，Ctrl+Z 撤销。Ctrl+滚轮围绕鼠标缩放，中键或空白处拖动画布；按住空格可从主题上拖动。拖动选中主题的右下角手柄到另一主题，可移动整个分支。')); tools.appendChild(help);
    const body = el('div', undefined, 'xmind-body'), outline = el('aside', undefined, 'xmind-outline'), properties = el('aside', undefined, 'xmind-properties');
    outline.setAttribute('aria-label', '主题大纲'); properties.setAttribute('aria-label', '主题属性');
    for (const [host, name, key] of [[outline, '大纲', 'outline'], [properties, '主题属性', 'properties']]) { const head = el('div', undefined, 'xmind-panel-heading'); head.appendChild(el('strong', name)); button(head, '×', `收起${name}`, () => panel(key)); host.appendChild(head); }
    const outlineList = el('div', undefined, 'xmind-outline-list'); outline.appendChild(outlineList);
    const stage = el('div', undefined, 'xmind-stage'); stage.tabIndex = 0; stage.setAttribute('aria-label', '思维导图画布，双击编辑，Ctrl 加滚轮缩放');
    const world = el('div', undefined, 'xmind-world'), nodeLayer = el('div', undefined, 'xmind-nodes');
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.classList.add('xmind-connections'); svg.setAttribute('aria-hidden', 'true'); world.append(svg, nodeLayer); stage.appendChild(world);
    function field(label, aria, rows) { const host = el('label', undefined, 'xmind-field'); host.appendChild(el('span', label)); const input = el('textarea'); input.rows = rows; input.setAttribute('aria-label', aria); host.appendChild(input); properties.appendChild(host); return input; }
    const title = field('主题文字', '主题文字', 3), notes = field('备注', '主题备注', 6), sheetTitle = field('画布名称', '画布名称', 1);
    const notesHint = el('p', '', 'xmind-field-hint'), metadata = el('div', undefined, 'xmind-metadata'); notes.parentElement.appendChild(notesHint); properties.appendChild(metadata);
    const styleTools = el('div', undefined, 'xmind-style-tools'); properties.appendChild(styleTools);
    const boldButton = button(styleTools, 'B', '主题文字加粗', () => setStyle({'fo:font-weight': topicStyle(selectedEntry().topic)['fo:font-weight'] === 'bold' ? 'normal' : 'bold'}));
    const italicButton = button(styleTools, 'I', '主题文字斜体', () => setStyle({'fo:font-style': topicStyle(selectedEntry().topic)['fo:font-style'] === 'italic' ? 'normal' : 'italic'}));
    const fontSize = el('select'); fontSize.setAttribute('aria-label', '主题字号');
    for (const size of ['', '10pt', '12pt', '14pt', '18pt', '24pt']) { const o = el('option', size || '默认字号'); o.value = size; fontSize.appendChild(o); } styleTools.appendChild(fontSize);
    const colorLabel = el('label', undefined, 'xmind-field'); colorLabel.appendChild(el('span', '主题配色')); const colors = el('select'); colors.setAttribute('aria-label', '主题配色');
    for (const [fill, text, name] of [['', '', '默认配色'], ['#e6edf2', '#283e50', '雾蓝'], ['#e8edde', '#35402a', '松绿'], ['#f2e8da', '#59412b', '暖纸'], ['#ece6f0', '#4b385c', '淡紫'], ['#f1e3e6', '#673943', '浅玫瑰'], ['#24292d', '#ffffff', '石墨']]) { const o = el('option', name); o.value = fill ? `${fill}|${text}` : ''; colors.appendChild(o); } colorLabel.appendChild(colors); properties.appendChild(colorLabel);
    const moveField = el('label', undefined, 'xmind-field'), moveTarget = el('select'); moveField.appendChild(el('span', '将当前分支移到')); moveTarget.setAttribute('aria-label', '移动分支目标'); moveField.appendChild(moveTarget); properties.appendChild(moveField);
    const moveButton = button(properties, '移动到所选主题下', '移动到所选主题下', () => move(selected, moveTarget.value));
    const orderTools = el('div', undefined, 'xmind-order-tools'); properties.appendChild(orderTools);
    const upButton = button(orderTools, '上移', '分支顺序上移', () => reorder(-1)), downButton = button(orderTools, '下移', '分支顺序下移', () => reorder(1));
    body.append(outline, stage, properties);
    const footer = el('div', undefined, 'xmind-footer'), breadcrumb = el('div', undefined, 'xmind-breadcrumb'), stats = el('span', '', 'xmind-stats'), zoomTools = el('div', undefined, 'xmind-zoom-tools');
    button(zoomTools, '−', '缩小导图', () => zoomAt(zoom / 1.2)); const zoomButton = button(zoomTools, '100%', '恢复导图 100% · Ctrl / ⌘ 1', () => zoomAt(1));
    button(zoomTools, '＋', '放大导图', () => zoomAt(zoom * 1.2)); button(zoomTools, '适应', '适应窗口 · Ctrl / ⌘ 0', fit); button(zoomTools, '定位', '定位所选主题', () => focusTopic(selected)); footer.append(breadcrumb, stats, zoomTools);
    const info = el('div', notice || '', 'xmind-notice'); info.hidden = !notice;
    const menu = el('div', undefined, 'xmind-context-menu'); menu.hidden = true; menu.setAttribute('role', 'menu');
    for (const [text, action, writes] of [['编辑文字', () => startEdit(), true], ['添加子主题', () => add(false), true], ['添加同级主题', () => add(true), true], ['复制主题文字', () => copy(false)], ['复制分支大纲', () => copy(true)], ['折叠 / 展开', () => fold(selected)], ['删除分支', requestDelete, true]]) { const b = button(menu, text, text, () => { menu.hidden = true; action(); }); b.setAttribute('role', 'menuitem'); if (writes) b.disabled = !editable; }
    root.append(header, tools, body, footer, info, menu); parent.replaceChildren(root); outline.hidden = !viewState.outline; properties.hidden = !viewState.properties;

    function blocked(message = '请先确认正在输入的文字。') { onBlocked?.(message); return false; }
    function ready() { return !destroyed && !composing || blocked(); }
    function writeReady() { return editable && ready(); }
    function ranged(topic) { return [topic.boundaries, topic.summaries].some(value => value != null && (!Array.isArray(value) || value.length > 0)); }
    function structureReady(topic) { return !ranged(topic) || blocked('此分支含范围边界或概要，结构修改请使用 XMind；文字和样式仍可编辑。'); }
    function referenced(id) { const ids = descendants(id), links = sheets[current].relationships; if (!links) return false; if (!Array.isArray(links)) return true; return links.some(link => !link || ids.has(link.end1Id) || ids.has(link.end2Id) || !link.end1Id || !link.end2Id); }
    function snapshot() { return {content:JSON.stringify(sheets), sheet:current, selected}; }
    function trim(list) { let bytes = list.reduce((sum, row) => sum + row.content.length, 0); while (list.length > 50 || bytes > 4 * 1024 * 1024 && list.length > 1) bytes -= list.shift().content.length; }
    function saveCamera() { if (initialized && sheets[current]) cameras[sheets[current].id || String(current)] = {zoom, x, y}; }
    function getViewState() { saveCamera(); return {layout, folded:[...folded], cameras:{...cameras}, outline:!outline.hidden, properties:!properties.hidden, undo:[...undo], redo:[...redo]}; }
    function viewChanged() { onViewChange?.(getViewState()); }
    function changed() { onChange?.(JSON.stringify(sheets)); viewChanged(); }
    function remember(before = snapshot()) { undo.push(before); trim(undo); redo.length = 0; }
    function mutate(action) { if (!writeReady()) return false; finishEdit(); fieldSession = null; const before = snapshot(); if (!action()) return false; remember(before); changed(); render(); return true; }
    function totalTopics() { let n = 0; for (const sheet of sheets) { const stack = [sheet.rootTopic]; while (stack.length) { const t = stack.pop(); n++; stack.push(...children(t).map(c => c.topic)); } } return n; }
    function index() {
      entries = []; byId = new Map(); visible = [];
      const stack = [{topic:sheets[current].rootTopic, parent:null, group:null, depth:0, hidden:false, branch:0}];
      while (stack.length) { const entry = stack.pop(); entries.push(entry); byId.set(entry.topic.id, entry); if (!entry.hidden) visible.push(entry); const list = children(entry.topic); for (let i = list.length - 1; i >= 0; i--) stack.push({...list[i], parent:entry.topic, depth:entry.depth + 1, hidden:entry.hidden || folded.has(entry.topic.id), branch:entry.depth ? entry.branch : i % 6}); }
      if (!byId.has(selected)) selected = entries[0].topic.id;
    }
    const selectedEntry = () => byId.get(selected) || entries[0];
    function descendants(id) { const found = new Set(), stack = [byId.get(id)?.topic]; while (stack.length) { const t = stack.pop(); if (!t) continue; found.add(t.id); stack.push(...children(t).map(c => c.topic)); } return found; }
    function select(id, focus = false) { if (!byId.has(id) || !ready()) return; if (selected !== id) { finishEdit(); fieldSession = null; selected = id; } deleting = false; controls(); paint(); if (focus) stage.focus({preventScroll:true}); }
    function controls() {
      const entry = selectedEntry(); if (!entry) return;
      const setField = (field, text) => { if (document.activeElement !== field) field.value = text; };
      setField(title, entry.topic.title || ''); setField(sheetTitle, sheets[current].title || ''); setField(notes, entry.topic.notes?.plain?.content || '');
      const rich = Object.keys(entry.topic.notes || {}).some(key => key !== 'plain'); notes.readOnly = !editable || rich; title.readOnly = sheetTitle.readOnly = !editable;
      const style = topicStyle(entry.topic), styleEditable = editable && (!entry.topic.style || typeof entry.topic.style === 'object' && !Array.isArray(entry.topic.style) && (!entry.topic.style.properties || typeof entry.topic.style.properties === 'object' && !Array.isArray(entry.topic.style.properties)));
      boldButton.disabled = italicButton.disabled = fontSize.disabled = colors.disabled = !styleEditable;
      boldButton.setAttribute('aria-pressed', String(style['fo:font-weight'] === 'bold')); italicButton.setAttribute('aria-pressed', String(style['fo:font-style'] === 'italic'));
      fontSize.value = [...fontSize.options].some(o => o.value === style['fo:font-size']) ? style['fo:font-size'] : '';
      colors.value = [...colors.options].some(o => o.value === `${style['svg:fill']}|${style['fo:color']}`) ? `${style['svg:fill']}|${style['fo:color']}` : '';
      notesHint.textContent = rich ? '原文件含富文本备注，保留原格式；此处提供纯文本复制。' : '备注保存在 XMind 文件中。';
      childButton.disabled = editButton.disabled = sheetButton.disabled = !editable; siblingButton.disabled = deleteButton.disabled = !editable || !entry.parent;
      undoButton.disabled = !editable || !undo.length; redoButton.disabled = !editable || !redo.length; deleteButton.textContent = deleting ? '确认删除整个分支' : '删除分支';
      foldButton.disabled = !children(entry.topic).length; foldButton.textContent = folded.has(selected) ? '展开分支' : '折叠分支';
      metadata.replaceChildren(); if (entry.topic.labels?.length) metadata.appendChild(el('p', `标签：${Array.isArray(entry.topic.labels) ? entry.topic.labels.join(' · ') : String(entry.topic.labels)}`)); if (entry.topic.hyperlink || entry.topic.href) metadata.appendChild(el('p', `链接：${String(entry.topic.hyperlink || entry.topic.href)}`)); if (entry.group === 'summary') metadata.appendChild(el('p', '概要主题（保留原文件关联）'));
      breadcrumb.replaceChildren(); const path = []; let cursor = entry; while (cursor) { path.unshift(cursor); cursor = cursor.parent ? byId.get(cursor.parent.id) : null; }
      path.forEach((a, i) => { if (i) breadcrumb.appendChild(el('span', '›')); button(breadcrumb, a.topic.title || '空主题', '定位 ' + (a.topic.title || '空主题'), () => focusTopic(a.topic.id)); });
      stats.textContent = `${entries.length} 个主题 · ${visible.length} 个展开${editable ? '' : ' · 只读'}`;
      outlineButton.setAttribute('aria-pressed', String(!outline.hidden)); propertiesButton.setAttribute('aria-pressed', String(!properties.hidden)); onSelection?.(selected, current);
      for (const row of outlineList.children) row.classList.toggle('selected', row.dataset.topicId === selected);
      if (!properties.hidden) { const excluded = descendants(selected); moveTarget.replaceChildren(); for (const target of entries) if (!excluded.has(target.topic.id) && target.topic.id !== entry.parent?.id && target.group !== 'summary' && target.depth < 64) { const o = el('option', `${'　'.repeat(Math.min(4, target.depth))}${target.topic.title || '空主题'}`); o.value = target.topic.id; moveTarget.appendChild(o); } }
      moveButton.disabled = !editable || !entry.parent || entry.group === 'summary' || !moveTarget.options.length;
      const siblings = entry.parent?.children?.[entry.group] || [], at = siblings.indexOf(entry.topic); upButton.disabled = !editable || at <= 0; downButton.disabled = !editable || at < 0 || at >= siblings.length - 1;
    }
    function updatePicker() { picker.replaceChildren(); sheets.forEach((sheet, i) => { const o = el('option', sheet.title || `画布 ${i + 1}`); o.value = String(i); picker.appendChild(o); }); picker.value = String(current); }
    function topicStyle(topic) { const entry = byId.get(topic.id), key = !entry?.depth ? 'centralTopic' : entry.group === 'detached' ? 'floatingTopic' : entry.group === 'summary' ? 'summaryTopic' : entry.depth === 1 ? 'mainTopic' : 'subTopic'; const inherited = sheets[current].theme?.[key]?.properties, props = topic.style?.properties; return {...(inherited && typeof inherited === 'object' && !Array.isArray(inherited) ? inherited : {}), ...(props && typeof props === 'object' && !Array.isArray(props) ? props : {})}; }
    function textSize(entry) { const raw = topicStyle(entry.topic)['fo:font-size'], match = typeof raw === 'string' && raw.match(/^(\d+(?:\.\d+)?)(pt|px)$/); return match ? clamp(Number(match[1]) * (match[2] === 'pt' ? 4 / 3 : 1), 12, 32) : entry.depth ? 14 : 16; }
    const measure = document.createElement('canvas').getContext('2d');
    function dimensions(entry) { const font = textSize(entry), style = topicStyle(entry.topic); measure.font = `${style['fo:font-weight'] === 'bold' ? 'bold ' : ''}${font}px "Segoe UI", "Microsoft YaHei", sans-serif`; const lines = String(entry.topic.title || '（空主题）').split('\n', 6), widths = lines.map(s => measure.measureText(s.slice(0, 1000)).width), width = clamp(Math.max(76, ...widths) + 40, entry.depth ? 124 : 164, 300), n = widths.reduce((n, w) => n + Math.max(1, Math.ceil(w / (width - 40))), 0); return {width, height:Math.max(entry.depth ? 48 : 60, Math.min(n, 6) * Math.ceil(font * 1.5) + 26)}; }
    function arrange() {
      positions = new Map(); const heights = new Map(), ids = new Set(visible.map(e => e.topic.id));
      for (const e of [...visible].reverse()) { const size = dimensions(e), kids = children(e.topic).filter(c => ids.has(c.topic.id)); const span = kids.reduce((n, c) => n + heights.get(c.topic.id), 0) + Math.max(0, kids.length - 1) * 24; positions.set(e.topic.id, {...size, entry:e, x:0, y:0, side:1}); heights.set(e.topic.id, Math.max(size.height, span)); }
      const main = positions.get(entries[0].topic.id); main.x = -main.width / 2; main.y = -main.height / 2;
      const first = children(main.entry.topic).filter(c => ids.has(c.topic.id)), left = layout === 'balanced' ? first.filter((_, i) => i % 2) : [], right = first.filter(c => !left.includes(c));
      function place(list, owner, side) { let top = owner.y + owner.height / 2 - (list.reduce((n, c) => n + heights.get(c.topic.id), 0) + Math.max(0, list.length - 1) * 24) / 2; const result = []; for (const c of list) { const p = positions.get(c.topic.id), span = heights.get(c.topic.id); p.side = side; p.x = side > 0 ? owner.x + owner.width + 72 : owner.x - p.width - 72; p.y = top + span / 2 - p.height / 2; result.push(p); top += span + 24; } return result; }
      const stack = [...place(left, main, -1), ...place(right, main, 1)]; while (stack.length) { const p = stack.pop(); stack.push(...place(children(p.entry.topic).filter(c => ids.has(c.topic.id)), p, p.side)); }
      const all = [...positions.values()]; bounds = {left:Math.min(...all.map(p => p.x)), top:Math.min(...all.map(p => p.y)), right:Math.max(...all.map(p => p.x + p.width)), bottom:Math.max(...all.map(p => p.y + p.height))};
    }
    function fit() { if (!bounds || !stage.clientWidth || !stage.clientHeight) return; zoom = clamp(Math.min((stage.clientWidth - 96) / (bounds.right - bounds.left), (stage.clientHeight - 96) / (bounds.bottom - bounds.top), 1), .2, 4); x = stage.clientWidth / 2 - (bounds.left + bounds.right) / 2 * zoom; y = stage.clientHeight / 2 - (bounds.top + bounds.bottom) / 2 * zoom; initialized = true; paint(); viewChanged(); }
    function zoomAt(next, px = stage.clientWidth / 2, py = stage.clientHeight / 2) { next = clamp(next, .2, 4); const ratio = next / zoom; x = px - (px - x) * ratio; y = py - (py - y) * ratio; zoom = next; paint(); viewChanged(); }
    function reveal(id) { let entry = byId.get(id); while (entry?.parent) { folded.delete(entry.parent.id); entry = byId.get(entry.parent.id); } }
    function focusTopic(id) { if (!byId.has(id) || !ready()) return; finishEdit(); reveal(id); selected = id; render(); const p = positions.get(id); x = stage.clientWidth / 2 - (p.x + p.width / 2) * zoom; y = stage.clientHeight / 2 - (p.y + p.height / 2) * zoom; paint(); viewChanged(); stage.focus({preventScroll:true}); }
    function inView(p) { const pad = 180 / zoom, left = -x / zoom - pad, top = -y / zoom - pad; return p.x + p.width >= left && p.y + p.height >= top && p.x <= left + stage.clientWidth / zoom + pad * 2 && p.y <= top + stage.clientHeight / zoom + pad * 2; }
    function makeCard(p) {
      const e = p.entry, card = el('div', undefined, 'xmind-topic'); card.dataset.topicId = e.topic.id; card.tabIndex = -1; card.setAttribute('role', 'treeitem'); card.style.setProperty('--branch-color', `var(--xmind-branch-${e.branch})`);
      if (!e.depth) card.classList.add('central'); if (e.group === 'detached') card.classList.add('detached'); if (e.group === 'summary') card.classList.add('summary');
      const style = topicStyle(e.topic); card.style.fontSize = `${textSize(e)}px`; card.style.lineHeight = `${Math.ceil(textSize(e) * 1.5)}px`;
      for (const [key, css] of [['svg:fill', 'backgroundColor'], ['fo:color', 'color']]) if (typeof style[key] === 'string' && /^#[a-f\d]{6}$/i.test(style[key])) card.style[css] = style[key];
      if (['normal', 'bold', '400', '500', '600', '700'].includes(style['fo:font-weight'])) card.style.fontWeight = style['fo:font-weight']; if (['normal', 'italic'].includes(style['fo:font-style'])) card.style.fontStyle = style['fo:font-style'];
      card.appendChild(el('span', e.topic.title || '（空主题）', 'xmind-topic-text'));
      if (children(e.topic).length) { const b = button(card, folded.has(e.topic.id) ? `＋${descendants(e.topic.id).size - 1}` : '−', '折叠或展开此分支', event => { event.stopPropagation(); fold(e.topic.id); }, 'xmind-fold'); b.setAttribute('aria-expanded', String(!folded.has(e.topic.id))); }
      if (e.topic.notes) card.appendChild(el('span', '≡', 'xmind-note-mark'));
      if (editable && e.parent && e.group !== 'summary') { const grip = el('span', '⠿', 'xmind-grip'); grip.draggable = true; grip.title = '拖动整个分支到另一个主题'; card.appendChild(grip); listen(grip, 'dragstart', event => { if (!writeReady()) { event.preventDefault(); return; } finishEdit(); dragId = e.topic.id; select(dragId); event.stopPropagation(); event.dataTransfer.setData('application/x-yingxu-topic', dragId); event.dataTransfer.effectAllowed = 'move'; }); listen(grip, 'dragend', () => { dragId = null; for (const c of cards.values()) c.classList.remove('drop-target'); }); }
      listen(card, 'click', event => { if (!event.target.closest('button,textarea')) select(e.topic.id); });
      listen(card, 'dblclick', event => { if (event.target.closest('button,textarea')) return; event.stopPropagation(); if (editable) { select(e.topic.id); startEdit(); } });
      listen(card, 'contextmenu', event => { event.preventDefault(); event.stopPropagation(); if (!ready()) return; select(e.topic.id); showMenu(event.clientX, event.clientY); });
      listen(card, 'dragover', event => { if (!dragId) return; event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect = 'move'; card.classList.toggle('drop-target', canMove(dragId, e.topic.id)); }); listen(card, 'dragleave', () => card.classList.remove('drop-target'));
      listen(card, 'drop', event => { if (!dragId) return; event.preventDefault(); event.stopPropagation(); const id = dragId; dragId = null; move(id, e.topic.id); }); return card;
    }
    function paint() {
      if (destroyed || !positions.size) return; world.style.transform = `translate(${x}px,${y}px) scale(${zoom})`; zoomButton.textContent = `${Math.round(zoom * 100)}%`;
      const keep = new Set(); for (const [id, p] of positions) if (inView(p) || id === selected || id === editing?.id) { keep.add(id); let card = cards.get(id); if (!card) { card = makeCard(p); cards.set(id, card); nodeLayer.appendChild(card); } card.style.left = `${p.x}px`; card.style.top = `${p.y}px`; card.style.width = `${p.width}px`; card.style.minHeight = `${p.height}px`; card.classList.toggle('selected', id === selected); card.classList.toggle('search-match', matches.includes(id)); card.setAttribute('aria-selected', String(id === selected)); }
      for (const [id, card] of cards) if (!keep.has(id)) { card.remove(); cards.delete(id); }
      svg.replaceChildren(); for (const p of positions.values()) { if (!p.entry.parent) continue; const a = positions.get(p.entry.parent.id); if (!inView(p) && !inView(a) && !inView({x:Math.min(p.x, a.x), y:Math.min(p.y, a.y), width:Math.abs(p.x - a.x) + p.width, height:Math.abs(p.y - a.y) + p.height})) continue; const ax = p.side > 0 ? a.x + a.width : a.x, bx = p.side > 0 ? p.x : p.x + p.width, ay = a.y + a.height / 2, by = p.y + p.height / 2, mid = (ax + bx) / 2; const path = document.createElementNS('http://www.w3.org/2000/svg', 'path'); path.setAttribute('d', `M ${ax} ${ay} C ${mid} ${ay}, ${mid} ${by}, ${bx} ${by}`); path.style.stroke = `var(--xmind-branch-${p.entry.branch})`; if (p.entry.group !== 'attached') path.setAttribute('stroke-dasharray', '5 5'); svg.appendChild(path); }
    }
    function renderOutline() { if (outline.hidden) return; outlineList.replaceChildren(); const query = search.value.trim(); for (const e of entries) { if (query ? !matches.includes(e.topic.id) : e.hidden) continue; const row = el('div', undefined, 'xmind-outline-row'); row.dataset.topicId = e.topic.id; row.style.paddingLeft = `${10 + Math.min(e.depth, 12) * 14}px`; const b = button(row, children(e.topic).length ? (folded.has(e.topic.id) ? '▸' : '▾') : '·', '展开或折叠分支', () => fold(e.topic.id)); b.disabled = !children(e.topic).length; const text = el('span', e.topic.title || '（空主题）'); text.tabIndex = 0; row.appendChild(text); listen(text, 'click', () => focusTopic(e.topic.id)); listen(text, 'dblclick', () => { focusTopic(e.topic.id); startEdit(); }); listen(text, 'keydown', event => { if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); focusTopic(e.topic.id); } }); row.classList.toggle('selected', selected === e.topic.id); outlineList.appendChild(row); } }
    function updateSearch() { const query = search.value.trim().toLocaleLowerCase(); matches = query ? entries.filter(e => String(e.topic.title || '').toLocaleLowerCase().includes(query)).map(e => e.topic.id) : []; matchLabel.textContent = query ? `${matches.includes(selected) ? matches.indexOf(selected) + 1 : 0}/${matches.length}` : ''; }
    function render() { if (destroyed) return; index(); arrange(); updatePicker(); updateSearch(); for (const card of cards.values()) card.remove(); cards = new Map(); if (!initialized) { const saved = cameras[sheets[current].id || String(current)]; if (saved && [saved.zoom, saved.x, saved.y].every(Number.isFinite)) { zoom = clamp(saved.zoom, .2, 4); x = saved.x; y = saved.y; initialized = true; } else fit(); } paint(); renderOutline(); controls(); viewChanged(); }
    function panel(which) { if (!ready()) return; finishEdit(); const p = which === 'outline' ? outline : properties; p.hidden = !p.hidden; controls(); renderOutline(); paint(); viewChanged(); }
    function fold(id) { if (!ready() || !byId.has(id) || !children(byId.get(id).topic).length) return; finishEdit(); if (folded.has(id)) folded.delete(id); else { folded.add(id); if (descendants(id).has(selected)) selected = id; } render(); }
    function stepMatch(step) { if (!matches.length || !ready()) return; const at = matches.indexOf(selected); focusTopic(matches[(at + step + matches.length) % matches.length]); updateSearch(); }
    function refreshText(id) { const text = cards.get(id)?.querySelector('.xmind-topic-text'); if (text) text.textContent = byId.get(id).topic.title || '（空主题）'; updateSearch(); paint(); renderOutline(); controls(); }
    function compositionStart() { composing = true; onComposition?.(true); } function compositionEnd() { composing = false; onComposition?.(false); }
    function startEdit(initial) {
      if (!writeReady()) return; finishEdit(); reveal(selected); render(); const entry = selectedEntry(); if (!cards.has(selected)) focusTopic(selected); const card = cards.get(selected); if (!card) return;
      const input = el('textarea', undefined, 'xmind-inline-editor'); input.setAttribute('aria-label', '直接编辑主题'); input.value = entry.topic.title || ''; input.rows = 2; card.querySelector('.xmind-topic-text').hidden = true; card.appendChild(input); card.classList.add('editing');
      editing = {id:selected, input, original:entry.topic.title || '', recorded:false}; listen(input, 'compositionstart', compositionStart); listen(input, 'compositionend', compositionEnd);
      listen(input, 'input', () => { if (!editable || !editing) return; if (!editing.recorded) { remember(); editing.recorded = true; } entry.topic.title = input.value; changed(); });
      listen(input, 'keydown', event => { if (event.isComposing || composing || event.keyCode === 229) return; if (event.key === 'Enter' && !event.shiftKey && !event.ctrlKey && !event.metaKey) { event.preventDefault(); event.stopPropagation(); finishEdit(); stage.focus(); } if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); finishEdit(true); stage.focus(); } });
      listen(input, 'blur', () => { if (!composing) finishEdit(); }); input.focus({preventScroll:true}); input.select(); if (initial !== undefined) { input.value = initial; input.dispatchEvent(new Event('input', {bubbles:true})); input.setSelectionRange(input.value.length, input.value.length); }
    }
    function finishEdit(cancel = false) { if (!editing || composing) return; const old = editing; editing = null; if (cancel && old.recorded) { byId.get(old.id).topic.title = old.original; undo.pop(); changed(); } old.input.remove(); const card = cards.get(old.id); if (card) { card.classList.remove('editing'); const text = card.querySelector('.xmind-topic-text'); text.hidden = false; text.textContent = byId.get(old.id)?.topic.title || '（空主题）'; } arrange(); paint(); renderOutline(); controls(); viewChanged(); }
    function bindField(input, change) { listen(input, 'focus', () => { fieldSession = {input, recorded:false}; }); listen(input, 'compositionstart', compositionStart); listen(input, 'compositionend', compositionEnd); listen(input, 'input', () => { if (!editable || input.readOnly) return; if (fieldSession?.input !== input) fieldSession = {input, recorded:false}; if (!fieldSession.recorded) { remember(); fieldSession.recorded = true; } change(input.value); changed(); undoButton.disabled = false; redoButton.disabled = true; }); listen(input, 'blur', () => { if (!composing) { fieldSession = null; arrange(); paint(); renderOutline(); updatePicker(); } }); }
    bindField(title, text => { selectedEntry().topic.title = text; refreshText(selected); }); bindField(notes, text => { const t = selectedEntry().topic; t.notes ||= {}; t.notes.plain ||= {}; t.notes.plain.content = text; }); bindField(sheetTitle, text => { sheets[current].title = text; updatePicker(); });
    function add(sibling) { let created; if (mutate(() => { const e = selectedEntry(); if (sibling && !e.parent) return false; if (totalTopics() >= 5000 || !sibling && e.depth >= 64) return blocked('已达到主题数量或层级上限。'); if (sibling && e.group === 'summary') return blocked('概要主题的关联结构请使用 XMind 修改。'); const owner = sibling ? e.parent : e.topic, group = sibling ? e.group : 'attached'; if (!structureReady(owner)) return false; owner.children ||= {}; owner.children[group] ||= []; created = {id:uid(), class:'topic', title:'新主题'}; const at = sibling ? owner.children[group].indexOf(e.topic) + 1 : owner.children[group].length; owner.children[group].splice(at, 0, created); folded.delete(owner.id); selected = created.id; return true; })) { focusTopic(created.id); startEdit(); } }
    function requestDelete() { if (!writeReady() || !selectedEntry().parent) return; if (!structureReady(selectedEntry().parent)) return; if (referenced(selected)) return blocked('此分支被关系线引用，请在 XMind 中解除关联后删除。'); finishEdit(); if (!deleting) { deleting = true; controls(); deleteButton.focus(); return; } mutate(() => { const e = selectedEntry(); e.parent.children[e.group].splice(e.parent.children[e.group].indexOf(e.topic), 1); selected = e.parent.id; deleting = false; return true; }); stage.focus(); }
    function history(forward) { if (!writeReady()) return; finishEdit(); const from = forward ? redo : undo, to = forward ? undo : redo; if (!from.length) return; to.push(snapshot()); trim(to); const old = from.pop(); saveCamera(); sheets = JSON.parse(old.content); current = clamp(old.sheet, 0, sheets.length - 1); selected = old.selected; fieldSession = null; initialized = false; changed(); render(); stage.focus(); }
    function canMove(id, targetId) { const e = byId.get(id), target = byId.get(targetId); if (!e?.parent || !target || e.group === 'summary' || target.group === 'summary' || e.parent.id === targetId || ranged(e.parent) || ranged(target.topic)) return false; const ids = descendants(id); if (ids.has(targetId)) return false; const height = Math.max(...entries.filter(e => ids.has(e.topic.id)).map(c => c.depth - e.depth)); return target.depth + 1 + height <= 64; }
    function move(id, targetId) { if (!canMove(id, targetId)) return blocked('请选择另一个主题；不能移动中心主题、概要或移入自己的分支。'); if (mutate(() => { const e = byId.get(id), target = byId.get(targetId).topic; e.parent.children[e.group].splice(e.parent.children[e.group].indexOf(e.topic), 1); target.children ||= {}; target.children.attached ||= []; target.children.attached.push(e.topic); folded.delete(targetId); selected = id; return true; })) focusTopic(id); }
    function reorder(step) { mutate(() => { const e = selectedEntry(), list = e.parent?.children?.[e.group]; if (!list || !structureReady(e.parent)) return false; const at = list.indexOf(e.topic), next = at + step; if (next < 0 || next >= list.length) return false; [list[at], list[next]] = [list[next], list[at]]; return true; }); }
    function setStyle(values) { mutate(() => { const topic = selectedEntry().topic; if (topic.style && (typeof topic.style !== 'object' || Array.isArray(topic.style)) || topic.style?.properties && (typeof topic.style.properties !== 'object' || Array.isArray(topic.style.properties))) return blocked('此文件使用了其他样式结构，已保留原样。'); topic.style ||= {id:uid(), type:'topic', properties:{}}; topic.style.properties ||= {}; for (const [key, val] of Object.entries(values)) { if (val) topic.style.properties[key] = val; else delete topic.style.properties[key]; } return true; }); }
    function outlineText(id = selected) { const base = byId.get(id); if (!base) return ''; const ids = descendants(id); return entries.filter(e => ids.has(e.topic.id)).map(e => `${'\t'.repeat(e.depth - base.depth)}${String(e.topic.title || '').replace(/\n/g, ' ')}`).join('\n'); }
    function copy(branch) { const text = branch ? outlineText() : selectedEntry().topic.title || ''; if (onCopy) { onCopy(text); return; } const fallback = () => { properties.hidden = false; controls(); title.focus(); title.select(); blocked('文字已选中，请按 Ctrl+C / ⌘C 复制。'); }; if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).catch(fallback); else fallback(); }
    function showMenu(px, py) { menu.hidden = false; const r = root.getBoundingClientRect(); menu.style.left = `${clamp(px - r.left, 0, Math.max(0, r.width - 180))}px`; menu.style.top = `${clamp(py - r.top, 0, Math.max(0, r.height - menu.offsetHeight))}px`; menu.querySelector('button:not(:disabled)')?.focus(); }
    function endPan() { if (!pan) return; const old = pan; pan = null; stage.classList.remove('panning'); if (stage.hasPointerCapture(old.id)) stage.releasePointerCapture(old.id); viewChanged(); }
    listen(stage, 'wheel', event => { if (!event.deltaX && !event.deltaY) return; event.preventDefault(); event.stopPropagation(); menu.hidden = true; const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? stage.clientHeight : 1; if (event.ctrlKey || event.metaKey) { const r = stage.getBoundingClientRect(); zoomAt(zoom * Math.exp(-clamp(event.deltaY * scale, -240, 240) * .002), event.clientX - r.left, event.clientY - r.top); } else { x -= (event.shiftKey ? event.deltaY : event.deltaX) * scale; y -= (event.shiftKey ? 0 : event.deltaY) * scale; paint(); viewChanged(); } }, {passive:false});
    listen(stage, 'pointerdown', event => { menu.hidden = true; if (event.button !== 1 && !(event.button === 0 && (space || !event.target.closest('.xmind-topic')))) return; if (event.target.closest('textarea,button') && event.button !== 1 && !space || !ready()) return; event.preventDefault(); event.stopPropagation(); finishEdit(); stage.focus({preventScroll:true}); pan = {id:event.pointerId, x:event.clientX, y:event.clientY}; stage.classList.add('panning'); try { stage.setPointerCapture(event.pointerId); } catch (_) { /* document fallback */ } });
    listen(document, 'pointermove', event => { if (!pan || event.pointerId !== pan.id) return; if (!event.buttons) { endPan(); return; } event.preventDefault(); x += event.clientX - pan.x; y += event.clientY - pan.y; pan.x = event.clientX; pan.y = event.clientY; paint(); }, {capture:true, passive:false});
    for (const type of ['pointerup', 'pointercancel']) listen(document, type, endPan, {capture:true}); listen(stage, 'lostpointercapture', endPan); listen(window, 'blur', () => { endPan(); space = false; stage.classList.remove('pan-ready'); });
    for (const type of ['mousedown', 'auxclick']) listen(stage, type, event => { if (event.button === 1) { event.preventDefault(); event.stopPropagation(); } });
    listen(root, 'pointerdown', event => { if (!menu.contains(event.target)) menu.hidden = true; }, {capture:true});
    listen(root, 'copy', event => { if (event.target.closest('input,textarea') || window.getSelection()?.toString() || !event.clipboardData) return; event.preventDefault(); event.stopPropagation(); event.clipboardData.setData('text/plain', selectedEntry().topic.title || ''); });
    listen(root, 'paste', event => {
      if (event.target.closest('input,textarea') || !stage.contains(event.target)) return; const text = event.clipboardData?.getData('text/plain'); if (!text || event.clipboardData.files.length) return; event.preventDefault(); event.stopPropagation(); if (!writeReady()) return;
      const lines = text.replace(/\r\n?/g, '\n').split('\n').filter(line => line.trim()); if (!lines.length) return; const entry = selectedEntry(); if (!structureReady(entry.topic)) return; if (lines.length > 5000 - totalTopics() || new TextEncoder().encode(text).length > 256 * 1024) return blocked('粘贴内容超过主题数量或文本限制。');
      const parsed = lines.map(line => ({depth:line.match(/^\t*/)[0].length, title:line.replace(/^\t*/, '')})), base = Math.min(...parsed.map(line => line.depth)); let level = -1;
      for (const row of parsed) { row.depth = Math.min(row.depth - base, level + 1); level = row.depth; if (entry.depth + row.depth + 1 > 64) return blocked('粘贴后超过 64 层深度。'); }
      mutate(() => { const owners = [entry.topic]; for (const row of parsed) { const owner = owners[row.depth] || entry.topic, topic = {id:uid(), class:'topic', title:row.title}; owner.children ||= {}; owner.children.attached ||= []; owner.children.attached.push(topic); owners[row.depth + 1] = topic; owners.length = row.depth + 2; } folded.delete(selected); return true; });
    });
    listen(root, 'keydown', event => {
      const mod = event.ctrlKey || event.metaKey, key = event.key.toLowerCase(), input = !!event.target.closest('input,textarea');
      if (event.isComposing || composing || event.keyCode === 229) { if (!input && ['tab','enter','delete'].includes(key)) { event.preventDefault(); event.stopPropagation(); } return; }
      if (mod && key === 'f') { event.preventDefault(); event.stopPropagation(); search.focus(); search.select(); return; }
      if (key === 'escape') { menu.hidden = true; deleting = false; controls(); if (search.value) { search.value = ''; updateSearch(); paint(); renderOutline(); } event.stopPropagation(); return; }
      if (input) return;
      if (event.target.closest('button,select,summary') && !mod && key !== 'delete') return;
      if (mod && key === 'c' && event.shiftKey) { event.preventDefault(); event.stopPropagation(); copy(true); return; }
      const action = mod && key === 'z' ? () => history(event.shiftKey) : mod && key === 'y' ? () => history(true) : mod && key === '0' ? fit : mod && key === '1' ? () => zoomAt(1) : mod && ['+','='].includes(key) ? () => zoomAt(zoom * 1.2) : mod && key === '-' ? () => zoomAt(zoom / 1.2) : mod && key === ' ' ? () => fold(selected) : key === 'f2' ? () => startEdit() : key === 'tab' && !event.shiftKey ? () => add(false) : key === 'enter' ? () => add(true) : ['delete','backspace'].includes(key) ? requestDelete : null;
      if (action) { event.preventDefault(); event.stopPropagation(); action(); return; }
      if (key === ' ') { event.preventDefault(); space = true; stage.classList.add('pan-ready'); return; }
      if (key.startsWith('arrow')) { event.preventDefault(); event.stopPropagation(); const e = selectedEntry(), at = visible.findIndex(c => c.topic.id === selected); const next = key === 'arrowup' ? visible[Math.max(0, at - 1)] : key === 'arrowdown' ? visible[Math.min(visible.length - 1, at + 1)] : key === 'arrowleft' ? byId.get(e.parent?.id) : byId.get(children(e.topic)[0]?.topic.id); if (next) focusTopic(next.topic.id); return; }
      if (!mod && !event.altKey && event.key.length === 1 && stage.contains(event.target)) { event.preventDefault(); event.stopPropagation(); startEdit(event.key); }
    });
    listen(root, 'keyup', event => { if (event.key === ' ') { space = false; stage.classList.remove('pan-ready'); } });
    listen(picker, 'change', () => { if (!ready()) { picker.value = String(current); return; } finishEdit(); saveCamera(); current = Number(picker.value); selected = sheets[current].rootTopic.id; fieldSession = null; initialized = false; render(); });
    listen(direction, 'change', () => { if (!ready()) { direction.value = layout; return; } finishEdit(); layout = direction.value; render(); fit(); }); listen(search, 'input', () => { updateSearch(); paint(); renderOutline(); }); listen(search, 'keydown', event => { if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); event.stopPropagation(); stepMatch(event.shiftKey ? -1 : 1); } });
    listen(fontSize, 'change', () => setStyle({'fo:font-size':fontSize.value})); listen(colors, 'change', () => { const [fill, color] = colors.value.split('|'); setStyle({'svg:fill':fill, 'fo:color':color}); });
    const observer = new ResizeObserver(() => { if (!initialized) fit(); else paint(); }); observer.observe(stage); render();
    return {isComposing:() => composing, getViewState, focusSearch:() => search.focus(), destroy:() => { if (destroyed) return; finishEdit(); viewChanged(); destroyed = true; observer.disconnect(); abort.abort(); root.remove(); }};
  }
  window.YingXuXmind = {mount};
})();
