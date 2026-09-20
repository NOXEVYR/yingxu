/* Shared, escaped Markdown preview for the workspace and offline reader. */
(() => {
'use strict';
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
function inlineMarkdown(text,imageResolver = () => null,linkResolver = () => null) {
  const tokens = []; let raw = String(text).replace(/\u0000/g,'');
  const token = html => { const key = `\u0000${tokens.length}\u0000`; tokens.push(html); return key; };
  raw = raw.replace(/(`+)([\s\S]*?)\1(?!`)/g,(_,marks,code) => token(`<code>${escapeHtml(code)}</code>`));
  raw = raw.replace(/\\([\\!])/g,(_,character)=>token(escapeHtml(character)));
  raw = raw.replace(/!\[\[[^\]\r\n]+\]\]/g,source => {
    const spec=window.YingXuObsidian?.parseImage(source);if(!spec)return source;
    const resolved=imageResolver(encodeURIComponent(spec.path).replace(/%2F/gi,'/'),{wiki:true});
    return token(resolved ? `<img class="markdown-attachment" src="${escapeHtml(resolved)}" alt="${escapeHtml(spec.alt)}"${spec.width ? ` width="${spec.width}"` : ''}${spec.height ? ` height="${spec.height}" style="height:${spec.height}px;object-fit:contain"` : ''} loading="lazy" decoding="async">` : escapeHtml(source));
  });
  raw = raw.replace(/!\[([^\]\n]*)\]\((?:<([^>\n]+)>|([^\s)]+))\)/g,(source,alt,angle,url) => {
    const resolved = imageResolver(angle || url);
    return token(resolved ? `<img class="markdown-attachment" src="${escapeHtml(resolved)}" alt="${escapeHtml(alt)}" loading="lazy" decoding="async">` : escapeHtml(source));
  });
  raw = raw.replace(/\[((?:\\.|[^\]\\\n])+)\]\((?:<([^>\n]+)>|([^\s)]+))\)/g,(source,label,angle,url)=>{
    const path=angle||url, name=label.replace(/\\([\\`*_{}\[\]()#+\-.!])/g,'$1');
    if(/^https?:\/\//i.test(path))return token(`<a href="${escapeHtml(path)}" target="_blank" rel="noopener noreferrer">${escapeHtml(name)}</a>`);
    return token(linkResolver(name,path)||escapeHtml(source));
  });
  let value = escapeHtml(raw);
  value = value.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/__([^_]+)__/g,'<strong>$1</strong>').replace(/(?<!\*)\*([^*]+)\*(?!\*)/g,'<em>$1</em>').replace(/~~([^~]+)~~/g,'<del>$1</del>');
  return value.replace(/\u0000(\d+)\u0000/g,(_,index) => tokens[Number(index)] || '');
}
function render(raw,inline = text => inlineMarkdown(text)) {
  const maximum = 140000; const source = String(raw || ''); const truncated = source.length > maximum; const lines = source.slice(0,maximum).replace(/\r\n?/g,'\n').split('\n'); let output = truncated ? '<div class="preview-notice">为保持流畅，预览显示前 14 万字符。编辑区保留完整内容。</div>' : ''; let code = false,buffer = [],list = '';
  const closeList = () => { if (list) { output += `</${list}>`; list = ''; } };
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index]; if (/^\s*```/.test(line)) { closeList(); if (code) { output += `<pre><code>${escapeHtml(buffer.join('\n'))}</code></pre>`; buffer = []; } code = !code; continue; }
    if (code) { buffer.push(line); continue; }
    if (!line.trim()) { closeList(); continue; }
    if (/^\s*\|?.+\|.+/.test(line) && /^\s*\|?\s*:?-{3,}/.test(lines[index+1] || '')) {
      closeList(); const cells = value => value.trim().replace(/^\||\|$/g,'').split('|').map(cell => cell.trim()); const headers = cells(line); output += `<table><thead><tr>${headers.map(cell => `<th>${inline(cell)}</th>`).join('')}</tr></thead><tbody>`; index += 2; let rowCount = 0;
      while (index < lines.length && lines[index].includes('|') && rowCount < 1000) { output += `<tr>${cells(lines[index]).map(cell => `<td>${inline(cell)}</td>`).join('')}</tr>`; index++; rowCount++; } index--; output += '</tbody></table>'; continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/); if (heading) { closeList(); output += `<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`; continue; }
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { closeList(); output += '<hr>'; continue; }
    const li = line.match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/); if (li) { const kind = li[2] ? 'ol' : 'ul'; if (list !== kind) { closeList(); list = kind; output += `<${kind}>`; } const value = li[3].replace(/^\[ \]\s*/,'☐ ').replace(/^\[x\]\s*/i,'☑ '); output += `<li>${inline(value)}</li>`; continue; }
    closeList(); if (/^>\s?/.test(line)) output += `<blockquote><p>${inline(line.replace(/^>\s?/,''))}</p></blockquote>`; else output += `<p>${inline(line)}</p>`;
  }
  closeList(); if (code) output += `<pre><code>${escapeHtml(buffer.join('\n'))}</code></pre>`; return output || '<p style="color:var(--subtle)">从第一句话开始。</p>';
}

window.YingXuPreview = Object.freeze({inline:inlineMarkdown,render});
})();
