/* Offline topic editor: ancillary XMind fields stay in the original JSON model. */
(function () {
  'use strict';
  function mount({parent, value, editable, selectedId, sheetIndex = 0, notice, onChange, onSelection, onComposition, onBlocked}) {
    let sheets = JSON.parse(value), current = Math.min(sheetIndex, sheets.length - 1), selected = selectedId;
    let composing = false, deleting = false;
    const undo = [], redo = [];
    const el = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; };
    const root = el('section', undefined, 'xmind-editor');
    const info = el('p', notice, 'xmind-notice');
    const toolbar = el('div', undefined, 'xmind-tools');
    const picker = el('select'); picker.setAttribute('aria-label', '思维导图画布');
    sheets.forEach((sheet, i) => { const option = el('option', sheet.title || `画布 ${i + 1}`); option.value = String(i); picker.appendChild(option); });
    picker.value = String(current); toolbar.appendChild(picker);
    const button = (text, action) => { const node = el('button', text, 'button button-secondary button-small'); node.type = 'button'; node.addEventListener('click', action); toolbar.appendChild(node); return node; };
    const guarded = action => { if (!editable || composing) { if (composing) onBlocked?.(); return; } action(); };
    const changed = () => onChange(JSON.stringify(sheets));
    function record() { undo.push(JSON.stringify(sheets)); if (undo.length > 50) undo.shift(); redo.length = 0; }
    function walk() {
      const result = [], pending = [{topic:sheets[current].rootTopic, parent:null, group:null, depth:0}];
      while (pending.length) {
        const entry = pending.pop(); result.push(entry);
        for (const [kind, group] of Object.entries(entry.topic.children || {}).reverse())
          for (let i = group.length - 1; i >= 0; i--) pending.push({topic:group[i],parent:entry.topic,group:kind,depth:entry.depth + 1});
      }
      return result;
    }
    let entries = [], cards = new Map();
    const selectedEntry = () => entries.find(entry => entry.topic.id === selected) || entries[0];
    function add(sibling) { guarded(() => {
      const entry = selectedEntry(); if (sibling && !entry.parent) return;
      if (sheets.reduce((total, sheet) => { const pending = [sheet.rootTopic]; while (pending.length) { const topic = pending.pop(); total++; Object.values(topic.children || {}).forEach(group => pending.push(...group)); } return total; }, 0) >= 5000 || (!sibling && entry.depth >= 64)) { onBlocked?.('已达到主题数量或层级上限。'); return; }
      record();
      const owner = sibling ? entry.parent : entry.topic, kind = sibling ? entry.group : 'attached';
      owner.children ||= {}; owner.children[kind] ||= [];
      const topic = {id:crypto.randomUUID().replaceAll('-', ''),class:'topic',title:'新主题'};
      const index = sibling ? owner.children[kind].indexOf(entry.topic) + 1 : owner.children[kind].length;
      owner.children[kind].splice(index, 0, topic); selected = topic.id; changed(); render(); title.focus(); title.select();
    }); }
    const childButton = button('添加子主题', () => add(false));
    const siblingButton = button('添加同级主题', () => add(true));
    const deleteButton = button('删除主题及分支', () => guarded(() => {
      const entry = selectedEntry(); if (!entry.parent) return;
      if (!deleting) { deleting = true; deleteButton.textContent = '确认删除此主题及所有分支'; return; }
      record(); entry.parent.children[entry.group].splice(entry.parent.children[entry.group].indexOf(entry.topic), 1);
      selected = entry.parent.id; changed(); render();
    }));
    const undoButton = button('撤销', () => guarded(() => { if (!undo.length) return; redo.push(JSON.stringify(sheets)); sheets = JSON.parse(undo.pop()); changed(); render(); }));
    const redoButton = button('重做', () => guarded(() => { if (!redo.length) return; undo.push(JSON.stringify(sheets)); sheets = JSON.parse(redo.pop()); changed(); render(); }));
    const form = el('label', undefined, 'xmind-title-field'); form.appendChild(el('span', '当前主题'));
    const title = el('input'); title.type = 'text'; title.setAttribute('aria-label', '主题文字'); title.readOnly = !editable; form.appendChild(title);
    const stage = el('div', undefined, 'xmind-stage'); stage.setAttribute('aria-label', '思维导图主题结构');
    stage.tabIndex = 0;
    root.append(info, toolbar, form, stage); parent.replaceChildren(root);
    function controls() {
      const entry = selectedEntry(); selected = entry.topic.id; title.value = entry.topic.title || '';
      for (const [id, card] of cards) { card.classList.toggle('selected', id === selected); card.setAttribute('aria-pressed', String(id === selected)); }
      childButton.disabled = !editable; siblingButton.disabled = deleteButton.disabled = !editable || !entry.parent;
      undoButton.disabled = !editable || !undo.length; redoButton.disabled = !editable || !redo.length;
      deleting = false; deleteButton.textContent = '删除主题及分支'; onSelection?.(selected, current);
    }
    function render() {
      entries = walk(); cards = new Map(); const branches = new Map();
      const tree = el('ul', undefined, 'xmind-tree');
      entries.forEach(entry => {
        const branch = el('li', undefined, 'xmind-branch');
        const card = el('button', entry.topic.title || '（空主题）', 'xmind-topic'); card.type = 'button';
        if (entry.group === 'detached') card.classList.add('detached');
        card.addEventListener('click', () => { if (composing) { onBlocked?.(); return; } selected = entry.topic.id; controls(); });
        const children = el('ul'); branch.appendChild(card); if (Object.values(entry.topic.children || {}).some(group => group.length)) branch.appendChild(children);
        branches.set(entry.topic.id, children); cards.set(entry.topic.id, card);
        (entry.parent ? branches.get(entry.parent.id) : tree).appendChild(branch);
      });
      stage.replaceChildren(tree); controls();
    }
    title.addEventListener('compositionstart', () => { composing = true; onComposition?.(true); });
    title.addEventListener('compositionend', () => { composing = false; onComposition?.(false); });
    title.addEventListener('input', () => {
      if (!editable) return;
      record(); selectedEntry().topic.title = title.value;
      cards.get(selected).textContent = title.value || '（空主题）'; changed();
      undoButton.disabled = false; redoButton.disabled = true; deleting = false; deleteButton.textContent = '删除主题及分支';
    });
    picker.addEventListener('change', () => {
      if (composing) { picker.value = String(current); onBlocked?.(); return; }
      current = Number(picker.value); selected = sheets[current].rootTopic.id; render();
    });
    render();
    return {isComposing:() => composing, destroy:() => { root.remove(); }};
  }
  window.YingXuXmind = {mount};
})();
