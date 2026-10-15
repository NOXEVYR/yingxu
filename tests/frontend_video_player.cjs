'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');

class FakeClassList {
  constructor(){this.values=new Set();}
  add(value){this.values.add(value);}
  remove(value){this.values.delete(value);}
  contains(value){return this.values.has(value);}
}
class FakeElement {
  constructor(name='div',classes=[]){this.name=name;this.classList=new FakeClassList();for(const value of classes)this.classList.add(value);this.children=[];this.parentElement=null;this.handlers=new Map();this.attributes=new Map();this.dataset={};this.textContent='';this.value='';this.disabled=false;this.hidden=false;this.hover=false;this._focused=false;}
  append(...nodes){for(const node of nodes){node.parentElement=this;this.children.push(node);}}
  addEventListener(type,fn){const rows=this.handlers.get(type)||[];rows.push(fn);this.handlers.set(type,rows);}
  emit(type,properties={}){
    const event={type,target:this,currentTarget:this,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;},stopPropagation(){this.stopped=true;},...properties};
    let node=this;
    while(node){event.currentTarget=node;for(const fn of node.handlers.get(type)||[])fn(event);if(event.stopped)break;node=node.parentElement;}
    return event;
  }
  setAttribute(name,value){this.attributes.set(name,String(value));}
  getAttribute(name){return this.attributes.get(name)??null;}
  matches(selector){return selector===':hover'&&this.hover;}
  contains(node){return node===this||this.children.some(child=>child.contains(node));}
  closest(selector){
    let node=this;
    while(node){
      if(selector==='[data-command]'&&node.dataset.command)return node;
      if(selector==='input,textarea,select,button,[contenteditable="true"]'&&['input','select','button'].includes(node.name))return node;
      node=node.parentElement;
    }
    return null;
  }
  focus(){const previous=this.ownerDocument.activeElement;if(previous===this)return;this.ownerDocument.activeElement=this;previous?.emit('focusout');this.emit('focusin');}
  blur(){if(this.ownerDocument.activeElement!==this)return;this.ownerDocument.activeElement=null;this.emit('focusout');}
}
function fixture(){
  const timers=new Map();let nextTimer=0;
  const shell=new FakeElement('section');
  const document={activeElement:null,hidden:false,fullscreenElement:null,handlers:new Map(),createElement:name=>name==='section'?shell:new FakeElement(name),addEventListener(type,fn){const rows=this.handlers.get(type)||[];rows.push(fn);this.handlers.set(type,rows);}};
  const window={handlers:new Map(),addEventListener(type,fn){const rows=this.handlers.get(type)||[];rows.push(fn);this.handlers.set(type,rows);}};
  const parent={replaceChildren(node){this.child=node;}};
  const nodes=new Map();
  const screen=new FakeElement('div',['video-screen']),video=new FakeElement('video'),message=new FakeElement('div'),hitArea=new FakeElement('div',['video-control-hit-area']),controls=new FakeElement('div',['video-controls']);
  const seekbar=new FakeElement('input'),playButton=new FakeElement('button'),backButton=new FakeElement('button'),forwardButton=new FakeElement('button'),muteButton=new FakeElement('button'),volume=new FakeElement('input'),rate=new FakeElement('select'),loopButton=new FakeElement('button'),fullscreenButton=new FakeElement('button'),time=new FakeElement('output');
  seekbar.setAttribute('aria-keyshortcuts','ArrowLeft ArrowRight Shift+ArrowLeft Shift+ArrowRight Home End PageUp PageDown');
  for(const [node,command] of [[playButton,'play'],[backButton,'back'],[forwardButton,'forward'],[muteButton,'mute'],[loopButton,'loop'],[fullscreenButton,'fullscreen']])node.dataset.command=command;
  const transport=new FakeElement('div'),utility=new FakeElement('div'),spacer=new FakeElement('span');transport.append(playButton,backButton,forwardButton,time);utility.append(muteButton,volume,rate,loopButton,fullscreenButton);controls.append(seekbar,transport,spacer,utility);screen.append(video,message,hitArea,controls);shell.append(screen);
  video.paused=true;video.duration=120;video.currentTime=10;video.volume=1;video.muted=false;video.playbackRate=1;video.loop=false;video.playCalls=0;video.src='';
  video.play=async()=>{video.playCalls++;video.paused=false;video.emit('play');video.emit('playing');};video.pause=()=>{video.paused=true;video.emit('pause');};video.load=()=>{};video.setAttribute=FakeElement.prototype.setAttribute;video.removeAttribute=name=>{video.attributes.delete(name);};
  screen.requestFullscreen=async()=>{};
  for(const node of [shell,screen,video,message,hitArea,controls,seekbar,playButton,backButton,forwardButton,muteButton,volume,rate,loopButton,fullscreenButton,time]){node.ownerDocument=document;}
  shell.querySelector=selector=>{
    if(selector==='video')return video;
    if(selector==='[data-command="play"]')return playButton;
    if(selector==='[data-command="mute"]')return muteButton;
    if(selector==='[data-command="loop"]')return loopButton;
    return nodes.get(selector)||null;
  };
  for(const [selector,node] of [['.video-screen',screen],['.video-message',message],['.video-control-hit-area',hitArea],['.video-controls',controls],['.video-seek',seekbar],['.video-transport',transport],['.video-utility',utility],['.video-volume',volume],['.video-rate',rate],['.video-time',time]])nodes.set(selector,node);
  const context=vm.createContext({window,document,AbortController,clearTimeout:id=>timers.delete(id),setTimeout:fn=>{const id=++nextTimer;timers.set(id,fn);return id;}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../frontend/video-player.js'),'utf8'),context);
  const player=window.YingXuVideo.mount(parent,{name:'synthetic fixture',src:'/media'});
  return {player,nodes:{screen,video,message,hitArea,controls,seekbar,playButton,backButton,forwardButton,muteButton,volume,rate,loopButton,fullscreenButton,time},document,timers,fireTimer(){const entry=[...timers.entries()].at(-1);assert.ok(entry,'expected an auto-hide timer');timers.delete(entry[0]);entry[1]();}};
}

test('a dedicated bottom hit area reveals hidden controls without toggling video or volume',async()=>{
  const f=fixture(),{video,screen,hitArea,controls}=f.nodes;
  await f.player.play();f.fireTimer();assert.equal(screen.classList.contains('controls-hidden'),true);
  hitArea.emit('pointerenter');assert.equal(screen.classList.contains('controls-hidden'),false);
  const plays=video.playCalls,volume=video.volume;
  hitArea.emit('click');
  assert.equal(video.playCalls,plays,'the reveal strip cannot start or toggle playback');
  assert.equal(video.volume,volume,'the reveal strip cannot alter volume');
  assert.equal(controls.matches(':hover'),false);
  const css=fs.readFileSync(path.join(__dirname,'../frontend/video-player.css'),'utf8');
  assert.match(css,/\.controls-hidden \.video-control-hit-area\{[^}]*pointer-events:auto/);
  assert.doesNotMatch(css,/\.controls-hidden\{cursor:none\}/,'the mouse pointer stays visible while controls are hidden');
});

test('control activation preserves focus and focused controls do not auto-hide',async()=>{
  const f=fixture(),{video,screen,controls,forwardButton,rate}=f.nodes;
  await f.player.play();f.fireTimer();assert.equal(screen.classList.contains('controls-hidden'),true);
  forwardButton.focus();assert.equal(screen.classList.contains('controls-hidden'),false);
  forwardButton.emit('click');assert.equal(f.document.activeElement,forwardButton,'delegated commands must not steal keyboard focus');
  assert.equal(video.currentTime,15);assert.equal(video.paused,false);
  f.fireTimer();assert.equal(screen.classList.contains('controls-hidden'),false,'focused transport controls remain visible');
  rate.focus();rate.value='1.5';rate.emit('change',{target:rate});assert.equal(video.playbackRate,1.5);
  f.fireTimer();assert.equal(screen.classList.contains('controls-hidden'),false,'focused menus remain visible');
  controls.hover=true;rate.blur();f.fireTimer();assert.equal(screen.classList.contains('controls-hidden'),false,'hovered controls remain visible');
});

test('the play-position slider supports accessible five- and thirty-second keyboard seeking',async()=>{
  const f=fixture(),{video,seekbar,screen}=f.nodes;
  video.currentTime=40;seekbar.value='40';
  const pausedPlays=video.playCalls;
  const first=seekbar.emit('keydown',{key:'ArrowRight'});
  assert.equal(first.defaultPrevented,true);assert.equal(video.currentTime,45);assert.equal(video.paused,true);assert.equal(video.playCalls,pausedPlays);
  const shifted=seekbar.emit('keydown',{key:'ArrowRight',shiftKey:true});assert.equal(shifted.defaultPrevented,true);assert.equal(video.currentTime,75);
  seekbar.emit('keydown',{key:'Home'});assert.equal(video.currentTime,0);
  seekbar.emit('keydown',{key:'End'});assert.equal(video.currentTime,120);
  assert.match(seekbar.getAttribute('aria-valuetext'),/00:02:00\.000/);
  assert.match(seekbar.getAttribute('aria-keyshortcuts'),/Shift\+ArrowRight/);
  video.currentTime=40;seekbar.value='40';await f.player.play();const playingPlays=video.playCalls;
  seekbar.emit('keydown',{key:'PageUp'});
  assert.equal(video.currentTime,70);assert.equal(video.paused,false);assert.equal(video.playCalls,playingPlays,'discrete keyboard seeking does not restart playback');
  seekbar.emit('keydown',{key:'End'});assert.equal(video.currentTime,120);assert.equal(video.playCalls,playingPlays,'End must not call play at the end and restart the video');
  assert.equal(screen.classList.contains('controls-hidden'),false);
});
