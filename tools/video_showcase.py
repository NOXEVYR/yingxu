"""Capture actual synthetic-video UI states; the native caption image is a labelled schematic."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Application, Server
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--media', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--browser', default='/usr/bin/chromium')
args = parser.parse_args()
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
captions = []

with tempfile.TemporaryDirectory(prefix='yingxu-showcase-') as temp:
    fixture = Path(temp).resolve()
    with patch('yingxu.skills.Path.home', return_value=fixture/'home'):
        app = Application(fixture/'data', fixture/'projects')
    server = Server(('127.0.0.1', 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    entries = {name: app.external.open({'paths': [str((args.media/name).resolve())]})['entries'][0]
               for name in ['cfr.mp4', 'mpeg4.avi', 'showcase-4k.mp4']}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=args.browser, headless=True,
                args=['--no-sandbox', '--autoplay-policy=no-user-gesture-required'])
            page = browser.new_page(viewport={'width': 1280, 'height': 1000})
            def shot(page, name, title, status):
                page.screenshot(path=str(output/name))
                captions.append({'file': name, 'title': title, 'status': status})
                print(name, title, status, flush=True)
            page.goto(base)
            page.wait_for_function('() => typeof state!=="undefined" && !!state.bootstrap')
            def open_video(name):
                page.evaluate('(id)=>openExternal(id,{workspace:true})', entries[name]['id'])
                page.wait_for_function('() => {const v=document.querySelector("#editorContent video");return v && (v.readyState>=2 || v.error)}')
            open_video('cfr.mp4')
            page.evaluate('document.querySelector("video").pause()')
            page.locator('.video-review summary').click()
            page.wait_for_function('() => !document.querySelector("[data-review=mark]").disabled')
            for t, commands in [(1, ['in','a']), (2.5, ['out','b'])]:
                page.evaluate('(t)=>document.querySelector("video").currentTime=t', t)
                page.wait_for_function('() => !document.querySelector("video").seeking')
                for command in commands: page.locator(f'[data-review={command}]').click()
            page.locator('.video-marker-note').fill('检查转场位置 · 合成素材')
            page.locator('[data-review=mark]').click()
            page.locator('[data-review=save]').click()
            page.wait_for_function('() => !activeTab().videoReviewSaving && !activeTab().videoReviewDirty')
            page.locator('[data-review=ab]').click()
            shot(page, '04-workbench-review.png', '主工作台：标记、AB 循环、入出点', '实测网页截图；±40 ms 是近似时间步进，未实现可靠逐帧')
            page.locator('[data-review=export]').click()
            page.wait_for_function('() => document.querySelector(".video-job a")', timeout=30000)
            page.locator('.video-review').evaluate('n=>n.scrollTop=n.scrollHeight')
            shot(page, '05-export-complete.png', '粗剪：真实重编码完成与下载', '实测网页截图；保留源文件，第一视频/音轨，8-bit SDR；快速复制另受关键帧约束')
            page.locator('[data-review=mark]').click()
            page.evaluate('() => {closeTab(state.activeKey);}')
            page.locator('#appDialog[open]').wait_for()
            shot(page, '07-unsaved-review.png', '关闭保护：保存、放弃或继续审片', '实测网页截图；真实未保存记录触发')
            page.locator('[data-choice=discard]').click()
            open_video('showcase-4k.mp4')
            page.evaluate('document.querySelector("video").pause()')
            page.locator('.video-review summary').click()
            page.wait_for_function('() => !document.querySelector("[data-review=in]").disabled')
            page.evaluate('document.querySelector("video").currentTime=0')
            page.locator('[data-review=in]').click()
            page.evaluate('document.querySelector("video").currentTime=7.5')
            page.wait_for_function('() => !document.querySelector("video").seeking')
            page.locator('[data-review=out]').click()
            page.locator('[data-review=export]').click()
            page.locator('[data-review=cancel]:visible').wait_for()
            page.locator('.video-review').evaluate('n=>n.scrollTop=n.scrollHeight')
            shot(page, '06-export-cancel.png', '粗剪：真实导出中与取消入口', '实测网页截图；4K 放大合成素材的实际 FFmpeg 任务，随后实际取消')
            page.locator('[data-review=cancel]').click()
            page.wait_for_function('() => document.querySelector(".video-job").textContent.includes("已取消")', timeout=30000)
            shot(page, '08-export-cancelled.png', '粗剪：取消完成与原文件保留', '实测网页截图；取消状态来自真实任务清理完成后的响应')
            page.evaluate('activeTab().discardVideoReview()')
            open_video('mpeg4.avi')
            page.wait_for_function('() => document.querySelector("video").error?.code===4')
            shot(page, '09-format-error.png', '格式反馈：AVI / MPEG-4 无法解码', '实测网页截图；明确显示当前引擎兼容边界，未宣称任意格式播放')
            html=(ROOT/'frontend/quick-video.html').read_text().replace('/*VIDEO_STYLE*/',(ROOT/'frontend/video-player.css').read_text()).replace('/*VIDEO_SCRIPTS*/',(ROOT/'frontend/video-player.js').read_text()+'\n'+(ROOT/'frontend/quick-video.js').read_text())
            quick=browser.new_page(viewport={'width':1280,'height':760})
            quick.add_init_script('window.chrome={webview:{addEventListener:(type,cb)=>window.hostReceive=cb}}')
            quick.route(base+'/quick/index.html',lambda route:route.fulfill(status=200,body=html,content_type='text/html'))
            quick.route(base+'/quick/media',lambda route:route.fulfill(path=str(args.media/'cfr.mp4'),content_type='video/mp4'))
            quick.goto(base+'/quick/index.html')
            quick.evaluate('(src)=>hostReceive({data:{action:"open-video",name:"CFR synthetic",src}})',base+'/quick/media')
            quick.wait_for_function('() => document.querySelector("video")?.readyState>=2')
            quick.evaluate('document.querySelector("video").loop=true;document.querySelector("video").currentTime=1')
            quick.wait_for_function('() => document.querySelector("video").currentTime>.5 && !document.querySelector("video").seeking')
            quick.mouse.move(300,600)
            shot(quick, '01-quick-controls.png', '快速播放：画面与常规控制条', '实测网页截图；运行真实快速页，宿主消息由测试发送；不含 Windows 原生标题栏')
            quick.locator('.video-screen').focus()
            quick.locator('.video-screen').press('f')
            quick.wait_for_function('() => !!document.fullscreenElement')
            quick.mouse.move(600,200)
            quick.wait_for_function('() => document.querySelector(".video-screen").classList.contains("controls-hidden")')
            quick.wait_for_timeout(220)
            shot(quick, '02-fullscreen-hidden-controls.png', '全屏播放：播放控件自动隐藏', '实测浏览器元素全屏；Windows 原生边框、标题栏、焦点及退出尺寸恢复尚未实机验证')
            diagram=browser.new_page(viewport={'width':1280,'height':640})
            diagram.set_content('''<!doctype html><meta charset="utf-8"><style>body{margin:0;background:#eef0f4;color:#202329;font:22px system-ui;padding:40px}h1{font-size:32px;margin:0 0 12px}p{line-height:1.6}small{display:block;color:#6b4a00;background:#fff0cb;padding:16px;border-radius:8px}.window{border:2px solid #a6abb5;border-radius:7px;margin-top:32px;overflow:hidden}.caption{height:48px;display:flex;align-items:center;background:#f6f7f9;font-size:17px}.title{flex:1;padding:0 16px}.entry{height:42px;display:flex;align-items:center;padding:0 16px;outline:3px solid #336de6;outline-offset:-3px;color:#17498f}.control{width:58px;text-align:center;font-size:27px}.body{height:205px;background:#121318;color:#b9bdc7;display:flex;align-items:center;justify-content:center}.note{font-size:18px;color:#3d4654}</style><h1>标题栏入口位置示意</h1><small>仅布局示意 · Windows 原生窗口尚未运行，不能作为原生验收截图</small><div class="window"><div class="caption"><span class="title">示例视频 · 映序快速播放</span><span class="entry">在映序中打开 ↗</span><span class="control">−</span><span class="control">□</span><span class="control">×</span></div><div class="body">内容区域示意（文档或视频）</div></div><p class="note">入口位于最小化 / 最大化 / 关闭的左侧，同属标题栏。<br>全屏时应连同标题栏隐藏；命中、DPI 和窗口还原仍待 Windows 实测。</p>''')
            shot(diagram, '03-native-caption-SCHEMATIC.png', '标题栏：在映序中打开的位置', '仅根据已实现 C# 布局制作的示意；不是原生截图')
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join();app.close()
        (output/'captions.json').write_text(json.dumps(sorted(captions,key=lambda x:x['file']),ensure_ascii=False,indent=2))
