"""Real long synthetic video playback during background indexing; no test gates.

Measures HTTP Range, browser event-loop, media clock and own FFmpeg processes.
OS caches are not cleared; this is observed cloud-browser behavior, not native
Windows or a guarantee for other CPU/storage/video sizes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from server import Application, Server
from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media',type=Path,required=True);parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--browser',default='/usr/bin/chromium');args=parser.parse_args()
    output=args.evidence.resolve();output.mkdir(parents=True,exist_ok=True)
    checks=[];processes=[];errors=[];report={'checks':checks,'processes':processes,'browser_errors':errors}
    original=hashlib.sha256(args.media.read_bytes()).hexdigest()
    def check(name,ok,detail=None):
        checks.append({'name':name,'ok':bool(ok),'detail':detail});print(name,ok,detail,flush=True)
        if not ok:raise AssertionError(name)
    real_popen=subprocess.Popen
    def observed(command,*a,**kw):
        process=real_popen(command,*a,**kw)
        if isinstance(command,list) and Path(command[0]).name in ('ffmpeg','ffprobe'):
            processes.append({'pid':process.pid,'command':command,'max_observed_threads':0,'max_observed_rss_kib':0})
        return process
    def sample_resources():
        for process in processes:
            try:
                values=dict(line.split(':',1) for line in Path(f"/proc/{process['pid']}/status").read_text().splitlines() if ':' in line)
                process['max_observed_threads']=max(process['max_observed_threads'],int(values.get('Threads','0')))
                process['max_observed_rss_kib']=max(process['max_observed_rss_kib'],int(values.get('VmRSS','0').split()[0]))
            except (OSError,ValueError):pass
    with tempfile.TemporaryDirectory(prefix='yingxu-frame-background-') as temporary:
        root=Path(temporary).resolve();media=root/'long-synthetic.mp4';shutil.copyfile(args.media,media)
        with patch('yingxu.skills.Path.home',return_value=root/'home'):app=Application(root/'data',root/'projects')
        server=Server(('127.0.0.1',0),app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            iid=app.external.open({'paths':[str(media)]})['entries'][0]['id']
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path=args.browser,headless=True,args=['--no-sandbox','--autoplay-policy=no-user-gesture-required'])
                page=browser.new_page(viewport={'width':1440,'height':1100});page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{server.server_port}');page.wait_for_function('() => !!state.bootstrap')
                page.evaluate('(id)=>openExternal(id,{workspace:true})',iid)
                page.wait_for_selector('[data-frame=start]',state='attached');page.locator('.video-review summary').click()
                page.wait_for_function('() => !document.querySelector("[data-frame=start]").disabled && !document.querySelector("[data-review=mark]").disabled')
                page.evaluate('''async()=>{window.bgVideo=document.querySelector('video');bgVideo.muted=true;await bgVideo.play();
                    window.bgSamples=[];window.bgLast=performance.now();window.bgTimer=setInterval(()=>{const n=performance.now();bgSamples.push(n-bgLast);bgLast=n},100)}''')
                page.wait_for_timeout(600)
                sample_js='''async()=>{const began=performance.now();const response=await fetch(bgVideo.currentSrc,{headers:{Range:'bytes=1048576-1114111'}});const bytes=await response.arrayBuffer();return {wall:performance.now(),media:bgVideo.currentTime,paused:bgVideo.paused,range_ms:performance.now()-began,range_status:response.status,range_bytes:bytes.byteLength,quality:{total:bgVideo.getVideoPlaybackQuality().totalVideoFrames,dropped:bgVideo.getVideoPlaybackQuality().droppedVideoFrames}}}'''
                report['baseline_samples']=[page.evaluate(sample_js) for _ in range(3)]
                page.locator('[data-review=a]').click();page.locator('[data-command=forward]').click();page.locator('[data-review=b]').click();page.locator('[data-review=ab]').click()
                with patch('yingxu.video_frames.subprocess.Popen',side_effect=observed):
                    start=time.monotonic();page.locator('[data-frame=start]').click()
                    check('background start does not pause playback',page.evaluate('!bgVideo.paused'))
                    check('background indexing preserves active AB loop',page.locator('[data-review=ab]').get_attribute('aria-pressed')=='true')
                    page.locator('[data-review=ab]').click()
                    check('background start does not display still overlay',not page.locator('.frame-overlay').is_visible())
                    page.locator('[data-command=play]').click();check('pause remains usable while indexing',page.evaluate('bgVideo.paused'))
                    page.locator('[data-command=play]').click();check('play remains usable while indexing',page.evaluate('!bgVideo.paused'))
                    page.locator('.video-rate').select_option('1.25');check('speed remains usable while indexing',page.evaluate('bgVideo.playbackRate===1.25'))
                    page.locator('.video-rate').select_option('1')
                    before=page.evaluate('bgVideo.currentTime');page.locator('[data-command=forward]').click()
                    check('seek remains usable while indexing',page.evaluate('bgVideo.currentTime')>=before+4.9)
                    page.locator('[data-review=a]').click();page.locator('[data-review=save]').click()
                    page.wait_for_function('() => !activeTab().videoReviewDirty')
                    check('review record controls remain usable while indexing',page.evaluate('activeTab().videoReviewDraft.a!==null'))
                    page.locator('[data-frame=start]').scroll_into_view_if_needed();page.screenshot(path=str(output/'00-background-index-playing.png'),full_page=True)
                    page.evaluate('bgSamples=[];bgLast=performance.now()')
                    samples=[]
                    while page.locator('[data-frame=enter]').is_disabled():
                        assert time.monotonic()-start<90
                        sample_resources();samples.append(page.evaluate(sample_js));page.wait_for_timeout(200)
                    sample_resources();report['index_seconds']=time.monotonic()-start;report['background_samples']=samples
                    report['event_loop_intervals_ms']=page.evaluate('bgSamples');page.evaluate('clearInterval(bgTimer)')
                    check('all Range probes return correct partial payload',all(s['range_status']==206 and s['range_bytes']==65536 for s in samples),len(samples))
                    check('media clock advances while index runs',len(samples)>=3 and samples[-1]['media']-samples[0]['media']>1)
                    check('index completion does not pause or paint',page.evaluate('!bgVideo.paused') and not page.locator('.frame-overlay').is_visible())
                    report['media_clock_advance_seconds']=samples[-1]['media']-samples[0]['media']
                    report['sample_window_seconds']=(samples[-1]['wall']-samples[0]['wall'])/1000
                    report['clock_ratio']=report['media_clock_advance_seconds']/report['sample_window_seconds']
                    quality_first=samples[0]['quality'];quality_last=samples[-1]['quality']
                    report['decoded_frames_during_samples']=quality_last['total']-quality_first['total']
                    report['dropped_frames_during_samples']=quality_last['dropped']-quality_first['dropped']
                    check('media clock tracks wall time at normal speed',.95<=report['clock_ratio']<=1.05,report['clock_ratio'])
                    check('video frames continue to be presented while indexing',report['decoded_frames_during_samples']-report['dropped_frames_during_samples']>report['sample_window_seconds']*12)
                    check('Range probes stay below one second in this run',max(s['range_ms'] for s in samples)<1000)
                    check('browser event loop stays below one second in this run',max(report['event_loop_intervals_ms'])<1000)
                    page.screenshot(path=str(output/'01-background-ready-playing.png'),full_page=True)
                    page.locator('[data-frame=enter]').click();page.wait_for_selector('.frame-overlay:not([hidden])')
                    check('only explicit inspection pauses video',page.evaluate('bgVideo.paused'))
                    page.evaluate('bgVideo.play()');page.wait_for_timeout(150)
                    check('play is guarded during static inspection',page.evaluate('bgVideo.paused'))
                    page.locator('[data-frame=cancel]').click();page.wait_for_function('() => !document.querySelector("[data-frame=start]").disabled')
                    page.locator('[data-frame=start]').click()
                    page.wait_for_function('() => !document.querySelector("[data-frame=enter]").disabled',timeout=90000)
                    check('index completion preserves user pause',page.evaluate('bgVideo.paused') and not page.locator('.frame-overlay').is_visible())
                    page.locator('[data-frame=cancel]').click();page.evaluate('bgVideo.play()')
                    page.locator('[data-frame=start]').click();page.wait_for_timeout(150)
                    page.locator('[data-frame=cancel]').click();page.locator('[data-frame=start]').click()
                    page.wait_for_function('() => !document.querySelector("[data-frame=enter]").disabled',timeout=90000)
                    check('cancel then immediate reentry leaves one ready session',len(app.video_frames.sessions)==1)
                    check('cancel/reentry does not pause or paint stale frame',page.evaluate('!bgVideo.paused') and not page.locator('.frame-overlay').is_visible())
                    page.locator('.frame-actions input').fill('7100');page.locator('[data-frame=locate]').click()
                    page.wait_for_function('() => document.querySelector(".frame-overlay-label")?.textContent.startsWith("解码静帧 7100 /")',timeout=30000)
                    page.screenshot(path=str(output/'02-explicit-tail-inspection.png'),full_page=True)
                    page.locator('[data-frame=cancel]').click();page.locator('[data-frame=start]').click()
                    page.evaluate('closeTab(state.activeKey)');page.wait_for_timeout(500)
                    deadline=time.monotonic()+5
                    while app.video_frames.sessions and time.monotonic()<deadline:page.wait_for_timeout(100)
                    check('tab close cancels background work and releases sessions',not app.video_frames.sessions)
                    page.evaluate('(id)=>openExternal(id,{workspace:true})',iid)
                    page.wait_for_selector('[data-frame=start]',state='attached')
                    if not page.locator('.video-review').evaluate('(n)=>n.open'):page.locator('.video-review summary').click()
                    page.wait_for_function('() => !document.querySelector("[data-frame=start]").disabled')
                    page.evaluate("async()=>{window.bgVideo=document.querySelector('video');bgVideo.muted=true;await bgVideo.play()}")
                    page.locator('[data-frame=start]').click();page.wait_for_timeout(150)
                    media.touch()
                    page.wait_for_function('() => !document.querySelector("[data-frame=start]").disabled && document.querySelector(".frame-status").textContent.includes("改变")',timeout=10000)
                    check('source change invalidates background index without pausing playback',page.evaluate('!bgVideo.paused') and not app.video_frames.sessions)
                    page.evaluate('closeTab(state.activeKey)')
                check('no uncaught browser errors',not errors,errors)
                for process in processes:
                    command=process['command']
                    if '-version' in command:continue
                    check('decoder thread limit is explicit',command[1:3]==['-threads','1'],Path(command[0]).name)
                browser.close()
        finally:
            server.shutdown();server.server_close();thread.join();app.close()
            report['source_sha256']=original;report['scope']='real 5-minute synthetic media; browser only; no artificial gates; OS caches not flushed'
            (output/'background-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    assert hashlib.sha256(args.media.read_bytes()).hexdigest()==original


if __name__=='__main__':main()
