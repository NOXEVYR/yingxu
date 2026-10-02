'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const css=fs.readFileSync(path.join(__dirname,'../frontend/appearance.css'),'utf8');
const keys=['swiss','graphite','paper','pine','ocean','plum'];

function palette(key){
  const block=css.match(new RegExp(':root\\[data-appearance="'+key+'"\\]\\s*\\{([^}]+)\\}'));
  assert.ok(block,'missing light palette: '+key);
  return Object.fromEntries([...block[1].matchAll(/--([\w-]+):\s*(#[\da-f]{6})(?=\s*;)/gi)].map(match=>[match[1],match[2]]));
}
function luminance(hex){
  assert.match(hex,/^#[\da-f]{6}$/i);
  const channels=hex.slice(1).match(/../g).map(value=>parseInt(value,16)/255)
    .map(value=>value<=0.04045?value/12.92:((value+0.055)/1.055)**2.4);
  return 0.2126*channels[0]+0.7152*channels[1]+0.0722*channels[2];
}
function contrast(a,b){
  const x=luminance(a),y=luminance(b);
  return (Math.max(x,y)+0.05)/(Math.min(x,y)+0.05);
}

test('all six real UI palettes keep small text and functional states readable',()=>{
  let total=0;
  for(const key of keys){
    const tokens=palette(key),pairs=[];
    const surfaces=['bg','panel','panel-2','panel-3','panel-hover','accent-dim','accent-soft'];
    for(const foreground of ['text','muted','subtle','accent']){
      for(const background of surfaces)pairs.push([foreground,background]);
    }
    pairs.push(['accent-text','accent'],['accent-text','accent-bright'],
      ['danger','danger-soft'],['danger','bg'],['success','success-soft'],['disabled','disabled-bg']);
    const measured=pairs.map(([foreground,background])=>{
      const ratio=contrast(tokens[foreground],tokens[background]);
      assert.ok(ratio>=4.5,`${key}: ${foreground}/${background} = ${ratio.toFixed(3)}:1`);
      return ratio;
    });
    for(const surface of surfaces){
      assert.ok(luminance(tokens[surface])>0.70,`${key}: ${surface} must remain light`);
    }
    total+=pairs.length;
    console.log(`${key}: ${pairs.length} text pairs, minimum ${Math.min(...measured).toFixed(2)}:1`);
  }
  assert.equal(total,204);
});

test('themes remain self-contained and disabled buttons use opaque dedicated colors',()=>{
  assert.doesNotMatch(css,/@font-face|@import|url\s*\(/i);
  const block=css.match(/\.app-shell button:disabled,\.app-dialog button:disabled,\.context-menu button:disabled\s*\{([^}]+)\}/);
  assert.ok(block,'all workbench button hosts need the dedicated disabled state');
  assert.match(block[1],/opacity\s*:\s*1\s*;/);
  assert.match(block[1],/color\s*:\s*var\(--disabled\)/);
  assert.match(block[1],/background\s*:\s*var\(--disabled-bg\)/);
});
