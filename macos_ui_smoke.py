"""Hidden real WKWebView probe with temporary data, including dirty-draft close refusal."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback


def run():
    import webview
    from server import Application, Server, ROOT
    from macos_app import Desktop, stop_server
    from yingxu.runtime import ffmpeg_path
    from yingxu import __version__
    result={'ok':False,'checks':[],'real_user_data_used':False}
    with tempfile.TemporaryDirectory(prefix='yingxu-webkit-') as temporary:
        root=Path(temporary).resolve()
        os.environ.update(HOME=str(root/'user'),USERPROFILE=str(root/'user'))
        ffmpeg=ffmpeg_path()
        if not ffmpeg and not getattr(sys,'frozen',False):
            import imageio_ffmpeg
            ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
        if getattr(sys,'frozen',False):
            assert ffmpeg and Path(ffmpeg).resolve().is_relative_to(ROOT.resolve()), 'Frozen app must use its bundled FFmpeg'
        assert ffmpeg, 'macOS WKWebView smoke requires the packaged FFmpeg runtime'
        video=root/'macos-synthetic.mp4'
        subprocess.run([ffmpeg,'-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i',
            'testsrc2=size=640x360:rate=24:duration=8','-an','-c:v','libx264','-preset','ultrafast',
            '-crf','30','-pix_fmt','yuv420p','-threads','1','-movflags','+faststart','-y',str(video)],
            check=True,timeout=30,capture_output=True)
        assert video.is_file() and video.stat().st_size>1000, 'Synthetic H.264 fixture was not created'
        skill_files=[]
        for folder,name in [('.dsh/skills/demo','DSH 合成规范'),('.workbuddy/skills/demo','WorkBuddy 合成规范')]:
            skill=root/'user'/folder/'SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_text('---\nname: '+name+'\n---\n外部原稿保持只读。',encoding='utf-8')
            skill_files.append((skill,skill.read_bytes()))
        app=Application(root/'data',root/'projects')
        service=Server(('127.0.0.1',0),app)
        threading.Thread(target=service.serve_forever,daemon=True).start()
        host=Desktop(app,f'http://127.0.0.1:{service.server_port}')
        window=webview.create_window('映序隔离测试',host.origin+'/?desktop=macos',
                                    hidden=True,width=1280,height=800)
        host.window=window
        app.desktop_message=host.post_message
        window.events.closing+=host.closing
        def wait(predicate,timeout=15,label='WebKit check'):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                if predicate():return
                time.sleep(.05)
            raise AssertionError(label+' timed out')
        video_deadline=[None]
        def wait_video(predicate,timeout,label):
            remaining=video_deadline[0]-time.monotonic()
            if remaining<=0:raise AssertionError('Video WKWebView acceptance exceeded its 75-second budget before '+label)
            wait(predicate,min(timeout,remaining),label)
        def read_json(expression):
            value=window.run_js('JSON.stringify('+expression+')')
            if not isinstance(value,str):raise AssertionError('WebKit JSON probe did not return a string: '+repr(value))
            return json.loads(value)
        def exercise():
            try:
                wait(lambda:host.ready)
                assert window.run_js("window.yingxuMac === true && document.querySelector('#connectionState').textContent === '本地连接正常'")
                result['checks'].append('Frozen app loads local frontend in WKWebView')
                assert host.post_message({'action':'exit-response','allow':True},'wrong-token') is False
                window.run_js("(async () => { const p=await api('/api/projects',{method:'POST',body:{name:'WebKit合成项目'}}); await refreshProjects(); const n=await api('/api/items',{method:'POST',body:{project_id:p.id,category:'scripts',name:'合成笔记',content:'# 初始正文'}}); await openItem(n.id); return true; })()")
                wait(lambda:window.run_js('state.tabs.length === 1 && !state.tabs[0].loading'))
                assert window.run_js("Boolean(window.YingXuMarkdown || document.querySelector('#editorContent'))")
                result['checks'].append('Chinese project and document open in frozen WebKit')
                wait(lambda:window.run_js("Boolean(document.querySelector('.cm-content'))"))
                window.run_js("const editor=document.querySelector('.cm-content');editor.focus();editor.dispatchEvent(new KeyboardEvent('keydown',{key:'f',metaKey:true,bubbles:true,cancelable:true}));true")
                wait(lambda:window.run_js("Boolean(documentSearchUI?.isOpen())"))
                window.run_js("const query=document.querySelector('[data-document-query]');query.value='初始';query.dispatchEvent(new Event('input',{bubbles:true}));true")
                wait(lambda:window.run_js('documentSearchUI.getState().count === 1'))
                window.run_js('documentSearchUI.close(false);true')
                result['checks'].append('Command F searches Chinese Markdown body in real WKWebView')
                window.run_js("settingsDialog(); true")
                wait(lambda:window.run_js("document.querySelector('#dialogTitle').textContent === '设置'"))
                assert window.run_js("!document.querySelector('[name=capture_enabled]') && !document.querySelector('[name=close_to_tray]')")
                assert window.run_js("Boolean(document.querySelector('#previewMaintenance')) && document.querySelector('#appDialog').textContent.includes("+json.dumps(__version__)+")")
                window.run_js("document.querySelector('#maintenanceCache').checked=false;document.querySelector('#maintenanceVersions').checked=false;document.querySelector('#previewMaintenance').click();true")
                wait(lambda:window.run_js("Boolean(document.querySelector('.maintenance-table'))"))
                result['checks'].append('Current application version and maintenance preview remain available without Windows-only settings')
                window.run_js("document.querySelector('#closeDialog').click(); true")
                window.run_js("state.tabs[0].draft += '\\n未保存草稿'; state.tabs[0].dirty = true; true")
                assert host.closing() is False
                wait(lambda:window.run_js("document.querySelector('#appDialog').open"))
                assert not host.guard.allowed
                window.run_js("document.querySelector('#closeDialog').click(); true")
                wait(lambda:host.guard.request_id is None)
                assert window.run_js('state.tabs[0].dirty === true')
                result['checks'].append('Native close prompts for dirty draft; cancel preserves draft and window')
                window.run_js("(async () => { const n=await api('/api/items',{method:'POST',body:{project_id:state.tabs[0].item.project_id,category:'unclassified',name:'离线画板',format:'excalidraw'}}); await openItem(n.id); return true; })()")
                wait(lambda:window.run_js("Boolean(document.querySelector('iframe[title=\"Excalidraw 画板\"]')?.contentDocument?.querySelector('canvas'))"))
                scene=window.run_js("document.querySelector('iframe[title=\"Excalidraw 画板\"]').contentWindow.yingxuCanvas.getValue()")
                scene=json.loads(scene)
                assert scene['type']=='excalidraw' and scene['version']==2 and isinstance(scene['elements'],list)
                assert window.run_js('state.tabs[0].dirty === true')
                result['checks'].append('Offline Excalidraw mounts and serializes a portable scene in frozen WKWebView without losing another document draft')
                window.run_js("window.__macCanvasFrame=document.querySelector('iframe[title=\"Excalidraw 画板\"]');window.__macCanvasDocument=window.__macCanvasFrame.contentDocument;document.querySelectorAll('[data-tab]')[0].click();true")
                wait(lambda:window.run_js('window.__macCanvasFrame.hidden === true'))
                window.run_js("document.querySelectorAll('[data-tab]')[1].click();true")
                wait(lambda:window.run_js('window.__macCanvasFrame.hidden === false'))
                assert window.run_js("window.__macCanvasFrame === document.querySelector('iframe[title=\"Excalidraw 画板\"]') && window.__macCanvasDocument === window.__macCanvasFrame.contentDocument")
                result['checks'].append('Canvas uses the same iframe document across Markdown tab switches')
                window.run_js("(async () => { const n=await api('/api/items',{method:'POST',body:{project_id:state.tabs[0].item.project_id,category:'scripts',name:'合成思维导图',format:'xmind'}}); await openItem(n.id);return true; })()")
                wait(lambda:window.run_js("activeTab()?.item.kind === 'xmind' && Boolean(document.querySelector('.xmind-topic'))"))
                window.run_js("document.querySelector('.xmind-topic').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));const field=document.querySelector('.xmind-inline-editor');field.value='WebKit中文中心';field.dispatchEvent(new Event('input',{bubbles:true}));field.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));true")
                assert window.run_js("activeTab().dirty && JSON.parse(activeTab().draft)[0].rootTopic.title === 'WebKit中文中心'")
                window.run_js("document.querySelector('.xmind-tools button').click();true")
                assert window.run_js("document.querySelectorAll('.xmind-topic').length === 2")
                window.run_js("window.__macSaveDone=false;saveTab(activeTab()).then(ok=>{window.__macSaveDone=ok;});true")
                wait(lambda:window.run_js("window.__macSaveDone === true && activeTab().saved === true && !activeTab().dirty && !activeTab().saving && !state.loadingItems"))
                assert window.run_js("JSON.parse(activeTab().draft)[0].rootTopic.children.attached.length === 1")
                result['checks'].append('XMind creation, Chinese topic editing, child insertion and save in final frozen WKWebView')
                assert window.run_js("""(() => {
                    const stage = document.querySelector('.xmind-stage');
                    const key = (value, extra = {}) => stage.dispatchEvent(new KeyboardEvent('keydown', {key:value, bubbles:true, cancelable:true, ...extra}));
                    const button = label => document.querySelector(`.xmind-editor button[aria-label="${label}"]`) || [...document.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === label);
                    key('Escape');
                    const rootId = JSON.parse(activeTab().draft)[0].rootTopic.id;
                    document.querySelector(`.xmind-topic[data-topic-id="${rootId}"]`).click();
                    button('添加子主题 · Tab').click();
                    const inline = document.querySelector('.xmind-inline-editor');
                    inline.value = '批量分支';
                    inline.dispatchEvent(new Event('input', {bubbles:true}));
                    inline.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true, cancelable:true}));
                    const children = JSON.parse(activeTab().draft)[0].rootTopic.children.attached;
                    children.forEach((topic, index) => document.querySelector(`.xmind-topic[data-topic-id="${topic.id}"]`).dispatchEvent(new MouseEvent('click', {bubbles:true, metaKey:index > 0})));
                    button('显示或收起主题属性').click();
                    const labels = document.querySelector('[aria-label="主题标签"]');
                    labels.value = 'Mac验收';
                    labels.dispatchEvent(new Event('input', {bubbles:true}));
                    button('追加所选主题标签').click();
                    const priority = document.querySelector('[aria-label="优先级"]');
                    priority.value = 'priority-2';
                    priority.dispatchEvent(new Event('change', {bubbles:true}));
                    const progress = document.querySelector('[aria-label="任务进度"]');
                    progress.value = 'task-done';
                    progress.dispatchEvent(new Event('change', {bubbles:true}));
                    const changed = JSON.parse(activeTab().draft)[0].rootTopic.children.attached;
                    if (changed.length !== 2 || !changed.every(topic => topic.labels.includes('Mac验收') && topic.markers.some(m => m.markerId === 'priority-2') && topic.markers.some(m => m.markerId === 'task-done'))) return false;
                    const beforeDelete = activeTab().draft;
                    stage.focus(); key('Delete');
                    if ((JSON.parse(activeTab().draft)[0].rootTopic.children?.attached || []).length !== 0) return false;
                    key('z', {metaKey:true});
                    return activeTab().draft === beforeDelete;
                })()""")
                result['checks'].append('WKWebView Command multi-selection, batch labels/priority/progress, Delete and one-step undo')
                assert window.run_js("""(() => {
                    const stage = document.querySelector('.xmind-stage');
                    const before = activeTab().draft;
                    const camera = document.querySelector('.xmind-world');
                    const transform = camera.style.transform;
                    const rect = stage.getBoundingClientRect();
                    const wheel = new WheelEvent('wheel', {metaKey:true, deltaY:-100, clientX:rect.x + rect.width / 2, clientY:rect.y + rect.height / 2, bubbles:true, cancelable:true});
                    stage.dispatchEvent(wheel);
                    return wheel.defaultPrevented && camera.style.transform !== transform && activeTab().draft === before;
                })()""")
                result['checks'].append('WKWebView consumes synthetic Command wheel and changes only the mind-map viewport')
                assert window.run_js("""(() => {
                    // The preceding zoom can virtualize a branch outside the viewport.
                    document.querySelector('[aria-label="适应窗口 · Ctrl / ⌘ 0"]').click();
                    const topic = JSON.parse(activeTab().draft)[0].rootTopic.children.attached[0];
                    document.querySelector(`.xmind-topic[data-topic-id="${topic.id}"]`).click();
                    const input = document.querySelector('[aria-label="主题链接"]');
                    input.value = 'yingxu://item/' + state.tabs[0].item.id;
                    input.dispatchEvent(new Event('input', {bubbles:true}));
                    [...document.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === '应用链接到所选主题').click();
                    if (JSON.parse(activeTab().draft)[0].rootTopic.children.attached[0].href !== 'yingxu://item/' + state.tabs[0].item.id) return false;
                    window.__macXmindTabId = activeTab().id;
                    window.__macSaveDone = false;
                    saveTab(activeTab()).then(ok => { window.__macSaveDone = ok; });
                    return true;
                })()""")
                wait(lambda:window.run_js("window.__macSaveDone === true && activeTab().saved === true && !activeTab().dirty && !activeTab().saving && !state.loadingItems"))
                assert window.run_js("document.querySelector('[aria-label=\"适应窗口 · Ctrl / ⌘ 0\"]').click();document.querySelector('.xmind-link-mark').click();true")
                wait(lambda:window.run_js("activeTab().item.id === state.tabs[0].item.id"))
                assert window.run_js("state.tabs[0].dirty === true && state.tabs.some(tab => tab.id === window.__macXmindTabId && tab.item.kind === 'xmind')")
                result['checks'].append('Saved XMind file link opens the registered document while preserving its earlier draft and the map tab')

                # Exercise the shipped video workspace against a generated, isolated H.264 file.
                video_deadline[0]=time.monotonic()+75
                window.run_js("(async () => { const opened=await api('/api/external-open',{method:'POST',body:{paths:["+json.dumps(str(video))+ "]}}); window.__macVideoId=opened.entries[0].id; await openExternal(window.__macVideoId,{workspace:true}); window.__macVideoOpened=true; })()")
                wait_video(lambda:window.run_js("activeTab()?.item.kind === 'video' && !activeTab().loading && Boolean(document.querySelector('#editorContent video'))"),15,'Synthetic H.264 open')
                wait_video(lambda:window.run_js("(()=>{const v=document.querySelector('#editorContent video');return v?.readyState>=2 && Number.isFinite(v.duration)})()"),15,'WKWebView video decode')
                info=read_json("(()=>{const v=document.querySelector('#editorContent video');return {width:v.videoWidth,height:v.videoHeight,duration:v.duration,review:!!document.querySelector('#inspector .video-review'),timeline:!!document.querySelector('.video-trim-timeline')}})()")
                assert info['width']==640 and info['height']==360 and 7.8<=info['duration']<=8.2 and info['review'] and info['timeline'],info
                wait_video(lambda:window.run_js("document.querySelector('.review-status')?.textContent.includes('审片记录已载入')"),10,'Review sidecar load')
                result['checks'].append('WKWebView decodes isolated H.264 metadata and mounts docked video tools and trim timeline')

                window.run_js("window.__macPlayback=null;window.__macPlaybackError=null;(async()=>{const v=document.querySelector('#editorContent video');v.pause();v.loop=false;v.muted=true;v.playbackRate=1;v.currentTime=1;await new Promise(resolve=>{let done=false;const finish=()=>{if(!done){done=true;resolve();}};v.addEventListener('seeked',finish,{once:true});setTimeout(finish,1200);});const start=v.currentTime;await v.play();await new Promise(resolve=>setTimeout(resolve,650));v.pause();window.__macPlayback={start,end:v.currentTime,paused:v.paused,ready:v.readyState};})().catch(error=>{window.__macPlaybackError=String(error);});")
                wait_video(lambda:window.run_js("Boolean(window.__macPlayback)||Boolean(window.__macPlaybackError)"),5,'Playback position sample')
                assert window.run_js("window.__macPlaybackError===null"),window.run_js("String(window.__macPlaybackError)")
                playback=read_json("window.__macPlayback")
                assert playback['ready']>=2 and playback['end']>playback['start']+.18 and playback['paused'],playback
                result['checks'].append('Muted synthetic H.264 playback advances the real WKWebView media clock')

                assert window.run_js("(()=>{const names=['trim','markers','frames','file'];return names.every(name=>document.querySelector(`[data-review-tab=\"${name}\"]`)&&document.querySelector(`[data-review-panel=\"${name}\"]`))})()")
                window.run_js("document.querySelector('[data-review-tab=\"markers\"]').click();true")
                assert window.run_js("document.querySelector('[data-review-panel=\"markers\"]:not([hidden])')!==null && document.activeElement===document.querySelector('.video-marker-note')")
                window.run_js("document.querySelector('[data-review-tab=\"file\"]').click();true")
                assert window.run_js("(()=>{const p=document.querySelector('[data-review-panel=\"file\"]');return p&&!p.hidden&&p.textContent.includes('macos-synthetic.mp4')&&p.textContent.includes('本地文件')})()")
                window.run_js("document.querySelector('[data-review-tab=\"trim\"]').click();true")
                result['checks'].append('Synthetic WebKit DOM tab clicks reach the marker and file tools; no native mouse input is claimed')

                window.run_js("window.__macVideoRangeSetupDone=false;window.__macVideoRangeSetupError=null;(async()=>{const v=document.querySelector('#editorContent video'),seek=async time=>{v.currentTime=time;await new Promise(resolve=>{let done=false;const finish=()=>{if(!done){done=true;resolve();}};v.addEventListener('seeked',finish,{once:true});setTimeout(finish,1200);});};v.pause();await seek(1.1);document.querySelector('.video-trim-timeline [data-review=\"in\"]').click();await seek(3.2);document.querySelector('.video-trim-timeline [data-review=\"out\"]').click();await seek(2.2);document.querySelector('[data-review-tab=\"markers\"]').click();const note=document.querySelector('.video-marker-note');note.value='Mac WKWebView 合成审片标记';note.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-review=\"mark\"]').click();document.querySelector('[data-review-tab=\"trim\"]').click();window.__macVideoRangeSetupDone=true;})().catch(error=>{window.__macVideoRangeSetupError=String(error);window.__macVideoRangeSetupDone=true;});")
                wait_video(lambda:window.run_js("window.__macVideoRangeSetupDone===true"),8,'Review range and marker setup')
                assert window.run_js("window.__macVideoRangeSetupError===null"),window.run_js("String(window.__macVideoRangeSetupError)")
                draft=read_json("(()=>{const t=activeTab();return {dirty:t.videoReviewDirty,review:t.videoReviewDraft,videoTime:document.querySelector('#editorContent video').currentTime}})()")
                assert draft['dirty'] and 1.0<=draft['review']['in']<=1.3 and 3.1<=draft['review']['out']<=3.3 and len(draft['review']['markers'])==1 and draft['review']['markers'][0]['note']=='Mac WKWebView 合成审片标记',draft
                window.run_js("document.querySelector('[data-review=\"preview-range\"]').click();true")
                wait_video(lambda:window.run_js("(()=>{const v=document.querySelector('#editorContent video');return v?.paused&&document.querySelector('.trim-preview')?.textContent==='预览选中范围'})()"),8,'Trim preview end pause')
                preview=read_json("(()=>{const v=document.querySelector('#editorContent video'),r=activeTab().videoReviewDraft;return {paused:v.paused,time:v.currentTime,out:r.out,button:document.querySelector('.trim-preview').textContent}})()")
                assert preview['paused'] and abs(preview['time']-preview['out'])<=.18 and preview['button']=='预览选中范围',preview
                result['checks'].append('Synthetic WebKit DOM trim controls set in/out and preview pauses at the selected out point')

                # Layout changes must retain the actual media/tool nodes, dirty review and paused position.
                layout_snapshot=read_json("(()=>{const v=document.querySelector('#editorContent video');v.pause();window.__macVideoNode=v;window.__macReviewNode=document.querySelector('.video-review');window.__macTimelineNode=document.querySelector('.video-trim-timeline');return {time:v.currentTime,draft:activeTab().videoReviewDraft,dirty:activeTab().videoReviewDirty}})()")
                starting_layout=window.run_js("preference('workspace_layout')")
                if starting_layout=='focus':
                    window.run_js("document.querySelector('[data-video-mode=\"classic\"]').click();true")
                    wait_video(lambda:window.run_js("preference('workspace_layout')==='classic'"),8,'Browse layout switch')
                window.run_js("document.querySelector('[data-video-mode=\"focus\"]').click();true")
                wait_video(lambda:window.run_js("preference('workspace_layout')==='focus'"),8,'Focus layout switch')
                focused=read_json("(()=>{const v=document.querySelector('#editorContent video');return {same:v===window.__macVideoNode&&document.querySelector('.video-review')===window.__macReviewNode&&document.querySelector('.video-trim-timeline')===window.__macTimelineNode,time:v.currentTime,draft:activeTab().videoReviewDraft,dirty:activeTab().videoReviewDirty}})()")
                assert focused['same'] and focused['dirty'] and focused['draft']==layout_snapshot['draft'] and abs(focused['time']-layout_snapshot['time'])<.2,focused
                window.run_js("document.querySelector('[data-video-mode=\"classic\"]').click();true")
                wait_video(lambda:window.run_js("preference('workspace_layout')==='classic'"),8,'Browse layout restore')
                browsed=read_json("(()=>{const v=document.querySelector('#editorContent video');return {same:v===window.__macVideoNode&&document.querySelector('.video-review')===window.__macReviewNode&&document.querySelector('.video-trim-timeline')===window.__macTimelineNode,time:v.currentTime,draft:activeTab().videoReviewDraft,dirty:activeTab().videoReviewDirty}})()")
                assert browsed['same'] and browsed['dirty'] and browsed['draft']==layout_snapshot['draft'] and abs(browsed['time']-layout_snapshot['time'])<.2,browsed
                result['checks'].append('Browse/focus layout switches preserve the same WKWebView video/tool DOM, unsaved review draft and playback position')

                # Resize the actual hidden Cocoa window and inspect the responsive tools drawer at 900 CSS px.
                window.resize(900,700)
                wait_video(lambda:window.run_js("window.innerWidth>=880&&window.innerWidth<=920&&window.matchMedia('(max-width:1150px)').matches"),8,'900px Cocoa window resize')
                window.run_js("document.querySelector('#videoToolsToggle').click();true")
                wait_video(lambda:window.run_js("document.querySelector('#workspace').classList.contains('video-tools-open')"),4,'Video tools drawer open')
                drawer=read_json("(()=>{const ws=document.querySelector('#workspace'),ins=document.querySelector('#inspector'),toggle=document.querySelector('#videoToolsToggle'),r=ins.getBoundingClientRect(),w=ws.getBoundingClientRect(),style=getComputedStyle(ins);return {width:window.innerWidth,visible:style.display!=='none'&&r.width>0,position:style.position,drawerWidth:r.width,left:r.left,right:r.right,workspaceLeft:w.left,workspaceRight:w.right,toggleWidth:toggle.getBoundingClientRect().width,layout:matchMedia('(max-width:1150px)').matches}})()")
                assert drawer['layout'] and drawer['visible'] and drawer['position']=='absolute' and drawer['drawerWidth']<=331 and drawer['left']>=drawer['workspaceLeft']-1 and drawer['right']<=drawer['workspaceRight']+1 and drawer['toggleWidth']>0,drawer
                result['checks'].append('900px WKWebView layout opens a visible, bounded video-tools drawer')
                window.resize(1280,800)
                wait_video(lambda:window.run_js("window.innerWidth>=1200&&window.innerWidth<=1320"),8,'WKWebView window restore')
                window.run_js("document.querySelector('#videoToolsToggle').click();document.querySelector('[data-review-tab=\"trim\"]').click();true")

                window.run_js("document.querySelector('[data-review-tab=\"frames\"]').click();true")
                wait_video(lambda:window.run_js("document.querySelector('.frame-status')?.textContent.includes('Mac 暂未验收')"),8,'macOS frame capability response')
                frames=read_json("(()=>({status:document.querySelector('.frame-status').textContent,startHidden:document.querySelector('[data-frame=\"start\"]').hidden,startDisabled:document.querySelector('[data-frame=\"start\"]').disabled}))()")
                capability=app.video_frames.capability()
                assert capability['available'] is False and 'Mac 暂未验收' in capability['reason'] and frames['startHidden'] and frames['startDisabled'] and 'Mac 暂未验收' in frames['status'],{'capability':capability,'ui':frames}
                result['checks'].append('macOS frame inspection stays disabled and the WKWebView states the unverified capability honestly')

                window.run_js("document.querySelector('[data-review-tab=\"markers\"]').click();document.querySelector('[data-review-tab=\"trim\"]').click();document.querySelector('[data-review=\"save\"]').click();true")
                wait_video(lambda:window.run_js("!activeTab().videoReviewDirty&&!activeTab().videoReviewSaving&&document.querySelector('.review-status')?.textContent.includes('审片记录已保存')"),10,'Review save')
                video_id=window.run_js("activeTab().id")
                saved_review=app.video_review.get('external',video_id)['review']
                assert saved_review==draft['review'],{'saved':saved_review,'draft':draft['review']}
                window.run_js("(async()=>{window.__macVideoReviewReadback=await api('/api/video/review/external/'+activeTab().id);})()")
                wait_video(lambda:window.run_js("Boolean(window.__macVideoReviewReadback?.revision)"),8,'Review API readback')
                webkit_review=json.loads(window.run_js("JSON.stringify(window.__macVideoReviewReadback.review)"))
                assert webkit_review==saved_review,{'webkit':webkit_review,'saved':saved_review}
                result['checks'].append('审片记录保存到隔离 sidecar，WKWebView API 与后端回读均匹配草稿')

                window.run_js("document.querySelector('[data-review-tab=\"trim\"]').click();document.querySelector('[data-review=\"export\"]').click();true")
                wait_video(lambda:window.run_js("activeTab().videoExportState==='done'&&Boolean(document.querySelector('.video-job a[href]'))"),25,'Trim export completion')
                export_id=window.run_js("activeTab().videoExportId")
                output=app.video_review.output(export_id)
                assert output.is_file() and output.stat().st_size>1000 and output.is_relative_to(root/'data'),str(output)
                decoded=subprocess.run([ffmpeg,'-nostdin','-hide_banner','-loglevel','error','-progress','pipe:1','-nostats',
                    '-i',str(output),'-map','0:v:0','-f','null','-'],check=True,timeout=12,capture_output=True,text=True)
                output_seconds=max((int(line.split('=',1)[1])/1_000_000 for line in decoded.stdout.splitlines()
                                    if line.startswith('out_time_us=')),default=-1)
                selected_seconds=float(saved_review['out']-saved_review['in'])
                assert output_seconds>=0 and abs(output_seconds-selected_seconds)<=.35 and output_seconds<info['duration']-1.0,{
                    'export_seconds':output_seconds,'selected_seconds':selected_seconds,'source_seconds':info['duration'],
                    'ffmpeg_progress':decoded.stdout[-1000:]}
                result['checks'].append('Selected trim export is an isolated MP4 decoded by bundled FFmpeg; its measured duration matches the selected range')

                window.run_js("selectSection('skills');true")
                wait(lambda:window.run_js("Boolean(document.querySelector('[data-source-group=dsh]'))"))
                window.run_js("document.querySelector('[data-source-group=dsh]').click();true")
                wait(lambda:window.run_js("state.skills.length === 1 && state.skills[0].name === 'DSH 合成规范'"))
                assert window.run_js("state.skills[0].editable === false && document.querySelector('#skillSourceDirectory').options.length === 2")
                window.run_js("document.querySelector('[data-action=manage-skill-sources]').click();true")
                wait(lambda:window.run_js("Boolean(document.querySelector('#skillSourceManager')) && Boolean(document.querySelector('#skillSourceList')?.textContent.includes('.workbuddy'))"))
                assert window.run_js("document.querySelector('#skillSourceList').textContent.includes('.workbuddy')")
                assert all(path.read_bytes()==before for path,before in skill_files)
                result['checks'].append('SKILL software and location filters show isolated DSH and WorkBuddy sources in WKWebView without modifying originals')
                result['ok']=True
            except Exception:
                result['error']=traceback.format_exc()
            finally:
                host.guard.allowed=True
                try:
                    if host.ready:
                        window.run_js("document.querySelectorAll('video,audio').forEach(media=>media.pause());true")
                except Exception:pass
                window.destroy()
        try:webview.start(exercise,private_mode=True)
        finally:stop_server(service,app)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    if not result['ok']:raise SystemExit(1)


if __name__=='__main__':run()
