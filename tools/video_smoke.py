import sys, tempfile, threading, json, hashlib, time
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from server import Application,Server
from playwright.sync_api import sync_playwright
import argparse
parser=argparse.ArgumentParser(description='Real synthetic video browser smoke; requires Playwright and an existing Chromium.')
parser.add_argument('--media',type=Path,required=True);parser.add_argument('--evidence',type=Path,required=True);parser.add_argument('--browser',default='/usr/bin/chromium')
args=parser.parse_args();root=ROOT;evidence=args.evidence.resolve();media=args.media.resolve();evidence.mkdir(parents=True,exist_ok=True)
rows=[]
def check(name,ok,detail=None):
    rows.append(dict(name=name,ok=bool(ok),detail=detail));print(name,ok,detail,flush=True)
    if not ok:raise AssertionError(name)
with tempfile.TemporaryDirectory(prefix='yingxu-browser-video-') as temp:
    fixture=Path(temp).resolve()
    with patch('yingxu.skills.Path.home',return_value=fixture/'home'):app=Application(fixture/'data',fixture/'projects')
    server=Server(('127.0.0.1',0),app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    entries={p.stem:app.external.open({'paths':[str(p)]})['entries'][0] for p in media.iterdir() if p.suffix in ('.mp4','.webm','.avi')}
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=args.browser,headless=True,args=['--no-sandbox','--autoplay-policy=no-user-gesture-required'])
            page=browser.new_page(viewport={'width':1280,'height':800});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(base);page.wait_for_function('() => typeof state!=="undefined" && !!state.bootstrap')
            page.evaluate('(id)=>openExternal(id,{workspace:true})',entries['cfr']['id'])
            page.wait_for_function('() => document.querySelector("#editorContent video")?.readyState>=2')
            info=page.evaluate('()=>{window.originalVideo=document.querySelector("#editorContent video");return {w:originalVideo.videoWidth,h:originalVideo.videoHeight,d:originalVideo.duration}}')
            check('real H264 picture decoded',info['w']==640 and info['h']==360,info)
            # The main workbench can give the player a narrow pane on a wide screen.
            check('narrow player retains every control within its viewport',page.evaluate('() => {const s=document.querySelector(".video-screen"),old=s.style.width;s.style.width="520px";const box=s.getBoundingClientRect();const ok=[...s.querySelectorAll(".video-buttons button,.video-buttons select,.video-buttons input")].every(n=>{const r=n.getBoundingClientRect();return r.right<=box.right+1&&r.left>=box.left-1});s.style.width=old;return ok;}'))
            page.evaluate('originalVideo.play()');page.wait_for_timeout(450)
            before=page.evaluate('originalVideo.currentTime');page.evaluate('renderEditorBody(activeTab())');after=page.evaluate('({same:originalVideo===document.querySelector("#editorContent video"),t:originalVideo.currentTime,paused:originalVideo.paused})')
            check('same-video rerender preserves decoder and playback',after['same'] and not after['paused'] and after['t']>=before,after)
            # Autohide works while the stage, rather than a control, has focus.
            page.locator('.video-screen').focus();page.mouse.move(5,5);page.wait_for_timeout(2400)
            check('controls hide during playback',page.locator('.video-screen').evaluate('(n)=>n.classList.contains("controls-hidden")'))
            page.locator('.video-screen').press(' ');check('space pauses',page.evaluate('originalVideo.paused'))
            page.locator('.video-screen').press('ArrowRight');page.wait_for_timeout(200);check('keyboard seek reaches later picture',page.evaluate('originalVideo.currentTime')>5)
            page.locator('.video-screen').press('Home');page.locator('.video-screen').press('m');check('mute shortcut',page.evaluate('originalVideo.muted'))
            page.locator('.video-rate').select_option('1.5');check('rate control',page.evaluate('originalVideo.playbackRate')==1.5)
            # Pointer scrubbing must restore the previous playing/paused state.
            slider=page.locator('.video-seek');box=slider.bounding_box()
            for playing in [False,True]:
                page.evaluate('(playing)=>playing?originalVideo.play():originalVideo.pause()',playing)
                page.mouse.move(box['x']+box['width']*.2,box['y']+box['height']/2);page.mouse.down();page.mouse.move(box['x']+box['width']*.5,box['y']+box['height']/2,steps=4);page.mouse.up();page.wait_for_timeout(120)
                check('scrub restores '+('playing' if playing else 'paused'),page.evaluate('originalVideo.paused') != playing)
            page.evaluate('originalVideo.pause()');page.locator('.video-screen').focus();page.locator('.video-screen').press('f');page.wait_for_function('() => !!document.fullscreenElement')
            check('browser fullscreen contains video and controls only',page.evaluate('document.fullscreenElement.className.includes("video-screen") && !document.fullscreenElement.querySelector(".video-review")'))
            page.screenshot(path=str(evidence/'video-fullscreen-browser.png'))
            page.keyboard.press('Escape');page.wait_for_function('() => !document.fullscreenElement')
            check('browser fullscreen exits',page.viewport_size=={'width':1280,'height':800})
            # Native handoff message shape, exercised against the actual app queue.
            for playing in (False,True):
                page.evaluate('(args)=>handleDesktopMessage({action:"external-open",workspace:true,entries:[{id:args.id}],playback:{time:1.25,playing:args.playing,volume:.4,muted:false,rate:1.25,loop:false}})',{'id':entries['cfr']['id'],'playing':playing})
                page.wait_for_timeout(100)
                handoff=page.evaluate('()=>{const v=document.querySelector("#editorContent video");return {t:v.currentTime,paused:v.paused,volume:v.volume,rate:v.playbackRate}}')
                check('handoff restores '+('playing' if playing else 'paused')+' state and position',1.2<=handoff['t']<2 and handoff['paused']!=playing and handoff['volume']==.4 and handoff['rate']==1.25,handoff)
            page.evaluate('originalVideo.pause()')
            page.locator('.video-review summary').click();page.wait_for_function('() => !document.querySelector("[data-review=mark]").disabled')
            page.evaluate('originalVideo.currentTime=1');page.locator('[data-review=in]').click();page.locator('[data-review=a]').click()
            page.evaluate('originalVideo.currentTime=2.5');page.locator('[data-review=out]').click();page.locator('[data-review=b]').click()
            page.locator('.video-marker-note').fill('Marker: keyboard safe');t=page.evaluate('originalVideo.currentTime');page.locator('.video-marker-note').press('ArrowLeft');page.locator('.video-marker-note').press('Space');check('input hotkeys do not seek or play',page.evaluate('originalVideo.currentTime')==t and page.evaluate('originalVideo.paused'))
            page.locator('[data-review=mark]').click();page.locator('[data-review=save]').click();page.wait_for_function('() => !activeTab().videoReviewDirty && !activeTab().videoReviewSaving');check('review sidecar saves',len(app.video_review.get('external',entries['cfr']['id'])['review']['markers'])==1)
            page.locator('[data-review=ab]').click();page.evaluate('originalVideo.currentTime=2.49; originalVideo.play()');page.wait_for_timeout(300);check('AB loop returns inside interval',page.evaluate('originalVideo.currentTime')<2)
            page.evaluate('originalVideo.pause()');page.screenshot(path=str(evidence/'video-workbench-review.png'))
            page.locator('[data-review=export]').click();page.wait_for_function('() => document.querySelector(".video-job a")',timeout=30000)
            check('workbench export produces download',page.locator('.video-job a').get_attribute('href').endswith('/file'))
            # Exercise the real single-tab close dialog and partial batch cancellation.
            page.locator('[data-review=mark]').click()
            page.evaluate('() => {window.reviewTab=activeTab();closeTab(reviewTab.key);}');page.locator('[data-choice=cancel]').click()
            check('single-tab close cancel keeps unsaved review',page.evaluate('tabStillOpen(reviewTab) && reviewTab.videoReviewDirty && reviewTab.videoReviewDraft.markers.length===2'))
            page.evaluate('() => {window.guardTab={key:"guard-only",item:{name:"synthetic later tab",kind:"text"},dirty:true,content:{editable:false}};state.tabs.push(guardTab);window.batchResult=null;prepareTabs([reviewTab,guardTab]).then(value=>window.batchResult=value).catch(error=>{console.error(error);window.batchResult=String(error);});}')
            page.evaluate('document.querySelector("#appDialog").dispatchEvent(new Event("close"))')
            check('stale dialog close event leaves current prompt pending',page.evaluate('window.batchResult===null && document.querySelector("#appDialog").open'))
            page.locator('[data-choice=discard]').click();page.locator('[data-choice=cancel]').click();page.wait_for_function('() => window.batchResult===false')
            check('discard then later cancel restores saved visible review',page.evaluate('tabStillOpen(reviewTab) && !reviewTab.videoReviewDirty && reviewTab.videoReviewDraft.markers.length===1 && document.querySelectorAll(".video-markers button").length===2'))
            page.evaluate('state.tabs=state.tabs.filter(tab=>tab!==guardTab)');page.locator('[data-review=mark]').click()
            page.evaluate('() => {closeTab(reviewTab.key);}');page.locator('[data-choice=save]').click();page.wait_for_function('() => !tabStillOpen(reviewTab)')
            check('single-tab close saves review before removing tab',len(app.video_review.get('external',entries['cfr']['id'])['review']['markers'])==2)
            # Observe decoded video PTS against the HTML media clock and actual audio decode.
            for sample in ['cfr','vfr']:
                page.evaluate('(id)=>openExternal(id,{workspace:true})',entries[sample]['id'])
                page.wait_for_function('() => document.querySelector("#editorContent video")?.readyState>=2')
                pts=page.evaluate("""async () => {
                  const v=document.querySelector('#editorContent video');v.pause();v.loop=false;v.playbackRate=1;v.currentTime=3.5;
                  await new Promise(resolve=>v.addEventListener('seeked',resolve,{once:true}));
                  const observed=[];let running=true;
                  const frame=(_,m)=>{observed.push({pts:m.mediaTime,clock:v.currentTime});if(running)v.requestVideoFrameCallback(frame);};
                  v.requestVideoFrameCallback(frame);await v.play();await new Promise(r=>setTimeout(r,1200));running=false;v.pause();
                  return {observed,audioBytes:v.webkitAudioDecodedByteCount||0,quality:v.getVideoPlaybackQuality().droppedVideoFrames};
                }""")
                expected=json.loads((media/(sample+'-pts.json')).read_text())['frames']
                expected=[float(frame['best_effort_timestamp_time']) for frame in expected]
                check(sample+' presented timestamps match fixture PTS',len(pts['observed'])>=8 and all(min(abs(frame['pts']-stamp) for stamp in expected)<.002 for frame in pts['observed']),pts)
                check(sample+' video presentation tracks media clock',max(abs(frame['pts']-frame['clock']) for frame in pts['observed'])<.25)
                check(sample+' audio track is decoded',pts['audioBytes']>0)
            for name in ['vfr','vp9','mpeg4']:
                old=page.evaluate('originalVideo.currentSrc');page.evaluate('(id)=>openExternal(id,{workspace:true})',entries[name]['id'])
                page.wait_for_function('() => (()=>{const v=document.querySelector("#editorContent video");return v && (v.readyState>=2 || v.error)})()')
                detail=page.evaluate('()=>{const v=document.querySelector("#editorContent video");return {w:v.videoWidth,h:v.videoHeight,error:v.error?.code||null,d:Number.isFinite(v.duration)?v.duration:null,oldReleased:originalVideo.getAttribute("src")===null}}')
                check(name+' format observed with visible picture or explicit error',detail['w']>0 or detail['error'],detail)
                check(name+' switch releases previous source',detail['oldReleased'])
                if detail['w']:
                    page.evaluate('document.querySelector("#editorContent video").currentTime=1.25');page.wait_for_timeout(180)
                    check(name+' seek decodes picture',page.evaluate('document.querySelector("#editorContent video").readyState>=2'))
            for i in range(6):
                page.evaluate('(id)=>openExternal(id,{workspace:true})',entries['cfr']['id']);page.wait_for_function('() => document.querySelector("#editorContent video")?.readyState>=2');page.evaluate('closeTab(state.activeKey)')
            page.evaluate('async () => {for(const tab of [...state.tabs])await closeTab(tab.key)}')
            check('repeated open/close leaves no active decoder',page.locator('#editorContent video').count()==0)
            # Independent quick HTML with the real page/component code, emulated host message only.
            html=(root/'frontend/quick-video.html').read_text().replace('/*VIDEO_STYLE*/',(root/'frontend/video-player.css').read_text()).replace('/*VIDEO_SCRIPTS*/',(root/'frontend/video-player.js').read_text()+'\n'+(root/'frontend/quick-video.js').read_text())
            quick=browser.new_page(viewport={'width':1040,'height':600});quick.add_init_script('window.chrome={webview:{addEventListener:(type,cb)=>window.hostReceive=cb}}')
            quick.route(base+'/quick/index.html',lambda route:route.fulfill(status=200,body=html,content_type='text/html'))
            quick.route(base+'/quick/media',lambda route:route.fulfill(path=str(media/'cfr.mp4'),content_type='video/mp4'))
            quick.goto(base+'/quick/index.html');quick.evaluate('(src)=>hostReceive({data:{action:"open-video",name:"CFR 24 fps synthetic",src}})',base+'/quick/media');quick.wait_for_function('() => document.querySelector("video")?.readyState>=2')
            check('independent quick page autoplays actual picture',quick.evaluate('!document.querySelector("video").paused && document.querySelector("video").videoWidth===640'))
            quick.evaluate('window.quickOriginal=document.querySelector("video")');quick.evaluate('(src)=>hostReceive({data:{action:"open-video",name:"again",src}})',base+'/quick/media');check('duplicate host open preserves video node',quick.evaluate('quickOriginal===document.querySelector("video")'))
            quick.evaluate('hostReceive({data:{action:"pause"}})');check('host pause pauses quick video',quick.evaluate('quickOriginal.paused'))
            quick.screenshot(path=str(evidence/'video-quick-browser.png'))
            check('quick mode has no advanced tools',quick.locator('.video-review').count()==0)
            check('no uncaught application errors',not errors,errors)
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join();app.close();(evidence/'browser-video-results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
