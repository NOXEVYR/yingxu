'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../frontend/hotkey-recorder.js'),'utf8');
class Node {
  constructor(){this.events=new Map();this.dataset={};this.attributes={};this.textContent='';this.value='Ctrl+Alt+Shift+S';this.checked=true;}
  addEventListener(name,fn){if(!this.events.has(name))this.events.set(name,new Set());this.events.get(name).add(fn);}
  removeEventListener(name,fn){this.events.get(name)?.delete(fn);}
  setAttribute(name,value){this.attributes[name]=value;}
  focus(){}
  fire(type,values={}){const e={type,preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;},...values};for(const fn of [...this.events.get(type)||[]])fn(e);return e;}
}
function setup(native=true){
  const window=new Node(),dialog=new Node(),input=new Node(),button=new Node(),status=new Node(),enabled=new Node(),messages=[],timers=new Map();let serial=0;
  vm.runInNewContext(source,{window,setTimeout:fn=>{timers.set(++serial,fn);return serial;},clearTimeout:id=>timers.delete(id)});
  const api=window.YingXuHotkeyRecorder;
  const ui=api.install({dialog,input,button,status,enabled,native,send:value=>messages.push(value)});
  const ack=extra=>ui.handle({action:'capture-hotkey-status',shortcut:'Ctrl+Alt+Shift+S',enabled:true,registered:true,recording:false,error:null,...extra});
  const begin=()=>{button.fire('click');if(native)ack({recording:true,registered:false});};
  return {window,dialog,input,button,status,enabled,messages,timers,ui,api,ack,begin};
}
test('physical keys are canonical under Chinese input and shifted number keys',()=>{
  const {api}=setup();
  assert.equal(api.combination({key:'s',code:'KeyS',ctrlKey:true,altKey:true}),'Ctrl+Alt+S');
  assert.equal(api.combination({key:'!',code:'Digit1',ctrlKey:true,shiftKey:true}),'Ctrl+Shift+1');
  assert.equal(api.combination({key:'F24',code:'F24',altKey:true,shiftKey:true}),'Alt+Shift+F24');
  for(const values of [{key:'F12',code:'F12',ctrlKey:true,altKey:true},{key:'s',code:'KeyS',ctrlKey:true},{key:'S',code:'KeyS',ctrlKey:true,altKey:true,metaKey:true},{key:'Process',code:'KeyS',ctrlKey:true,altKey:true},{key:'S',code:'KeyS',ctrlKey:true,altKey:true,isComposing:true},{key:'S',code:'KeyS',ctrlKey:true,altKey:true,getModifierState:()=>true}])assert.equal(api.combination(values),null);
});
test('recording waits for native suspension, shows held keys, records without saving',()=>{
  const s=setup();s.button.fire('click');assert.equal(s.messages[0].active,true);assert.match(s.button.textContent,/准备/);
  s.button.fire('keydown',{key:'A',code:'KeyA',ctrlKey:true,altKey:true});assert.equal(s.input.value,'Ctrl+Alt+Shift+S');
  s.ack({recording:true,registered:false});s.button.fire('keydown',{key:'Control',ctrlKey:true});assert.equal(s.button.textContent,'Ctrl + …');
  const e=s.button.fire('keydown',{key:'a',code:'KeyA',ctrlKey:true,altKey:true});assert.ok(e.prevented&&e.stopped);
  assert.equal(s.input.value,'Ctrl+Alt+A');assert.equal(s.button.textContent,'Ctrl + Alt + A');assert.match(s.status.textContent,/尚未保存/);assert.equal(s.messages.at(-1).active,false);
  s.ack();assert.match(s.status.textContent,/尚未保存/);
});
test('successful registration, conflict and stale native status are distinguished after saving',()=>{
  const s=setup();s.begin();s.button.fire('keydown',{key:'F8',code:'F8',ctrlKey:true,shiftKey:true});
  s.ui.saved({capture_hotkey:'Ctrl+Shift+F8',capture_enabled:true});assert.match(s.status.textContent,/等待系统确认/);
  s.ack();assert.match(s.status.textContent,/等待系统确认/);
  s.ack({shortcut:'Ctrl+Shift+F8',registered:false,error:'已被占用'});assert.equal(s.status.dataset.state,'error');assert.match(s.status.textContent,/未生效.*占用/);
  s.ack({shortcut:'Ctrl+Shift+F8'});assert.match(s.status.textContent,/已生效：Ctrl \+ Shift \+ F8/);
});
test('Escape, Tab, blur and window deactivation restore the original hotkey',()=>{
  for(const trigger of ['Escape','Tab','blur','window']){
    const s=setup();s.begin();
    if(trigger==='window')s.window.fire('blur');else if(trigger==='blur')s.button.fire('blur');else {const e=s.button.fire('keydown',{key:trigger});assert.equal(!!e.prevented,trigger==='Escape');}
    assert.equal(s.input.value,'Ctrl+Alt+Shift+S');assert.equal(s.messages.at(-1).active,false);assert.equal(s.ui.isRecording(),false);
  }
});
test('invalid and repeating keys never change the stored candidate',()=>{
  const s=setup();s.begin();s.button.fire('keydown',{key:'F12',code:'F12',ctrlKey:true,altKey:true});assert.match(s.status.textContent,/未接受/);
  s.button.fire('keydown',{key:'X',code:'KeyX',ctrlKey:true,altKey:true,repeat:true});assert.equal(s.input.value,'Ctrl+Alt+Shift+S');assert.equal(s.ui.isRecording(),true);
});
test('missing native acknowledgement times out, restores registration and cannot claim success',()=>{
  const s=setup();s.button.fire('click');[...s.timers.values()][0]();assert.match(s.status.textContent,/未收到/);assert.equal(s.messages.at(-1).active,false);assert.equal(s.ui.isRecording(),false);
});
test('a stale settings acknowledgement does not suppress confirmation timeout',()=>{
  const s=setup();s.ui.saved({capture_hotkey:'Ctrl+Alt+B',capture_enabled:true});s.ack();
  [...s.timers.values()].at(-1)();assert.match(s.status.textContent,/尚未收到系统确认/);assert.doesNotMatch(s.status.textContent,/已生效/);
});
test('closing/unloading disposes handlers and late messages cannot modify a replacement dialog',()=>{
  for(const type of ['close','pagehide']){
    const s=setup();s.begin();(type==='close'?s.dialog:s.window).fire(type);const text=s.status.textContent;
    s.ack({registered:false,error:'late'});s.button.fire('click');assert.equal(s.status.textContent,text);assert.equal(s.messages.at(-1).active,false);assert.equal(s.timers.size,0);
  }
});
test('disabled screenshot setting and unsupported native host never claim registration',()=>{
  const s=setup(false);assert.doesNotMatch(s.status.textContent,/已生效/);s.begin();s.button.fire('keydown',{key:'B',code:'KeyB',ctrlKey:true,altKey:true});assert.equal(s.messages.length,0);assert.match(s.status.textContent,/尚未保存/);
  s.ui.saved({capture_hotkey:'Ctrl+Alt+B',capture_enabled:false});assert.match(s.status.textContent,/已关闭/);
});
