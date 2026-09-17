const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('frontend/app.js','utf8').replace(/boot\(\);\s*$/,'');
function fixture(){
  const result={textContent:'',innerHTML:''},dialog={open:true},calls=[],toasts=[];
  const nodes={'#updateCheckResult':result,'#appDialog':dialog};
  const context=vm.createContext({localStorage:{getItem:()=>null},window:{},document:{querySelector:s=>nodes[s]},
    fakeApi:async(path,options)=>{calls.push({path,options});return {current_version:'0.4.11',latest_version:'0.4.12',update_available:true,channel:'Windows 正式版',tag:'yingxu-v0.4.12',url:'https://github.com/turnsolesama/yingxu/releases/tag/yingxu-v0.4.12'};},
    fakeToast:(...args)=>toasts.push(args)});
  vm.runInContext(source+'\napi=(...args)=>fakeApi(...args);toast=fakeToast;globalThis.app={state,updateSettingsHtml,updateSettingsAction};',context);
  context.app.state.bootstrap={capabilities:{manual_update_check:true}};
  return {context,...context.app,result,dialog,calls,toasts,nodes};
}
(async()=>{
  const s=fixture(),button={disabled:false,dataset:{}};
  assert.equal(s.calls.length,0);assert.match(s.updateSettingsHtml(),/检查更新/);assert.equal(s.calls.length,0);
  await s.updateSettingsAction('check-update',button);
  assert.match(s.result.innerHTML,/发现新版本 0.4.12/);assert.equal(button.disabled,false);
  assert.equal(s.calls[0].path,'/api/updates/check');assert.equal(s.calls[0].options.method,'POST');
  await s.updateSettingsAction('open-update',{disabled:false,dataset:{tag:'yingxu-v0.4.12'}});
  assert.equal(s.calls[1].path,'/api/updates/open');assert.equal(s.calls[1].options.body.tag,'yingxu-v0.4.12');
  s.context.fakeApi=async()=>{throw Error('GitHub 请求受限');};
  await s.updateSettingsAction('check-update',button);assert.match(s.result.textContent,/未能检查更新.*受限/);
  let finish;s.context.fakeApi=()=>new Promise(resolve=>finish=resolve);
  const pending=s.updateSettingsAction('check-update',button);assert.equal(button.disabled,true);
  s.state.modalSequence++;s.nodes['#updateCheckResult']={innerHTML:'新弹窗'};
  finish({update_available:true,latest_version:'9.0.0'});await pending;
  assert.equal(s.nodes['#updateCheckResult'].innerHTML,'新弹窗');
  const closed=fixture();closed.dialog.open=false;await closed.updateSettingsAction('check-update',button);assert.equal(closed.calls.length,0);
  const old=fixture();old.state.bootstrap.capabilities={};assert.equal(old.updateSettingsHtml(),'');
  console.log('Manual update settings: no automatic call, results, failure, browser action and stale dialog guards passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
