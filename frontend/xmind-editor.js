/* Offline XMind workspace. Preserve file data; keep camera/history outside the archive. */
(function () {
  'use strict';
  const children = t => Object.entries(t.children || {}).flatMap(([group, list]) => list.map(topic => ({topic, group})));
  const uid = () => crypto.randomUUID().replaceAll('-', '');
  const clamp = (n, a, b) => Math.max(a, Math.min(b, n));
  const priorities = Array.from({length:9}, (_, i) => [`priority-${i + 1}`, `优先级 ${i + 1}`]);
  const progress = [['task-start','未开始'],['task-oct','进度 12.5%'],['task-quarter','进度 25%'],['task-3oct','进度 37.5%'],['task-half','进度 50%'],['task-5oct','进度 62.5%'],['task-3quar','进度 75%'],['task-7oct','进度 87.5%'],['task-done','已完成'],['task-pause','已暂停']];
  function mount({parent, value, editable, selectedId, sheetIndex = 0, notice, viewState = {}, onChange, onSelection, onViewChange, onComposition, onBlocked, onCopy, onPickLink, onOpenLink}) {
    let sheets = JSON.parse(value), current = clamp(sheetIndex, 0, sheets.length - 1), selected = selectedId;
    let documentContent = value;
    let composing = false, destroyed = false, editing = null, fieldSession = null, pan = null, marquee = null, space = false, dragId = null;
    let selection = new Set(viewState.documentContent === value && Array.isArray(viewState.selection) ? viewState.selection : [selectedId || sheets[current].rootTopic.id]);
    let entries = [], byId = new Map(), visible = [], positions = new Map(), cards = new Map(), matches = [], bounds;
    let zoom = 1, x = 0, y = 0, initialized = false, layout = viewState.layout === 'right' ? 'right' : 'balanced';
    const folded = new Set(viewState.folded || []), cameras = {...viewState.cameras}, undo = viewState.documentContent === value ? [...(viewState.undo || [])] : [], redo = viewState.documentContent === value ? [...(viewState.redo || [])] : [];
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
      sheets.push(sheet); current = sheets.length - 1; selected = sheet.rootTopic.id; selection = new Set([selected]); initialized = false; return true;
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
    const foldButton = button(tools, '折叠分支', '折叠或展开当前分支 · Ctrl / ⌘ Space', foldSelection);
    button(tools, '选中子分支', '选中当前分支及全部子主题', selectBranches);
    button(tools, '全部展开', '展开所有主题', () => { if (!ready()) return; finishEdit(); folded.clear(); render(); });
    const direction = el('select'); direction.setAttribute('aria-label', '导图布局');
    for (const [key, text] of [['balanced', '左右展开'], ['right', '向右展开']]) { const o = el('option', text); o.value = key; direction.appendChild(o); } direction.value = layout; tools.appendChild(direction);
    const help = el('details', undefined, 'xmind-help'); help.append(el('summary', '操作提示'), el('p', '左键单选，空白处拉框多选；Ctrl / ⌘ 点击切换选中，Shift 点击追加，Ctrl / ⌘ A 全选展开主题。中键拖动画布，Ctrl / ⌘ 滚轮缩放。Delete 一次删除选中分支，Ctrl / ⌘ Z 撤销整批操作。双击或 F2 编辑，文字可拖选复制；Tab 新建子主题，Enter 新建同级。节点下方可查看标注、打开链接；右下角手柄移动选中分支。')); tools.appendChild(help);
    const body = el('div', undefined, 'xmind-body'), outline = el('aside', undefined, 'xmind-outline'), properties = el('aside', undefined, 'xmind-properties');
    outline.setAttribute('aria-label', '主题大纲'); properties.setAttribute('aria-label', '主题属性');
    for (const [host, name, key] of [[outline, '大纲', 'outline'], [properties, '主题属性', 'properties']]) { const head = el('div', undefined, 'xmind-panel-heading'); head.appendChild(el('strong', name)); button(head, '×', `收起${name}`, () => panel(key)); host.appendChild(head); }
    const outlineList = el('div', undefined, 'xmind-outline-list'); outline.appendChild(outlineList);
    const stage = el('div', undefined, 'xmind-stage'); stage.tabIndex = 0; stage.setAttribute('aria-label', '思维导图画布，左键框选，中键平移，双击编辑，Ctrl 加滚轮缩放'); stage.setAttribute('role', 'tree'); stage.setAttribute('aria-multiselectable', 'true');
    const world = el('div', undefined, 'xmind-world'), nodeLayer = el('div', undefined, 'xmind-nodes');
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.classList.add('xmind-connections'); svg.setAttribute('aria-hidden', 'true'); world.append(svg, nodeLayer); stage.appendChild(world);
    const marqueeBox = el('div', undefined, 'xmind-marquee'); marqueeBox.hidden = true; marqueeBox.setAttribute('aria-hidden', 'true'); stage.appendChild(marqueeBox);
    const selectionInfo = el('p', '', 'xmind-selection-info'); properties.appendChild(selectionInfo);
    function field(label, aria, rows) { const host = el('label', undefined, 'xmind-field'); host.appendChild(el('span', label)); const input = el('textarea'); input.rows = rows; input.setAttribute('aria-label', aria); host.appendChild(input); properties.appendChild(host); return input; }
    const title = field('主题文字', '主题文字', 2), notes = field('备注', '主题备注', 3), sheetTitle = field('画布名称', '画布名称', 1);
    const notesHint = el('p', '', 'xmind-field-hint'), metadata = el('div', undefined, 'xmind-metadata'); notes.parentElement.appendChild(notesHint); properties.appendChild(metadata);
    const applyNotes = button(properties, '应用备注到所选主题', '应用备注到所选主题', () => applyAnnotation('notes', notes.value));
    const labels = field('标签（逗号或换行分隔）', '主题标签', 2), labelActions = el('div', undefined, 'xmind-annotation-actions'); properties.appendChild(labelActions);
    const labelButtons = ['追加','替换','移除'].map((name, i) => button(labelActions, name, `${name}所选主题标签`, () => applyLabels(['append','replace','remove'][i])));
    function markerField(label, list) {
      const host = el('label', undefined, 'xmind-field'), input = el('select'); host.append(el('span', label), input); input.setAttribute('aria-label', label);
      for (const [value, text] of [['','无'], ['mixed','多种值'], ...list]) { const o = el('option', text); o.value = value; o.disabled = value === 'mixed'; input.appendChild(o); }
      properties.appendChild(host); listen(input, 'change', () => setMarker(list, input.value)); return input;
    }
    const priority = markerField('优先级', priorities), taskProgress = markerField('任务进度', progress);
    const link = field('链接 · 网址 / 文件 / 主题', '主题链接', 2); link.placeholder = 'https://…';
    const linkActions = el('div', undefined, 'xmind-annotation-actions'); properties.appendChild(linkActions);
    const saveLink = button(linkActions, '应用链接', '应用链接到所选主题', () => applyAnnotation('hyperlink', link.value.trim()));
    const openLinkButton = button(linkActions, '打开', '打开主题链接', () => openLink(topicLink(selectedEntry().topic)));
    const pickLinkButton = button(properties, '关联项目文件…', '关联映序项目文件', pickLink);
    const topicLinkField = el('label', undefined, 'xmind-field'), topicLinkTarget = el('select'); topicLinkTarget.setAttribute('aria-label', '链接到导图主题'); topicLinkField.append(el('span', '链接到导图主题'), topicLinkTarget); properties.appendChild(topicLinkField);
    const saveTopicLink = button(properties, '关联所选主题', '关联所选导图主题', () => applyAnnotation('hyperlink', `xmind:#${encodeURIComponent(topicLinkTarget.value)}`));
    const annotationHint = el('p', '点击节点的链接标记打开；清空链接后应用可移除。映序文件链接使用文件身份，改名后仍可打开。', 'xmind-field-hint'); properties.appendChild(annotationHint);
    const relations = el('div', undefined, 'xmind-relations'); properties.appendChild(relations);
    properties.appendChild(sheetTitle.parentElement);
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
    for (const [text, action, writes] of [['编辑文字', () => startEdit(), true], ['添加子主题', () => add(false), true], ['添加同级主题', () => add(true), true], ['复制主题文字', () => copy(false)], ['复制分支大纲', () => copy(true)], ['选中子分支', selectBranches], ['折叠 / 展开', foldSelection], ['主题标注与链接', () => { properties.hidden = false; controls(); }], ['打开链接', () => openLink(topicLink(selectedEntry().topic))], ['删除分支', requestDelete, true]]) { const b = button(menu, text, text, () => { menu.hidden = true; action(); }); b.setAttribute('role', 'menuitem'); if (writes) b.disabled = !editable; }
    root.append(header, tools, body, footer, info, menu); parent.replaceChildren(root); outline.hidden = !viewState.outline; properties.hidden = !viewState.properties;

    function blocked(message = '请先确认正在输入的文字。') { onBlocked?.(message); return false; }
    function ready() { return !destroyed && !composing || blocked(); }
    function writeReady() { return editable && ready(); }
    function ranged(topic) { return [topic.boundaries, topic.summaries].some(value => value != null && (!Array.isArray(value) || value.length > 0)); }
    function structureReady(topic) { return !ranged(topic) || blocked('此分支含范围边界或概要，结构修改请使用 XMind；文字和样式仍可编辑。'); }
    function referenced(id) { const ids = descendants(id), links = sheets[current].relationships; if (!links) return false; if (!Array.isArray(links)) return true; return links.some(link => !link || ids.has(link.end1Id) || ids.has(link.end2Id) || !link.end1Id || !link.end2Id); }
    function snapshot() { return {content:JSON.stringify(sheets), sheet:current, selected, selection:[...selection]}; }
    function trim(list) { let bytes = list.reduce((sum, row) => sum + row.content.length, 0); while (list.length > 50 || bytes > 4 * 1024 * 1024 && list.length > 1) bytes -= list.shift().content.length; }
    function saveCamera() { if (initialized && sheets[current]) cameras[sheets[current].id || String(current)] = {zoom, x, y}; }
    function getViewState() { saveCamera(); return {documentContent, layout, selection:[...selection], folded:[...folded], cameras:{...cameras}, outline:!outline.hidden, properties:!properties.hidden, undo:[...undo], redo:[...redo]}; }
    function viewChanged() { onViewChange?.(getViewState()); }
    function changed() { documentContent = JSON.stringify(sheets); onChange?.(documentContent); viewChanged(); }
    function remember(before = snapshot()) { undo.push(before); trim(undo); redo.length = 0; }
    function mutate(action) { if (!writeReady()) return false; finishEdit(); fieldSession = null; const before = snapshot(); if (!action()) return false; remember(before); changed(); render(); return true; }
    function totalTopics() { let n = 0; for (const sheet of sheets) { const stack = [sheet.rootTopic]; while (stack.length) { const t = stack.pop(); n++; stack.push(...children(t).map(c => c.topic)); } } return n; }
    function index() {
      entries = []; byId = new Map(); visible = [];
      const stack = [{topic:sheets[current].rootTopic, parent:null, group:null, depth:0, hidden:false, branch:0}];
      while (stack.length) { const entry = stack.pop(); entries.push(entry); byId.set(entry.topic.id, entry); if (!entry.hidden) visible.push(entry); const list = children(entry.topic); for (let i = list.length - 1; i >= 0; i--) stack.push({...list[i], parent:entry.topic, depth:entry.depth + 1, hidden:entry.hidden || folded.has(entry.topic.id), branch:entry.depth ? entry.branch : i % 6}); }
      if (!byId.has(selected)) selected = entries[0].topic.id;
      selection = new Set([...selection].filter(id => byId.has(id)));
    }
    const selectedEntry = () => byId.get(selected) || entries[0];
    function descendants(id) { const found = new Set(), stack = [byId.get(id)?.topic]; while (stack.length) { const t = stack.pop(); if (!t) continue; found.add(t.id); stack.push(...children(t).map(c => c.topic)); } return found; }
    function setSelection(ids, primary = ids[0], focus = false) {
      if (!ready()) return; finishEdit(); fieldSession = null;
      selection = new Set(ids.filter(id => byId.has(id))); selected = selection.has(primary) ? primary : [...selection][0] || selected;
      controls(); paint(); viewChanged(); if (focus) stage.focus({preventScroll:true});
    }
    function select(id, focus = false, event = {}) {
      if (!byId.has(id)) return;
      const ids = event.ctrlKey || event.metaKey || event.shiftKey ? new Set(selection) : new Set();
      if ((event.ctrlKey || event.metaKey) && ids.has(id)) ids.delete(id); else ids.add(id);
      setSelection([...ids], id, focus);
    }
    function selectedEntries() { return entries.filter(e => selection.has(e.topic.id)); }
    function plainNotesEditable(topic) {
      const object = value => value != null && typeof value === 'object' && !Array.isArray(value);
      return topic.notes == null || object(topic.notes) && Object.keys(topic.notes).every(key => key === 'plain') &&
        (topic.notes.plain == null || object(topic.notes.plain) && (topic.notes.plain.content == null || typeof topic.notes.plain.content === 'string'));
    }
    function topicLink(topic) { return typeof topic.href === 'string' ? topic.href : typeof topic.hyperlink === 'string' ? topic.hyperlink : ''; }
    function markerValue(topic, list) { const ids = new Set(list.map(row => row[0])); return (Array.isArray(topic.markers) ? topic.markers : []).find(marker => ids.has(marker?.markerId))?.markerId || ''; }
    function commonValue(targets, getter) { const values = targets.map(e => getter(e.topic)); return values.every(v => v === values[0]) ? values[0] || '' : 'mixed'; }
    function annotationControls() {
      const targets = selectedEntries(), count = targets.length, single = count === 1, topic = selectedEntry().topic;
      if (document.activeElement !== labels) labels.value = single && Array.isArray(topic.labels) ? topic.labels.join(', ') : '';
      labels.placeholder = count > 1 ? '追加保留原标签；替换将统一所有所选主题' : '例如：待确认, 角色, 重要';
      const labelsReady = editable && count && targets.every(e => e.topic.labels == null || Array.isArray(e.topic.labels) && e.topic.labels.every(v => typeof v === 'string'));
      labels.readOnly = !labelsReady; for (const b of labelButtons) b.disabled = !labelsReady;
      priority.value = commonValue(targets, t => markerValue(t, priorities)); taskProgress.value = commonValue(targets, t => markerValue(t, progress));
      priority.disabled = taskProgress.disabled = !editable || !count || targets.some(e => e.topic.markers != null && !Array.isArray(e.topic.markers));
      applyNotes.hidden = count < 2; applyNotes.disabled = notes.readOnly;
      if (document.activeElement !== link) link.value = single ? topicLink(topic) : '';
      link.readOnly = saveLink.disabled = !editable || !count; openLinkButton.disabled = !single || !topicLink(topic);
      pickLinkButton.disabled = !editable || !count || !onPickLink;
      if (!properties.hidden) {
        const previous = topicLinkTarget.value; topicLinkTarget.replaceChildren();
        for (const sheet of sheets) {
          const pending = [sheet.rootTopic]; while (pending.length) { const target = pending.pop(); pending.push(...children(target).map(c => c.topic).reverse()); if (selection.has(target.id)) continue; const o = el('option', `${sheet.title || '画布'} · ${target.title || '空主题'}`); o.value = target.id; topicLinkTarget.appendChild(o); }
        }
        if ([...topicLinkTarget.options].some(o => o.value === previous)) topicLinkTarget.value = previous;
      }
      saveTopicLink.disabled = !editable || !count || !topicLinkTarget.options.length;
      relations.replaceChildren();
      if (single && Array.isArray(sheets[current].relationships)) {
        const related = sheets[current].relationships.filter(r => r && (r.end1Id === selected || r.end2Id === selected));
        if (related.length) relations.appendChild(el('strong', '关联主题'));
        for (const relation of related) { const id = relation.end1Id === selected ? relation.end2Id : relation.end1Id, target = byId.get(id); if (target) button(relations, `${relation.title || '关联'} → ${target.topic.title || '空主题'}`, '跳转关联主题', () => focusTopic(id)); }
      }
    }
    function applyLabels(mode) {
      const values = [...new Set(labels.value.split(/[,，\n]/).map(v => v.trim()).filter(Boolean))];
      if (values.length > 32 || values.some(v => v.length > 80)) return blocked('最多 32 个标签，每个不超过 80 个字符。');
      if (!selection.size) return;
      mutate(() => {
        const targets = selectedEntries();
        if (targets.some(e => e.topic.labels != null && (!Array.isArray(e.topic.labels) || e.topic.labels.some(v => typeof v !== 'string')))) return blocked('所选主题含其他标签结构，已保留原样。');
        const updated = targets.map(({topic}) => mode === 'replace' ? values : mode === 'remove' ? (topic.labels || []).filter(v => !values.includes(v)) : [...new Set([...(topic.labels || []), ...values])]);
        if (updated.some(list => list.length > 32)) return blocked('追加后某个主题超过 32 个标签，未修改任何主题。');
        targets.forEach(({topic}, i) => { topic.labels = [...updated[i]]; }); return true;
      });
    }
    function setMarker(list, value) {
      if (!selection.size || value === 'mixed') return;
      const known = new Set(list.map(row => row[0])); if (value && !known.has(value)) return;
      mutate(() => {
        const targets = selectedEntries(); if (targets.some(e => e.topic.markers != null && !Array.isArray(e.topic.markers))) return blocked('所选主题含其他标记结构，已保留原样。');
        for (const {topic} of targets) {
          const original = topic.markers || [], previous = original.find(marker => known.has(marker?.markerId));
          topic.markers = original.filter(marker => !known.has(marker?.markerId));
          if (value) topic.markers.push({...previous, markerId:value});
        }
        return true;
      });
    }
    function validLink(value) {
      if (!value || /^yingxu:\/\/item\/[a-f0-9]{32}$/.test(value) || /^xmind:#[^\s]+$/.test(value)) return true;
      if (!/^https?:\/\//i.test(value) || value.length > 4096 || /[\s\\\u0000-\u001f\u007f-\u009f]/u.test(value)) return false;
      try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && !!url.hostname && !url.username && !url.password && url.href.length <= 4096; } catch (_) { return false; }
    }
    function applyAnnotation(key, value) {
      if (!selection.size) return;
      if (key === 'hyperlink' && (!validLink(value) || value.length > 4096)) return blocked('请输入完整的 http / https 网址，或使用按钮关联文件、主题。');
      mutate(() => {
        const targets = selectedEntries();
        if (key === 'notes' && targets.some(e => !plainNotesEditable(e.topic))) return blocked('所选主题含富文本或其他备注结构，不能批量覆盖。');
        for (const {topic} of targets) {
          if (key === 'notes') { topic.notes ||= {}; topic.notes.plain ||= {}; topic.notes.plain.content = value; }
          else if (value) { topic.href = value; if ('hyperlink' in topic) topic.hyperlink = value; }
          else { delete topic.hyperlink; delete topic.href; }
        }
        return true;
      });
    }
    async function pickLink() {
      if (!writeReady() || !selection.size || !onPickLink) return;
      finishEdit(); const ids = [...selection], at = current;
      try { const href = await onPickLink(); if (!href || destroyed) return; if (current !== at || ids.length !== selection.size || ids.some(id => !selection.has(id))) return blocked('选择已变化，请重新关联文件。'); applyAnnotation('hyperlink', href); } catch (error) { if (!destroyed) blocked(error.message || '无法选择文件。'); }
    }
    async function openLink(href) {
      if (!ready() || !href) return; finishEdit();
      if (href.startsWith('xmind:#')) {
        let id; try { id = decodeURIComponent(href.slice(7)); } catch (_) { return blocked('主题链接无效。'); }
        for (let i = 0; i < sheets.length; i++) {
          const pending = [sheets[i].rootTopic]; while (pending.length) { const topic = pending.pop(); if (topic.id === id) { saveCamera(); current = i; selected = id; selection = new Set([id]); initialized = false; render(); focusTopic(id); return; } pending.push(...children(topic).map(c => c.topic)); }
        }
        return blocked('链接指向的主题已不存在。');
      }
      if (!validLink(href)) return blocked('该链接格式暂不支持打开，原始内容已保留。');
      try { if (onOpenLink) await onOpenLink(href); else blocked('请在映序工作台中打开文件或网页链接。'); } catch (error) { if (!destroyed) blocked(error.message || '无法打开链接。'); }
    }
    function selectionRoots({omitRoot = false} = {}) {
      const ids = new Set([...selection].filter(id => !omitRoot || byId.get(id)?.parent));
      return entries.filter(e => { if (!ids.has(e.topic.id)) return false; let p = e.parent; while (p) { if (ids.has(p.id)) return false; p = byId.get(p.id)?.parent; } return true; });
    }
    function selectBranches() { const ids = new Set(); for (const id of selection) for (const child of descendants(id)) ids.add(child); setSelection([...ids], selected, true); }
    function foldSelection() { if (!ready()) return; finishEdit(); const targets = selectedEntries().filter(e => children(e.topic).length), expand = targets.every(e => folded.has(e.topic.id)); for (const e of targets) { if (expand) folded.delete(e.topic.id); else folded.add(e.topic.id); } render(); }
    function controls() {
      const entry = selectedEntry(); if (!entry) return;
      const setField = (field, text) => { if (document.activeElement !== field) field.value = text; };
      const count = selection.size, many = count > 1, chosen = selectedEntries();
      selectionInfo.textContent = count ? many ? `已选 ${count} 个主题 · 标注和样式可批量设置` : '已选 1 个主题' : '点击主题或在画布空白处拉框选择';
      setField(title, count === 1 ? entry.topic.title || '' : ''); setField(sheetTitle, sheets[current].title || ''); setField(notes, count === 1 ? entry.topic.notes?.plain?.content || '' : '');
      title.placeholder = many ? '多选时保留各主题文字' : '主题文字'; notes.placeholder = many ? '输入后点击“应用备注到所选主题”' : '记录说明、想法和补充信息';
      const rich = chosen.some(e => !plainNotesEditable(e.topic)); notes.readOnly = !editable || !count || rich; title.readOnly = !editable || count !== 1; sheetTitle.readOnly = !editable;
      const style = topicStyle(entry.topic), styleEditable = editable && count && chosen.every(e => validStyle(e.topic));
      boldButton.disabled = italicButton.disabled = fontSize.disabled = colors.disabled = !styleEditable;
      boldButton.setAttribute('aria-pressed', String(style['fo:font-weight'] === 'bold')); italicButton.setAttribute('aria-pressed', String(style['fo:font-style'] === 'italic'));
      fontSize.value = [...fontSize.options].some(o => o.value === style['fo:font-size']) ? style['fo:font-size'] : '';
      colors.value = [...colors.options].some(o => o.value === `${style['svg:fill']}|${style['fo:color']}`) ? `${style['svg:fill']}|${style['fo:color']}` : '';
      notesHint.textContent = rich ? '所选主题含富文本或其他备注结构，保留原格式；单选时可复制已有纯文本。' : '备注保存在 XMind 文件中。';
      childButton.disabled = editButton.disabled = !editable || count !== 1; sheetButton.disabled = !editable; siblingButton.disabled = !editable || count !== 1 || !entry.parent; deleteButton.disabled = !editable || !selectionRoots({omitRoot:true}).length;
      undoButton.disabled = !editable || !undo.length; redoButton.disabled = !editable || !redo.length; deleteButton.textContent = many ? `删除所选分支 (${count})` : '删除分支';
      foldButton.disabled = !chosen.some(e => children(e.topic).length); foldButton.textContent = chosen.every(e => folded.has(e.topic.id)) ? '展开分支' : '折叠分支';
      metadata.replaceChildren(); if (count === 1 && topicLink(entry.topic) && !validLink(topicLink(entry.topic))) metadata.appendChild(el('p', `保留的原链接：${topicLink(entry.topic)}`)); if (entry.group === 'summary') metadata.appendChild(el('p', '概要主题（保留原文件关联）'));
      breadcrumb.replaceChildren(); const path = []; let cursor = entry; while (cursor) { path.unshift(cursor); cursor = cursor.parent ? byId.get(cursor.parent.id) : null; }
      path.forEach((a, i) => { if (i) breadcrumb.appendChild(el('span', '›')); button(breadcrumb, a.topic.title || '空主题', '定位 ' + (a.topic.title || '空主题'), () => focusTopic(a.topic.id)); });
      stats.textContent = `${entries.length} 个主题 · ${visible.length} 个展开 · 已选 ${count}${editable ? '' : ' · 只读'}`;
      outlineButton.setAttribute('aria-pressed', String(!outline.hidden)); propertiesButton.setAttribute('aria-pressed', String(!properties.hidden)); onSelection?.(selected, current);
      for (const row of outlineList.children) row.classList.toggle('selected', selection.has(row.dataset.topicId));
      if (!properties.hidden) { const excluded = new Set(); for (const id of selection) for (const child of descendants(id)) excluded.add(child); moveTarget.replaceChildren(); for (const target of entries) if (!excluded.has(target.topic.id) && target.topic.id !== entry.parent?.id && target.group !== 'summary' && target.depth < 64) { const o = el('option', `${'　'.repeat(Math.min(4, target.depth))}${target.topic.title || '空主题'}`); o.value = target.topic.id; moveTarget.appendChild(o); } }
      moveButton.disabled = !editable || !count || chosen.some(e => !e.parent || e.group === 'summary') || !moveTarget.options.length;
      const siblings = entry.parent?.children?.[entry.group] || [], at = siblings.indexOf(entry.topic); upButton.disabled = !editable || count !== 1 || at <= 0; downButton.disabled = !editable || count !== 1 || at < 0 || at >= siblings.length - 1;
      annotationControls();
    }
    function updatePicker() { picker.replaceChildren(); sheets.forEach((sheet, i) => { const o = el('option', sheet.title || `画布 ${i + 1}`); o.value = String(i); picker.appendChild(o); }); picker.value = String(current); }
    function topicStyle(topic) { const entry = byId.get(topic.id), key = !entry?.depth ? 'centralTopic' : entry.group === 'detached' ? 'floatingTopic' : entry.group === 'summary' ? 'summaryTopic' : entry.depth === 1 ? 'mainTopic' : 'subTopic'; const inherited = sheets[current].theme?.[key]?.properties, props = topic.style?.properties; return {...(inherited && typeof inherited === 'object' && !Array.isArray(inherited) ? inherited : {}), ...(props && typeof props === 'object' && !Array.isArray(props) ? props : {})}; }
    function textSize(entry) { const raw = topicStyle(entry.topic)['fo:font-size'], match = typeof raw === 'string' && raw.match(/^(\d+(?:\.\d+)?)(pt|px)$/); return match ? clamp(Number(match[1]) * (match[2] === 'pt' ? 4 / 3 : 1), 12, 32) : entry.depth ? 14 : 16; }
    const measure = document.createElement('canvas').getContext('2d');
    function dimensions(entry) {
      const font = textSize(entry), style = topicStyle(entry.topic);
      measure.font = `${style['fo:font-weight'] === 'bold' ? 'bold ' : ''}${font}px "Segoe UI", "Microsoft YaHei", sans-serif`;
      const rows = Number(!!(entry.topic.notes || topicLink(entry.topic))) + Number(!!entry.topic.markers?.length) + Number(!!entry.topic.labels?.length);
      const lines = String(entry.topic.title || '（空主题）').split('\n', 6), widths = lines.map(s => measure.measureText(s.slice(0, 1000)).width);
      const width = clamp(Math.max(76, ...widths) + 40, rows ? 176 : entry.depth ? 124 : 164, 300), n = widths.reduce((n, w) => n + Math.max(1, Math.ceil(w / (width - 40))), 0);
      return {width, height:Math.max(entry.depth ? 48 : 60, Math.min(n, 6) * Math.ceil(font * 1.5) + 26) + rows * 24};
    }
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
    function focusTopic(id) { if (!byId.has(id) || !ready()) return; finishEdit(); reveal(id); selected = id; selection = new Set([id]); render(); const p = positions.get(id); x = stage.clientWidth / 2 - (p.x + p.width / 2) * zoom; y = stage.clientHeight / 2 - (p.y + p.height / 2) * zoom; paint(); viewChanged(); stage.focus({preventScroll:true}); }
    function inView(p) { const pad = 180 / zoom, left = -x / zoom - pad, top = -y / zoom - pad; return p.x + p.width >= left && p.y + p.height >= top && p.x <= left + stage.clientWidth / zoom + pad * 2 && p.y <= top + stage.clientHeight / zoom + pad * 2; }
    function makeCard(p) {
      const e = p.entry, card = el('div', undefined, 'xmind-topic'); card.dataset.topicId = e.topic.id; card.tabIndex = -1; card.setAttribute('role', 'treeitem'); card.style.setProperty('--branch-color', `var(--xmind-branch-${e.branch})`);
      if (!e.depth) card.classList.add('central'); if (e.group === 'detached') card.classList.add('detached'); if (e.group === 'summary') card.classList.add('summary');
      const style = topicStyle(e.topic); card.style.fontSize = `${textSize(e)}px`; card.style.lineHeight = `${Math.ceil(textSize(e) * 1.5)}px`;
      for (const [key, css] of [['svg:fill', 'backgroundColor'], ['fo:color', 'color']]) if (typeof style[key] === 'string' && /^#[a-f\d]{6}$/i.test(style[key])) card.style[css] = style[key];
      if (['normal', 'bold', '400', '500', '600', '700'].includes(style['fo:font-weight'])) card.style.fontWeight = style['fo:font-weight']; if (['normal', 'italic'].includes(style['fo:font-style'])) card.style.fontStyle = style['fo:font-style'];
      card.appendChild(el('span', e.topic.title || '（空主题）', 'xmind-topic-text'));
      if (children(e.topic).length) { const b = button(card, folded.has(e.topic.id) ? `＋${descendants(e.topic.id).size - 1}` : '−', '折叠或展开此分支', event => { event.stopPropagation(); fold(e.topic.id); }, 'xmind-fold'); b.setAttribute('aria-expanded', String(!folded.has(e.topic.id))); }
      const badgeRow = el('div', undefined, 'xmind-badges');
      for (const [list, cls] of [[priorities,'priority'],[progress,'progress']]) { const id = markerValue(e.topic, list), label = list.find(row => row[0] === id)?.[1]; if (label) { const badge = el('span', cls === 'priority' ? `P${id.split('-')[1]}` : label, `xmind-badge ${cls}`); badge.title = label; badgeRow.appendChild(badge); } }
      if (Array.isArray(e.topic.markers)) { const known = new Set([...priorities, ...progress].map(row => row[0])), extra = e.topic.markers.filter(m => !known.has(m?.markerId)); if (extra.length) { const badge = el('span', `＋${extra.length}`, 'xmind-badge'); badge.title = extra.map(m => m?.markerId || '自定义标记').join(' · '); badgeRow.appendChild(badge); } }
      if (badgeRow.children.length) card.appendChild(badgeRow);
      const actions = el('div', undefined, 'xmind-badges xmind-topic-actions');
      if (e.topic.notes) button(actions, '备注', '查看主题备注', event => { event.stopPropagation(); select(e.topic.id); properties.hidden = false; controls(); notes.focus({preventScroll:true}); notes.scrollIntoView({block:'nearest'}); }, 'xmind-note-mark');
      const href = topicLink(e.topic); if (href) { const b = button(actions, href.startsWith('yingxu:') ? '文件 ↗' : href.startsWith('xmind:') ? '主题 ↗' : '链接 ↗', '打开此主题链接', event => { event.stopPropagation(); openLink(href); }, 'xmind-link-mark'); b.title = href; }
      if (actions.children.length) card.appendChild(actions);
      if (Array.isArray(e.topic.labels) && e.topic.labels.length) { const row = el('div', undefined, 'xmind-tags'); row.title = e.topic.labels.join(' · '); for (const tag of e.topic.labels.slice(0, 3)) row.appendChild(el('span', String(tag), 'xmind-tag')); if (e.topic.labels.length > 3) row.appendChild(el('span', `＋${e.topic.labels.length - 3}`, 'xmind-tag')); card.appendChild(row); }
      if (editable && e.parent && e.group !== 'summary') { const grip = el('span', '⠿', 'xmind-grip'); grip.draggable = true; grip.title = '拖动整个分支到另一个主题'; card.appendChild(grip); listen(grip, 'dragstart', event => { if (!writeReady()) { event.preventDefault(); return; } finishEdit(); dragId = e.topic.id; if (!selection.has(dragId)) select(dragId); event.stopPropagation(); event.dataTransfer.setData('application/x-yingxu-topic', dragId); event.dataTransfer.effectAllowed = 'move'; }); listen(grip, 'dragend', () => { dragId = null; for (const c of cards.values()) c.classList.remove('drop-target'); }); }
      listen(card, 'click', event => { if (!event.target.closest('button,textarea')) select(e.topic.id, true, event); });
      listen(card, 'dblclick', event => { if (event.target.closest('button,textarea')) return; event.stopPropagation(); if (editable) { select(e.topic.id); startEdit(); } });
      listen(card, 'contextmenu', event => { event.preventDefault(); event.stopPropagation(); if (!ready()) return; if (!selection.has(e.topic.id)) select(e.topic.id); showMenu(event.clientX, event.clientY); });
      listen(card, 'dragover', event => { if (!dragId) return; event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect = 'move'; card.classList.toggle('drop-target', canMove(dragId, e.topic.id)); }); listen(card, 'dragleave', () => card.classList.remove('drop-target'));
      listen(card, 'drop', event => { if (!dragId) return; event.preventDefault(); event.stopPropagation(); const id = dragId; dragId = null; move(id, e.topic.id); }); return card;
    }
    function paint() {
      if (destroyed || !positions.size) return; world.style.transform = `translate(${x}px,${y}px) scale(${zoom})`; zoomButton.textContent = `${Math.round(zoom * 100)}%`;
      const keep = new Set(); for (const [id, p] of positions) if (inView(p) || id === editing?.id) { keep.add(id); let card = cards.get(id); if (!card) { card = makeCard(p); cards.set(id, card); nodeLayer.appendChild(card); } card.style.left = `${p.x}px`; card.style.top = `${p.y}px`; card.style.width = `${p.width}px`; card.style.minHeight = `${p.height}px`; card.classList.toggle('selected', selection.has(id)); card.classList.toggle('search-match', matches.includes(id)); card.setAttribute('aria-selected', String(selection.has(id))); }
      for (const [id, card] of cards) if (!keep.has(id)) { card.remove(); cards.delete(id); }
      svg.replaceChildren(); for (const p of positions.values()) { if (!p.entry.parent) continue; const a = positions.get(p.entry.parent.id); if (!inView(p) && !inView(a) && !inView({x:Math.min(p.x, a.x), y:Math.min(p.y, a.y), width:Math.abs(p.x - a.x) + p.width, height:Math.abs(p.y - a.y) + p.height})) continue; const ax = p.side > 0 ? a.x + a.width : a.x, bx = p.side > 0 ? p.x : p.x + p.width, ay = a.y + a.height / 2, by = p.y + p.height / 2, mid = (ax + bx) / 2; const path = document.createElementNS('http://www.w3.org/2000/svg', 'path'); path.setAttribute('d', `M ${ax} ${ay} C ${mid} ${ay}, ${mid} ${by}, ${bx} ${by}`); path.style.stroke = `var(--xmind-branch-${p.entry.branch})`; if (p.entry.group !== 'attached') path.setAttribute('stroke-dasharray', '5 5'); svg.appendChild(path); }
    }
    function renderOutline() { if (outline.hidden) return; outlineList.replaceChildren(); const query = search.value.trim(); for (const e of entries) { if (query ? !matches.includes(e.topic.id) : e.hidden) continue; const row = el('div', undefined, 'xmind-outline-row'); row.dataset.topicId = e.topic.id; row.style.paddingLeft = `${10 + Math.min(e.depth, 12) * 14}px`; const b = button(row, children(e.topic).length ? (folded.has(e.topic.id) ? '▸' : '▾') : '·', '展开或折叠分支', () => fold(e.topic.id)); b.disabled = !children(e.topic).length; const text = el('span', e.topic.title || '（空主题）'); text.tabIndex = 0; row.appendChild(text); listen(text, 'click', event => select(e.topic.id, true, event)); listen(text, 'dblclick', () => { focusTopic(e.topic.id); startEdit(); }); listen(text, 'keydown', event => { if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); focusTopic(e.topic.id); } }); row.classList.toggle('selected', selection.has(e.topic.id)); outlineList.appendChild(row); } }
    function updateSearch() { const query = search.value.trim().toLocaleLowerCase(); matches = query ? entries.filter(e => String(e.topic.title || '').toLocaleLowerCase().includes(query)).map(e => e.topic.id) : []; matchLabel.textContent = query ? `${matches.includes(selected) ? matches.indexOf(selected) + 1 : 0}/${matches.length}` : ''; }
    function render() { if (destroyed) return; index(); arrange(); updatePicker(); updateSearch(); for (const card of cards.values()) card.remove(); cards = new Map(); if (!initialized) { const saved = cameras[sheets[current].id || String(current)]; if (saved && [saved.zoom, saved.x, saved.y].every(Number.isFinite)) { zoom = clamp(saved.zoom, .2, 4); x = saved.x; y = saved.y; initialized = true; } else fit(); } paint(); renderOutline(); controls(); viewChanged(); }
    function panel(which) { if (!ready()) return; finishEdit(); const p = which === 'outline' ? outline : properties; p.hidden = !p.hidden; controls(); renderOutline(); paint(); viewChanged(); }
    function fold(id) { if (!ready() || !byId.has(id) || !children(byId.get(id).topic).length) return; finishEdit(); if (folded.has(id)) folded.delete(id); else { folded.add(id); const hidden = descendants(id); selection = new Set([...selection].filter(value => !hidden.has(value))); selection.add(id); selected = id; } render(); }
    function stepMatch(step) { if (!matches.length || !ready()) return; const at = matches.indexOf(selected); focusTopic(matches[(at + step + matches.length) % matches.length]); updateSearch(); }
    function refreshText(id) { const text = cards.get(id)?.querySelector('.xmind-topic-text'); if (text) text.textContent = byId.get(id).topic.title || '（空主题）'; updateSearch(); paint(); renderOutline(); controls(); }
    function compositionStart() { composing = true; onComposition?.(true); } function compositionEnd() { composing = false; onComposition?.(false); }
    function startEdit(initial) {
      if (!writeReady() || selection.size !== 1) return; finishEdit(); reveal(selected); render(); const entry = selectedEntry(); if (!cards.has(selected)) focusTopic(selected); const card = cards.get(selected); if (!card) return;
      const input = el('textarea', undefined, 'xmind-inline-editor'); input.setAttribute('aria-label', '直接编辑主题'); input.value = entry.topic.title || ''; input.rows = 2; card.querySelector('.xmind-topic-text').hidden = true; card.appendChild(input); card.classList.add('editing');
      editing = {id:selected, input, original:entry.topic.title || '', recorded:false}; listen(input, 'compositionstart', compositionStart); listen(input, 'compositionend', compositionEnd);
      listen(input, 'input', () => { if (!editable || !editing) return; if (!editing.recorded) { editing.redoBefore = [...redo]; remember(); editing.recorded = true; } entry.topic.title = input.value; changed(); });
      listen(input, 'keydown', event => { if (event.isComposing || composing || event.keyCode === 229) return; if (event.key === 'Enter' && !event.shiftKey && !event.ctrlKey && !event.metaKey) { event.preventDefault(); event.stopPropagation(); finishEdit(); stage.focus(); } if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); finishEdit(true); stage.focus(); } });
      listen(input, 'blur', () => { if (!composing) finishEdit(); }); input.focus({preventScroll:true}); input.select(); if (initial !== undefined) { input.value = initial; input.dispatchEvent(new Event('input', {bubbles:true})); input.setSelectionRange(input.value.length, input.value.length); }
    }
    function finishEdit(cancel = false) { if (!editing || composing) return; const old = editing; editing = null; if (cancel && old.recorded) { const before = undo.pop(); sheets = JSON.parse(before.content); redo.splice(0, redo.length, ...old.redoBefore); old.input.remove(); changed(); render(); return; } old.input.remove(); const card = cards.get(old.id); if (card) { card.classList.remove('editing'); const text = card.querySelector('.xmind-topic-text'); text.hidden = false; text.textContent = byId.get(old.id)?.topic.title || '（空主题）'; } arrange(); paint(); renderOutline(); controls(); viewChanged(); }
    function bindField(input, change) {
      listen(input, 'focus', () => { fieldSession = {input, recorded:false}; });
      listen(input, 'compositionstart', compositionStart); listen(input, 'compositionend', compositionEnd);
      listen(input, 'input', () => {
        if (!editable || input.readOnly || input === notes && (selection.size !== 1 || !plainNotesEditable(selectedEntry().topic))) return;
        if (fieldSession?.input !== input) fieldSession = {input, recorded:false};
        if (!fieldSession.recorded) { remember(); fieldSession.recorded = true; }
        change(input.value); changed(); undoButton.disabled = false; redoButton.disabled = true;
      });
      listen(input, 'blur', () => { if (!composing) { fieldSession = null; if (input === notes && selection.size === 1) render(); else { arrange(); paint(); renderOutline(); updatePicker(); } } });
    }
    bindField(title, text => { selectedEntry().topic.title = text; refreshText(selected); }); bindField(notes, text => { const t = selectedEntry().topic; t.notes ||= {}; t.notes.plain ||= {}; t.notes.plain.content = text; }); bindField(sheetTitle, text => { sheets[current].title = text; updatePicker(); });
    for (const input of [labels, link]) { listen(input, 'compositionstart', compositionStart); listen(input, 'compositionend', compositionEnd); }
    function add(sibling) { if (selection.size !== 1) return; let created; if (mutate(() => { const e = selectedEntry(); if (sibling && !e.parent) return false; if (totalTopics() >= 5000 || !sibling && e.depth >= 64) return blocked('已达到主题数量或层级上限。'); if (sibling && e.group === 'summary') return blocked('概要主题的关联结构请使用 XMind 修改。'); const owner = sibling ? e.parent : e.topic, group = sibling ? e.group : 'attached'; if (!structureReady(owner)) return false; owner.children ||= {}; owner.children[group] ||= []; created = {id:uid(), class:'topic', title:'新主题'}; const at = sibling ? owner.children[group].indexOf(e.topic) + 1 : owner.children[group].length; owner.children[group].splice(at, 0, created); folded.delete(owner.id); selected = created.id; selection = new Set([selected]); return true; })) { focusTopic(created.id); startEdit(); } }
    function requestDelete() {
      if (!writeReady()) return;
      const targets = selectionRoots({omitRoot:true}); if (!targets.length) return;
      // Validate the whole set before removing anything; a blocked branch keeps the batch intact.
      for (const e of targets) {
        if (e.group === 'summary') return blocked('概要主题的关联结构请使用 XMind 修改。');
        if (!structureReady(e.parent)) return;
        if (referenced(e.topic.id)) return blocked('所选分支被关系线引用，请在 XMind 中解除关联后删除。');
      }
      mutate(() => {
        const parentId = targets[0].parent.id;
        for (const e of targets) e.parent.children[e.group].splice(e.parent.children[e.group].indexOf(e.topic), 1);
        selected = parentId; selection = new Set([selected]); return true;
      });
      stage.focus({preventScroll:true});
    }
    function history(forward) { if (!writeReady()) return; finishEdit(); const from = forward ? redo : undo, to = forward ? undo : redo; if (!from.length) return; to.push(snapshot()); trim(to); const old = from.pop(); saveCamera(); sheets = JSON.parse(old.content); current = clamp(old.sheet, 0, sheets.length - 1); selected = old.selected; selection = new Set(old.selection || [selected]); fieldSession = null; initialized = false; changed(); render(); stage.focus(); }
    function canMove(id, targetId) { const e = byId.get(id), target = byId.get(targetId); if (!e?.parent || !target || e.group === 'summary' || target.group === 'summary' || e.parent.id === targetId || ranged(e.parent) || ranged(target.topic)) return false; const ids = descendants(id); if (ids.has(targetId)) return false; const height = Math.max(...entries.filter(e => ids.has(e.topic.id)).map(c => c.depth - e.depth)); return target.depth + 1 + height <= 64; }
    function move(id, targetId) {
      const targets = selection.has(id) ? selectionRoots() : [byId.get(id)];
      if (!targets.length || targets.some(e => !e || !canMove(e.topic.id, targetId))) return blocked('请选择另一个主题；不能移动中心主题、概要或移入自己的分支。');
      if (mutate(() => {
        const target = byId.get(targetId).topic; target.children ||= {}; target.children.attached ||= [];
        for (const e of targets) { e.parent.children[e.group].splice(e.parent.children[e.group].indexOf(e.topic), 1); target.children.attached.push(e.topic); }
        folded.delete(targetId); selected = id; return true;
      })) { const p = positions.get(id); if (p && !inView(p)) { x = stage.clientWidth / 2 - (p.x + p.width / 2) * zoom; y = stage.clientHeight / 2 - (p.y + p.height / 2) * zoom; paint(); viewChanged(); } stage.focus({preventScroll:true}); }
    }
    function reorder(step) { mutate(() => { const e = selectedEntry(), list = e.parent?.children?.[e.group]; if (!list || !structureReady(e.parent)) return false; const at = list.indexOf(e.topic), next = at + step; if (next < 0 || next >= list.length) return false; [list[at], list[next]] = [list[next], list[at]]; return true; }); }
    function validStyle(topic) { return (topic.style == null || typeof topic.style === 'object' && !Array.isArray(topic.style)) && (topic.style?.properties == null || typeof topic.style.properties === 'object' && !Array.isArray(topic.style.properties)); }
    function setStyle(values) {
      if (!selection.size) return;
      mutate(() => {
        const targets = selectedEntries();
        if (targets.some(e => !validStyle(e.topic))) return blocked('所选文件使用了其他样式结构，已保留原样。');
        for (const {topic} of targets) {
          topic.style ||= {id:uid(), type:'topic', properties:{}}; topic.style.properties ||= {};
          for (const [key, val] of Object.entries(values)) { if (val) topic.style.properties[key] = val; else delete topic.style.properties[key]; }
        }
        return true;
      });
    }
    function outlineText(id = selected) { const base = byId.get(id); if (!base) return ''; const ids = descendants(id); return entries.filter(e => ids.has(e.topic.id)).map(e => `${'\t'.repeat(e.depth - base.depth)}${String(e.topic.title || '').replace(/\n/g, ' ')}`).join('\n'); }
    function selectionText(branch) { return (branch ? selectionRoots() : selectedEntries()).map(e => branch ? outlineText(e.topic.id) : e.topic.title || '').join('\n'); }
    function copy(branch) { if (!selection.size) return; const text = selectionText(branch); if (onCopy) { onCopy(text); return; } const fallback = () => { properties.hidden = false; controls(); title.focus(); title.select(); blocked('文字已选中，请按 Ctrl+C / ⌘C 复制。'); }; if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).catch(fallback); else fallback(); }
    function showMenu(px, py) {
      for (const b of menu.children) {
        const name = b.textContent;
        if (['编辑文字','添加子主题','添加同级主题'].includes(name)) b.disabled = !editable || selection.size !== 1 || name === '添加同级主题' && !selectedEntry().parent;
        if (name === '删除分支') b.disabled = deleteButton.disabled;
        if (name === '打开链接') b.disabled = selection.size !== 1 || !topicLink(selectedEntry().topic);
      }
      menu.hidden = false; const r = root.getBoundingClientRect(); menu.style.left = `${clamp(px - r.left, 0, Math.max(0, r.width - 180))}px`; menu.style.top = `${clamp(py - r.top, 0, Math.max(0, r.height - menu.offsetHeight))}px`; menu.querySelector('button:not(:disabled)')?.focus();
    }
    function endPan() { if (!pan) return; const old = pan; pan = null; stage.classList.remove('panning'); if (stage.hasPointerCapture(old.id)) stage.releasePointerCapture(old.id); viewChanged(); }
    function endMarquee(cancel = false) {
      if (!marquee) return; const old = marquee; marquee = null;
      marqueeBox.hidden = true; stage.classList.remove('selecting');
      if (stage.hasPointerCapture(old.id)) stage.releasePointerCapture(old.id);
      if (cancel) setSelection([...old.base], old.primary); else { controls(); viewChanged(); }
    }
    function updateMarquee(event) {
      const r = stage.getBoundingClientRect(), px = clamp(event.clientX - r.left, 0, r.width), py = clamp(event.clientY - r.top, 0, r.height);
      const left = Math.min(marquee.x, px), top = Math.min(marquee.y, py), width = Math.abs(px - marquee.x), height = Math.abs(py - marquee.y);
      if (!marquee.moved && Math.max(width, height) < 4) return;
      marquee.moved = true; marqueeBox.hidden = false;
      Object.assign(marqueeBox.style, {left:`${left}px`, top:`${top}px`, width:`${width}px`, height:`${height}px`});
      const next = new Set(marquee.add ? marquee.base : []);
      for (const [id, p] of positions) {
        const lx = p.x * zoom + x, ty = p.y * zoom + y;
        if (lx <= left + width && lx + p.width * zoom >= left && ty <= top + height && ty + p.height * zoom >= top) {
          if (marquee.toggle && marquee.base.has(id)) next.delete(id); else next.add(id);
        }
      }
      selection = next; selected = next.has(marquee.primary) ? marquee.primary : [...next][0] || selected;
      paint(); stats.textContent = `${entries.length} 个主题 · 已选 ${next.size}`;
    }
    listen(stage, 'wheel', event => { if (!event.deltaX && !event.deltaY) return; event.preventDefault(); event.stopPropagation(); menu.hidden = true; const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? stage.clientHeight : 1; if (event.ctrlKey || event.metaKey) { const r = stage.getBoundingClientRect(); zoomAt(zoom * Math.exp(-clamp(event.deltaY * scale, -240, 240) * .002), event.clientX - r.left, event.clientY - r.top); } else { x -= (event.shiftKey ? event.deltaY : event.deltaX) * scale; y -= (event.shiftKey ? 0 : event.deltaY) * scale; paint(); viewChanged(); } }, {passive:false});
    listen(stage, 'pointerdown', event => {
      menu.hidden = true;
      const panning = event.button === 1 || event.button === 0 && space;
      if (!panning && (event.button !== 0 || event.target.closest('.xmind-topic'))) return;
      if (!ready() || pan || marquee) return;
      event.preventDefault(); event.stopPropagation(); finishEdit(); stage.focus({preventScroll:true});
      if (panning) { pan = {id:event.pointerId, x:event.clientX, y:event.clientY}; stage.classList.add('panning'); }
      else {
        const r = stage.getBoundingClientRect();
        marquee = {id:event.pointerId, x:event.clientX - r.left, y:event.clientY - r.top, base:new Set(selection), primary:selected, add:event.ctrlKey || event.metaKey || event.shiftKey, toggle:event.ctrlKey || event.metaKey, moved:false};
        if (!marquee.add) setSelection([]); window.getSelection()?.removeAllRanges(); stage.classList.add('selecting');
      }
      try { stage.setPointerCapture(event.pointerId); } catch (_) { /* document fallback */ }
    });
    listen(document, 'pointermove', event => {
      if (marquee && event.pointerId === marquee.id) { if (!event.buttons) { endMarquee(); return; } event.preventDefault(); updateMarquee(event); return; }
      if (!pan || event.pointerId !== pan.id) return; if (!event.buttons) { endPan(); return; } event.preventDefault(); x += event.clientX - pan.x; y += event.clientY - pan.y; pan.x = event.clientX; pan.y = event.clientY; paint();
    }, {capture:true, passive:false});
    for (const type of ['pointerup', 'pointercancel']) listen(document, type, event => { if (pan?.id === event.pointerId) endPan(); if (marquee?.id === event.pointerId) endMarquee(type === 'pointercancel'); }, {capture:true});
    listen(stage, 'lostpointercapture', () => { endPan(); endMarquee(); }); listen(window, 'blur', () => { endPan(); endMarquee(true); space = false; stage.classList.remove('pan-ready'); });
    for (const type of ['mousedown', 'auxclick']) listen(stage, type, event => { if (event.button === 1) { event.preventDefault(); event.stopPropagation(); } });
    listen(root, 'pointerdown', event => { if (!menu.contains(event.target)) menu.hidden = true; }, {capture:true});
    listen(root, 'copy', event => { if (event.target.closest('input,textarea') || window.getSelection()?.toString() || !event.clipboardData) return; event.preventDefault(); event.stopPropagation(); event.clipboardData.setData('text/plain', selectionText(false)); });
    listen(root, 'paste', event => {
      if (event.target.closest('input,textarea') || !stage.contains(event.target)) return; const text = event.clipboardData?.getData('text/plain'); if (!text || event.clipboardData.files.length) return; event.preventDefault(); event.stopPropagation(); if (!writeReady() || selection.size !== 1) return;
      const lines = text.replace(/\r\n?/g, '\n').split('\n').filter(line => line.trim()); if (!lines.length) return; const entry = selectedEntry(); if (!structureReady(entry.topic)) return; if (lines.length > 5000 - totalTopics() || new TextEncoder().encode(text).length > 256 * 1024) return blocked('粘贴内容超过主题数量或文本限制。');
      const parsed = lines.map(line => ({depth:line.match(/^\t*/)[0].length, title:line.replace(/^\t*/, '')})), base = Math.min(...parsed.map(line => line.depth)); let level = -1;
      for (const row of parsed) { row.depth = Math.min(row.depth - base, level + 1); level = row.depth; if (entry.depth + row.depth + 1 > 64) return blocked('粘贴后超过 64 层深度。'); }
      mutate(() => { const owners = [entry.topic]; for (const row of parsed) { const owner = owners[row.depth] || entry.topic, topic = {id:uid(), class:'topic', title:row.title}; owner.children ||= {}; owner.children.attached ||= []; owner.children.attached.push(topic); owners[row.depth + 1] = topic; owners.length = row.depth + 2; } folded.delete(selected); return true; });
    });
    listen(root, 'keydown', event => {
      const mod = event.ctrlKey || event.metaKey, key = event.key.toLowerCase(), input = !!event.target.closest('input,textarea,select,[contenteditable=true]');
      if (event.isComposing || composing || event.keyCode === 229) { if (!input && ['tab','enter','delete'].includes(key)) { event.preventDefault(); event.stopPropagation(); } return; }
      if (mod && key === 'f') { event.preventDefault(); event.stopPropagation(); search.focus(); search.select(); return; }
      if (key === 'escape') { menu.hidden = true; if (marquee) endMarquee(true); else setSelection([]); controls(); if (search.value) { search.value = ''; updateSearch(); paint(); renderOutline(); } event.stopPropagation(); return; }
      if (input) return;
      if (event.target.closest('button,select,summary') && !mod && key !== 'delete') return;
      if (mod && key === 'a') { event.preventDefault(); event.stopPropagation(); setSelection(visible.map(e => e.topic.id), selected, true); return; }
      if (mod && key === 'c' && event.shiftKey) { event.preventDefault(); event.stopPropagation(); copy(true); return; }
      const action = mod && key === 'z' ? () => history(event.shiftKey) : mod && key === 'y' ? () => history(true) : mod && key === '0' ? fit : mod && key === '1' ? () => zoomAt(1) : mod && ['+','='].includes(key) ? () => zoomAt(zoom * 1.2) : mod && key === '-' ? () => zoomAt(zoom / 1.2) : mod && key === ' ' ? foldSelection : key === 'f2' ? () => startEdit() : key === 'tab' && !event.shiftKey ? () => add(false) : key === 'enter' ? () => add(true) : ['delete','backspace'].includes(key) ? requestDelete : null;
      if (action) { event.preventDefault(); event.stopPropagation(); action(); return; }
      if (key === ' ') { event.preventDefault(); space = true; stage.classList.add('pan-ready'); return; }
      if (key.startsWith('arrow')) { event.preventDefault(); event.stopPropagation(); const e = selectedEntry(), at = visible.findIndex(c => c.topic.id === selected); const next = key === 'arrowup' ? visible[Math.max(0, at - 1)] : key === 'arrowdown' ? visible[Math.min(visible.length - 1, at + 1)] : key === 'arrowleft' ? byId.get(e.parent?.id) : byId.get(children(e.topic)[0]?.topic.id); if (next) focusTopic(next.topic.id); return; }
      if (!mod && !event.altKey && event.key.length === 1 && stage.contains(event.target)) { event.preventDefault(); event.stopPropagation(); startEdit(event.key); }
    });
    listen(root, 'keyup', event => { if (event.key === ' ') { space = false; stage.classList.remove('pan-ready'); } });
    listen(picker, 'change', () => { if (!ready()) { picker.value = String(current); return; } finishEdit(); saveCamera(); current = Number(picker.value); selected = sheets[current].rootTopic.id; selection = new Set([selected]); fieldSession = null; initialized = false; render(); });
    listen(direction, 'change', () => { if (!ready()) { direction.value = layout; return; } finishEdit(); layout = direction.value; render(); fit(); }); listen(search, 'input', () => { updateSearch(); paint(); renderOutline(); }); listen(search, 'keydown', event => { if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); event.stopPropagation(); stepMatch(event.shiftKey ? -1 : 1); } });
    listen(fontSize, 'change', () => setStyle({'fo:font-size':fontSize.value})); listen(colors, 'change', () => { const [fill, color] = colors.value.split('|'); setStyle({'svg:fill':fill, 'fo:color':color}); });
    const observer = new ResizeObserver(() => { if (!initialized) fit(); else paint(); }); observer.observe(stage); render();
    return {isComposing:() => composing, getViewState, focusSearch:() => search.focus(), destroy:() => { if (destroyed) return; finishEdit(); endPan(); endMarquee(true); viewChanged(); destroyed = true; observer.disconnect(); abort.abort(); root.remove(); }};
  }
  window.YingXuXmind = {mount};
})();
