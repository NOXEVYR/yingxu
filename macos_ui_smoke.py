"""Hidden real WKWebView probe with temporary data, including dirty-draft close refusal."""
from pathlib import Path
import json
import os
import tempfile
import threading
import time
import traceback


def run():
    import webview
    from server import Application, Server
    from macos_app import Desktop, stop_server
    from yingxu import __version__
    result={'ok':False,'checks':[],'real_user_data_used':False}
    with tempfile.TemporaryDirectory(prefix='yingxu-webkit-') as temporary:
        root=Path(temporary).resolve()
        os.environ.update(HOME=str(root/'user'),USERPROFILE=str(root/'user'))
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
        def wait(predicate):
            for _ in range(150):
                if predicate():return
                time.sleep(.1)
            raise AssertionError('WebKit check timed out')
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
                window.run_js("saveTab(activeTab());true")
                wait(lambda:window.run_js("activeTab().saved === true && !activeTab().dirty && !activeTab().saving"))
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
                    const wheel = new WheelEvent('wheel', {metaKey:true, deltaY:-100, bubbles:true, cancelable:true});
                    stage.dispatchEvent(wheel);
                    return wheel.defaultPrevented && camera.style.transform !== transform && activeTab().draft === before;
                })()""")
                result['checks'].append('WKWebView consumes synthetic Command wheel and changes only the mind-map viewport')
                window.run_js("""(() => {
                    const topic = JSON.parse(activeTab().draft)[0].rootTopic.children.attached[0];
                    document.querySelector(`.xmind-topic[data-topic-id="${topic.id}"]`).click();
                    const input = document.querySelector('[aria-label="主题链接"]');
                    input.value = 'yingxu://item/' + state.tabs[0].item.id;
                    input.dispatchEvent(new Event('input', {bubbles:true}));
                    [...document.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === '应用链接到所选主题').click();
                    window.__macXmindTabId = activeTab().id;
                    saveTab(activeTab());
                    return true;
                })()""")
                wait(lambda:window.run_js("activeTab().saved === true && !activeTab().dirty && !activeTab().saving"))
                window.run_js("document.querySelector('.xmind-link-mark').click();true")
                wait(lambda:window.run_js("activeTab().item.id === state.tabs[0].item.id"))
                assert window.run_js("state.tabs[0].dirty === true && state.tabs.some(tab => tab.id === window.__macXmindTabId && tab.item.kind === 'xmind')")
                result['checks'].append('Saved XMind file link opens the registered document while preserving its earlier draft and the map tab')
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
                window.destroy()
        try:webview.start(exercise,private_mode=True)
        finally:stop_server(service,app)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    if not result['ok']:raise SystemExit(1)


if __name__=='__main__':run()
