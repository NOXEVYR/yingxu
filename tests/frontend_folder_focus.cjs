'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8').replace(/boot\(\);\s*$/,'');
function setup(native=true){
 const calls=[],messages=[],indicator={};let response={focus_folder:'C:\\synthetic',native_open:native,reveal_file:null},ok=true;
 const c=vm.createContext({window:{yingxuDesktopFocus:true,yingxuDesktopOpenFolder:native,chrome:{webview:{postMessage:m=>messages.push(m)}}},document:{querySelector:()=>indicator},localStorage:{getItem:()=>null},fetch:async(path,init)=>{calls.push({path,init});return {ok,status:ok?200:403,json:async()=>response};}});
 vm.runInContext(source+'\nglobalThis.app={state,api};',c);c.app.state.bootstrap={token:'fixture'};
 return {app:c.app,calls,messages,setResponse:value=>response=value,fail:()=>ok=false};
}
test('capable Windows host gets deferred registered folder open without mutating caller body',async()=>{
 const s=setup(),body=Object.freeze({project_id:'synthetic',category:'characters'});await s.app.api('/api/open-folder',{method:'POST',body});
 assert.equal(JSON.parse(s.calls[0].init.body).native_open,true);assert.equal(body.native_open,undefined);assert.deepEqual(JSON.parse(JSON.stringify(s.messages)),[{action:'focus-folder',path:'C:\\synthetic',open:true,select:null}]);
});
test('reveal passes returned registered file path to native host',async()=>{
 const s=setup();s.setResponse({focus_folder:'C:\\synthetic',native_open:true,reveal_file:'C:\\synthetic\\image.png'});await s.app.api('/api/open',{method:'POST',body:{id:'item',action:'reveal'}});assert.equal(JSON.parse(s.calls[0].init.body).native_open,true);assert.equal(s.messages[0].select,'C:\\synthetic\\image.png');
});
test('old host preserves backend opening and legacy focus message',async()=>{
 const s=setup(false);s.setResponse({focus_folder:'C:\\synthetic'});await s.app.api('/api/open-folder',{method:'POST',body:{project_id:'synthetic'}});assert.equal(JSON.parse(s.calls[0].init.body).native_open,undefined);assert.deepEqual(Object.keys(s.messages[0]).sort(),['action','path']);
});
test('ordinary file open does not defer launch; denied request never reaches native shell',async()=>{
 const s=setup();s.setResponse({ok:true});await s.app.api('/api/open',{method:'POST',body:{id:'item',action:'open'}});assert.equal(JSON.parse(s.calls[0].init.body).native_open,undefined);assert.equal(s.messages.length,0);
 s.fail();s.setResponse({error:'not registered'});await assert.rejects(s.app.api('/api/open-folder',{method:'POST',body:{project_id:'bad'}}),/not registered/);assert.equal(s.messages.length,0);
});
