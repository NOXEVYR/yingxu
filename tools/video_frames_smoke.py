"""Real HTTP + Chromium checks for decoded paused frames. No host simulation."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from server import Application,Server
from playwright.sync_api import sync_playwright

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media',type=Path,required=True);parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--browser',default='/usr/bin/chromium');args=parser.parse_args()
    output=args.evidence.resolve();output.mkdir(parents=True,exist_ok=True);rows=[]
    def check(name,ok,detail=None):
        rows.append(dict(name=name,ok=bool(ok),detail=detail));print(name,ok,detail,flush=True)
        if not ok:raise AssertionError(name)
    with tempfile.TemporaryDirectory(prefix='yingxu-frame-browser-') as temp:
        root=Path(temp).resolve()
        with patch('yingxu.skills.Path.home',return_value=root/'home'):app=Application(root/'data',root/'projects')
        files={}
        for name in ('cfr.mp4','vfr.mp4','offset.mp4','duplicate-pts.mkv','rotated.mp4'):
            files[name]=root/name;shutil.copyfile(args.media/name,files[name])
        original={name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in files.items()}
        entries={name:app.external.open({'paths':[str(path)]})['entries'][0]['id'] for name,path in files.items()}
        server=Server(('127.0.0.1',0),app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        errors=[]
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path=args.browser,headless=True,args=['--no-sandbox','--autoplay-policy=no-user-gesture-required'])
                page=browser.new_page(viewport={'width':1440,'height':1000});page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{server.server_port}');page.wait_for_function('() => !!state.bootstrap')
                def open_video(name):
                    page.evaluate('(id)=>openExternal(id,{workspace:true})',entries[name])
                    page.wait_for_selector('.video-frame-inspector',state='attached')
                    page.wait_for_function('() => !document.querySelector("[data-frame=start]").disabled')
                    if not page.locator('.video-review').evaluate('(n)=>n.open'):page.locator('.video-review summary').click()
                    page.wait_for_function('() => !document.querySelector("[data-review=mark]").disabled')
                def start():
                    if not page.locator('[data-frame=start]').is_disabled():page.locator('[data-frame=start]').click()
                    page.wait_for_function('() => !document.querySelector("[data-frame=enter]").disabled',timeout=30000)
                    page.locator('[data-frame=enter]').click()
                    page.wait_for_function('() => document.querySelector(".frame-overlay:not([hidden])") && !document.querySelector("[data-frame=locate]").disabled',timeout=30000)
                def locate(n):
                    page.locator('.frame-actions input').fill(str(n));page.locator('[data-frame=locate]').click()
                    page.wait_for_function('(n)=>document.querySelector(".frame-overlay-label")?.textContent.startsWith("解码静帧 "+n+" /") && !document.querySelector("[data-frame=locate]").disabled',arg=n,timeout=30000)
                def ordinal(n):
                    page.wait_for_function('(n)=>document.querySelector(".frame-overlay-label")?.textContent.startsWith("解码静帧 "+n+" /") && !document.querySelector("[data-frame=locate]").disabled',arg=n)
                open_video('vfr.mp4');page.evaluate('window.keptVideo=document.querySelector("video")')
                page.locator('[data-frame=start]').click()
                page.wait_for_function('() => !document.querySelector("[data-frame=enter]").disabled',timeout=30000)
                # Artificial gate for lifecycle only, never a performance measure.
                original_extract=app.video_frames._extract;entered_extract=threading.Event();release_extract=threading.Event()
                def held_extract(job,session):
                    entered_extract.set();release_extract.wait(8);original_extract(job,session)
                with patch.object(app.video_frames,'_extract',side_effect=held_extract):
                    page.locator('[data-frame=enter]').click();check('explicit inspection worker entered test gate',entered_extract.wait(3))
                    check('seek controls hidden before first decoded image arrives',page.locator('.video-controls').evaluate('(n)=>getComputedStyle(n).visibility==="hidden"'))
                    before=page.evaluate('keptVideo.currentTime');page.locator('.video-screen').focus();page.keyboard.press('ArrowRight')
                    check('seek hotkeys locked while first frame is pending',page.evaluate('(before)=>keptVideo.paused && keptVideo.currentTime===before',before))
                    release_extract.set();ordinal(0)
                locate(96)
                check('VFR real ordinal and PTS shown','原始 PTS 49152 × 1/12288' in page.locator('.frame-overlay-label').inner_text())
                page.locator('[data-frame=next]').click();ordinal(97)
                check('VFR next uses actual 125ms PTS delta','原始 PTS 50688' in page.locator('.frame-overlay-label').inner_text())
                check('static inspection preserves paused browser decoder',page.evaluate('keptVideo===document.querySelector("video") && keptVideo.paused'))
                page.locator('.video-screen').focus();page.keyboard.press(',');ordinal(96)
                page.keyboard.press(' ');page.wait_for_timeout(100)
                check('space cannot secretly play under still frame',page.evaluate('keptVideo.paused'))
                page.evaluate('keptVideo.play()');page.wait_for_timeout(100)
                check('external play request remains paused in inspection',page.evaluate('keptVideo.paused'))
                page.locator('.frame-actions input').focus();before=page.locator('.frame-overlay-label').inner_text();page.keyboard.press('ArrowRight')
                check('numeric input hotkey does not step frames',page.locator('.frame-overlay-label').inner_text()==before)
                before=page.evaluate('activeTab().videoReviewDirty||false');page.locator('[data-review=in]').click()
                check('review time actions blocked during still inspection',page.evaluate('activeTab().videoReviewDirty||false')==before)
                page.locator('[data-frame=next]').scroll_into_view_if_needed();page.screenshot(path=str(output/'10-vfr-paused-frame.png'))
                locate(93);page.locator('[data-frame=set-first]').click();locate(97)
                page.locator('[data-frame=set-last]').click()
                check('strict range explicitly inclusive and silent','两端包含' in page.locator('.frame-range').inner_text() and '不含音频' in page.locator('[data-frame=export]').inner_text())
                original_export=app.video_frames._export;entered_export=threading.Event();release_export=threading.Event();export_jobs=[]
                def held_export(job,session):
                    export_jobs.append(job);entered_export.set();release_export.wait(8);original_export(job,session)
                with patch.object(app.video_frames,'_export',side_effect=held_export):
                    page.locator('[data-frame=export]').click();check('export worker entered lifecycle test gate',entered_export.wait(3))
                    page.locator('[data-frame=resume]').click()
                    check('return during export resumes ordinary playback',page.evaluate('!keptVideo.paused') and not page.locator('.frame-overlay').is_visible())
                    check('return during export does not cancel task',not export_jobs[0]['cancel'].is_set())
                    release_export.set()
                    page.wait_for_function('() => document.querySelector(".frame-export-result a")',timeout=30000)
                    check('export completes and publishes after returning to playback',export_jobs[0]['state']=='done' and app.video_review.output(export_jobs[0]['id']).is_file())
                page.screenshot(path=str(output/'16-export-complete-playing.png'),full_page=True)
                locate(97)
                check('strict VFR export reports real verified NUT','NUT' in page.locator('.frame-export-result').inner_text())
                with page.expect_download() as download:
                    page.locator('.frame-export-result a').first.click()
                download.value.save_as(str(output/'strict-vfr.nut'))
                manifest=page.request.get(f'http://127.0.0.1:{server.server_port}'+page.locator('.frame-export-result a').nth(1).get_attribute('href')).json()
                check('downloaded source range receipt matches selection',manifest['first_inclusive']==93 and manifest['last_inclusive']==97 and manifest['count']==5 and manifest['audio']=='none')
                (output/'strict-vfr-frames.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
                page.locator('[data-frame=export]').scroll_into_view_if_needed();page.screenshot(path=str(output/'15-strict-vfr-export.png'),full_page=True)
                page.locator('.video-screen').focus();page.keyboard.press('f');page.wait_for_function('() => !!document.fullscreenElement')
                page.screenshot(path=str(output/'11-frame-fullscreen.png'));page.keyboard.press('Escape');page.wait_for_function('() => !document.fullscreenElement')
                check('decoded still supports browser fullscreen exit',page.locator('.frame-overlay').is_visible())
                page.locator('.frame-overlay>button').click();page.wait_for_timeout(150)
                check('return explicitly says approximate and remains paused','近似定位' in page.locator('.frame-status').inner_text() and page.evaluate('keptVideo.paused && Math.abs(keptVideo.currentTime-4.125)<.05'))
                check('return preserves ready index for repeated inspection',len(app.video_frames.sessions)==1 and not page.locator('[data-frame=enter]').is_disabled())
                start();page.locator('[data-frame=resume]').click();page.wait_for_timeout(150)
                check('explicit resume continues browser playback',page.evaluate('!keptVideo.paused'))
                open_video('duplicate-pts.mkv');start();locate(2)
                image_hash='async(n)=>{const c=document.createElement("canvas");c.width=n.naturalWidth;c.height=n.naturalHeight;const ctx=c.getContext("2d");ctx.drawImage(n,0,0);return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",ctx.getImageData(0,0,c.width,c.height).data))).join(",");}'
                label2=page.locator('.frame-overlay-label').inner_text();pixels2=page.locator('.frame-overlay img').evaluate(image_hash)
                locate(3);label3=page.locator('.frame-overlay-label').inner_text();pixels3=page.locator('.frame-overlay img').evaluate(image_hash)
                check('duplicate PTS retain separate display ordinals and pictures','原始 PTS 83' in label2 and '原始 PTS 83' in label3 and pixels2!=pixels3,[label2,label3])
                page.locator('[data-frame=next]').scroll_into_view_if_needed();page.screenshot(path=str(output/'12-duplicate-pts-frames.png'))
                open_video('rotated.mp4');start();locate(24)
                check('display rotation is applied to actual still',page.locator('.frame-overlay img').evaluate('(n)=>n.naturalWidth===360 && n.naturalHeight===640'))
                page.locator('[data-frame=next]').scroll_into_view_if_needed();page.screenshot(path=str(output/'13-rotation-frame.png'))
                open_video('offset.mp4');start();check('nonzero source PTS retained','原始 PTS 16896' in page.locator('.frame-overlay-label').inner_text())
                page.locator('[data-frame=cancel]').click();page.wait_for_function('() => !document.querySelector("[data-frame=start]").disabled')
                # Hold the real index worker at a cooperative boundary to reliably
                # exercise progress/cancel UI; never use its screenshot as speed proof.
                original_index=app.video_frames._index;entered=threading.Event();release=threading.Event()
                def held(job,session):
                    job['phase']='等待测试闸门（用于取消回归）';entered.set();release.wait(8);original_index(job,session)
                with patch.object(app.video_frames,'_index',side_effect=held):
                    page.locator('[data-frame=start]').click();check('index worker started',entered.wait(3))
                    page.wait_for_function('() => document.querySelector(".frame-status").textContent.includes("测试闸门")')
                    check('long index exposes progress and cancellation',page.locator('[data-frame=cancel]').is_visible())
                    page.locator('[data-frame=cancel]').click();release.set()
                    page.wait_for_timeout(400)
                    check('cancelled index never paints late frame',not page.locator('.frame-overlay').is_visible())
                start();path=files['offset.mp4'];path.write_bytes(path.read_bytes()+b'changed-test-copy')
                page.wait_for_function('() => document.querySelector(".frame-status").textContent.includes("改变")',timeout=5000)
                check('source change invalidates visible old still',not page.locator('.frame-overlay').is_visible())
                page.locator('[data-frame=start]').scroll_into_view_if_needed();page.screenshot(path=str(output/'14-source-change-invalidated.png'))
                for i in range(3):
                    open_video('cfr.mp4');start();page.evaluate('closeTab(state.activeKey)');page.wait_for_timeout(150)
                page.evaluate('async()=>{for(const tab of [...state.tabs])await closeTab(tab.key)}');page.wait_for_timeout(500)
                check('close/switch cancels every inspection session',not app.video_frames.sessions)
                check('source originals preserved',all(hashlib.sha256(path.read_bytes()).hexdigest()==original[name] for name,path in files.items() if name!='offset.mp4'))
                check('no uncaught browser errors',not errors,errors)
                browser.close()
        finally:
            server.shutdown();server.server_close();thread.join();app.close();(output/'frame-browser-results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
