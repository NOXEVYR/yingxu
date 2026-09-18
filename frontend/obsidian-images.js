/* Obsidian image syntax is a view of the source, never a rewrite of the note. */
(function(scope){
  'use strict';
  function parseImage(source) {
    const match = /^!\[\[([^\]\r\n]+)\]\]$/.exec(source);
    if (!match) return null;
    const parts=match[1].split('|'); if(parts.length>2)return null;
    const path=parts[0].trim(),label=(parts[1]||'').trim();
    if(!path || path.length>4096 || /^[\/\\]|[:\\\x00-\x1f\x7f?#]/.test(path) || path.split('/').some(p=>!p||p==='.'||p==='..') || !/\.(png|jpe?g|webp|gif|bmp)$/i.test(path))return null;
    const size=/^(\d+)(?:x(\d+))?$/.exec(label);
    if(size && (+size[1]<1 || +size[1]>2000 || size[2] && (+size[2]<1 || +size[2]>2000)))return null;
    return {path,alt:size ? path.split('/').at(-1) : label || path.split('/').at(-1),width:size?+size[1]:null,height:size?.[2]?+size[2]:null};
  }
  scope.YingXuObsidian={parseImage};
  if(typeof module!=='undefined')module.exports=scope.YingXuObsidian;
})(typeof window!=='undefined'?window:globalThis);
