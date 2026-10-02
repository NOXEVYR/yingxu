'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const read=name=>fs.readFileSync(path.join(__dirname,'../frontend',name),'utf8');
const appearance=read('appearance.css'),marquee=read('marquee.css');
const keys=['swiss','graphite','paper','pine','ocean','plum'];

function palette(key){
  const block=appearance.match(new RegExp(':root\\[data-appearance="'+key+'"\\]\\s*\\{([^}]+)\\}'));
  assert.ok(block,'missing palette: '+key);
  return Object.fromEntries([...block[1].matchAll(/--([\w-]+):\s*([^;]+);/g)].map(match=>[match[1],match[2].trim()]));
}
function rgb(hex){
  assert.match(hex,/^#[\da-f]{6}$/i);
  return hex.slice(1).match(/../g).map(value=>parseInt(value,16));
}
function fill(value){
  const match=value?.match(/^rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(0?\.\d+)\s*\)$/);
  assert.ok(match,'marquee fill must use a WebView-compatible rgba color');
  return {channels:match.slice(1,4).map(Number),alpha:Number(match[4])};
}
function luminance(channels){
  const linear=channels.map(value=>value/255).map(value=>value<=0.04045?value/12.92:((value+0.055)/1.055)**2.4);
  return 0.2126*linear[0]+0.7152*linear[1]+0.0722*linear[2];
}
function contrast(a,b){
  const x=luminance(a),y=luminance(b);
  return (Math.max(x,y)+0.05)/(Math.min(x,y)+0.05);
}

test('marquee border and translucent fill use all six live palette tokens',()=>{
  const box=marquee.match(/\.yx-marquee-box\s*\{([^}]+)\}/)?.[1];
  assert.ok(box);
  assert.match(box,/border\s*:\s*1px solid var\(--accent\)/);
  const background=box.match(/background\s*:\s*var\(--marquee-fill\s*,\s*(rgba\([^)]*\))\)/);
  assert.ok(background,'marquee fill needs a neutral fallback and live palette token');
  assert.match(box,/pointer-events\s*:\s*none/);
  assert.doesNotMatch(box,/opacity\s*:|animation\s*:|transition\s*:|color-mix\s*\(/);
  assert.deepEqual(fill(background[1]),fill(palette('swiss')['marquee-fill']));
  assert.doesNotMatch(marquee,/#87ad9724/i);
  const html=read('index.html');
  assert.ok(html.indexOf('/marquee.css')<html.indexOf('/appearance.css'));
  assert.doesNotMatch(read('marquee.js'),/\.style\.(?:background|border|opacity)|setProperty\(['"]--marquee-fill/);
  for(const key of keys){
    const tokens=palette(key),tint=fill(tokens['marquee-fill']);
    assert.deepEqual(tint.channels,rgb(tokens.accent),key+': fill must share the accent hue');
    assert.ok(tint.alpha>0&&tint.alpha<=0.15,key+': fill must remain translucent');
  }
});

test('translucent marquee leaves resource text readable and its edge visible in every palette',()=>{
  let total=0;
  for(const key of keys){
    const tokens=palette(key),tint=fill(tokens['marquee-fill']);
    // The rectangle overlays both text and surface pixels; composite both before measuring.
    const overlay=hex=>rgb(hex).map((value,i)=>value*(1-tint.alpha)+tint.channels[i]*tint.alpha);
    const measured=[];
    for(const surface of ['bg','panel','panel-2','panel-3','panel-hover','accent-dim','accent-soft']){
      assert.ok(contrast(rgb(tokens.accent),overlay(tokens[surface]))>=3,key+': marquee edge must remain visible');
      for(const foreground of ['text','muted','subtle','accent']){
        const ratio=contrast(overlay(tokens[foreground]),overlay(tokens[surface]));
        assert.ok(ratio>=4.5,`${key}: ${foreground}/${surface} under marquee = ${ratio.toFixed(3)}:1`);
        measured.push(ratio);
      }
    }
    total+=measured.length;
    console.log(`${key}: ${measured.length} tinted text pairs, minimum ${Math.min(...measured).toFixed(3)}:1`);
  }
  assert.equal(total,168);
});

test('real appearance switching updates the root synchronously while retaining drafts and selection',()=>{
  const root={dataset:{}},document={documentElement:root,querySelector(){throw Error('Appearance must not remount the workspace');}};
  const context=vm.createContext({window:{},document,localStorage:{getItem:()=>null},setTimeout(){throw Error('Appearance must not schedule work');},clearTimeout(){}});
  vm.runInContext(read('app.js').replace(/boot\(\);\s*$/,'')+';globalThis.fixture={state,applyAppearance};',context);
  const {state,applyAppearance}=context.fixture;
  const draft={body:'未保存正文'},selected=new Set(['resource-one']),tab={key:'file:one',draft};
  Object.assign(state,{bootstrap:{settings:{}},selectedIds:selected,tabs:[tab],activeKey:tab.key,drafts:{one:draft}});
  for(const key of [...keys,'plum','swiss']){
    state.bootstrap.settings.appearance_theme=key;
    applyAppearance();
    assert.equal(root.dataset.appearance,key);
    assert.ok(palette(root.dataset.appearance)['marquee-fill']);
    assert.equal(state.selectedIds,selected);
    assert.equal(state.tabs[0],tab);
    assert.equal(state.drafts.one,draft);
  }
  state.bootstrap.settings.appearance_theme='unknown';
  applyAppearance();
  assert.equal(root.dataset.appearance,'swiss');
});
