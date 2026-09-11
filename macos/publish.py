"""Publish only after executing the downloaded preview ZIP; retain old releases."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'macos'))
from verify_release import verify

REPO='turnsolesama/yingxu'
TAG='yingxu-v0.4.5-mac.1'
NAME='YingXu-v0.4.5-mac.1-macOS-arm64'

def gh(*args,check=True):
    return subprocess.run(['gh',*args,'--repo',REPO],check=check,capture_output=True,text=True)

def main():
    assert os.environ['GITHUB_REPOSITORY']==REPO
    commit=os.environ['GITHUB_SHA']
    artifacts=ROOT/'releases/macos-preview'
    names=[NAME+'.zip',NAME+'-manifest.json',NAME+'-SHA256.txt','YingXu-v0.4.5-mac.1-verification.json']
    manifest=json.loads((artifacts/names[1]).read_text())
    assert manifest['source_commit']==commit
    existing=gh('release','view',TAG,'--json','isDraft,targetCommitish',check=False)
    if existing.returncode==0:
        release=json.loads(existing.stdout)
        if not release['isDraft']:
            raise RuntimeError('This preview is already published; preserve its immutable assets')
        if release['targetCommitish']!=commit:
            raise RuntimeError('Existing draft belongs to another source commit')
    notes='''映序 0.4.5 macOS 试用版（0.4.5-mac.1），适用于 macOS 14+ 的 Apple Silicon / M 系列芯片。

## 本次更新

- SKILL 库增加 DSH、WorkBuddy、ZCode、共享技能来源和具体目录筛选；支持登记自定义目录，外部原文件保持只读。没有增加模型或运行依赖。
- 同步 0.4.5 文档内查找，Command+F 根据焦点搜索正文或当前资源，Command+K 保留全局搜索。
- Word 预览与编辑分页，查找可跨页定位；Markdown 支持拖入项目文件建立链接。
- 修复画板切换与撤销、本地字体和隐藏刷新，减少缩放时重复序列化。
- 统一右键菜单和多选范围提示；设置提供版本信息及缓存、历史占用预览与清理。

## 安装与边界

下载下方 macOS-arm64.zip，完整解压，将 YingXu.app 拖入“应用程序”。自带 Python、Pillow、FFmpeg 和本地编辑器，使用系统 WebKit，无新增模型，正常启动不下载组件。

这是未做 Apple Developer ID 签名、公证的试用包，不支持 Intel Mac。没有全局截图、菜单栏常驻、原生拖出和 Finder“打开方式”注册；人工输入法、权限提示和长期使用仍需试用反馈。更新前保存文稿并退出旧版，应用数据与项目目录保留。

最终 ZIP 在 macOS 构建机解压后验证签名、原生操作与真实 WKWebView，再上传、重新下载并重复验证。所有测试使用隔离合成数据。校验值、来源提交和测试范围见附件清单。

[Windows 发布列表](https://github.com/turnsolesama/yingxu/releases) · [完整更新记录](https://github.com/turnsolesama/yingxu#readme) · [Mac 安装说明](https://github.com/turnsolesama/yingxu/blob/main/macos/README.md)
'''
    with tempfile.TemporaryDirectory(prefix='yingxu-release-publish-') as temporary:
        root=Path(temporary);note=root/'notes.md';note.write_text(notes,encoding='utf-8')
        if existing.returncode:
            gh('release','create',TAG,*[str(artifacts/n) for n in names],
               '--target',commit,'--title','映序 0.4.5 · macOS M 系列试用版',
               '--notes-file',str(note),'--draft','--prerelease')
        else:
            gh('release','edit',TAG,'--notes-file',str(note),'--prerelease')
            gh('release','upload',TAG,*[str(artifacts/n) for n in names],'--clobber')
        downloaded=root/'downloaded'
        gh('release','download',TAG,'--dir',str(downloaded))
        for name in names:
            assert hashlib.sha256((downloaded/name).read_bytes()).digest()==hashlib.sha256((artifacts/name).read_bytes()).digest()
        result=verify(downloaded/names[0])
        assert result['ok'] and result['source_commit']==commit
        gh('release','edit',TAG,'--draft=false','--prerelease','--latest=false')
        print('macOS preview downloaded, extracted, verified, and published.',flush=True)
        portal=root/'downloads.md'
        portal.write_text('''映序 0.4.5 下载入口。Windows 正式包和 macOS 试用包分别选择，历史版本保留。

## 当前下载

- [Windows 完整包发布列表](https://github.com/turnsolesama/yingxu/releases)（以已发布附件为准）
- [macOS M 系列 0.4.5-mac.1 试用版与校验](https://github.com/turnsolesama/yingxu/releases/tag/yingxu-v0.4.5-mac.1)

## 0.4.5 更新

SKILL 新增多软件来源、目录分类与自定义登记；保留文档内查找、Word 跨页定位与 Markdown 文件链接；修复画板撤销、本地字体及后台刷新；整理右键菜单，加入版本和容量预览。详情见[更新记录](https://github.com/turnsolesama/yingxu#readme)。

Mac 包适用于 macOS 14+ Apple Silicon，未公证，暂不支持 Intel、全局截图或菜单栏常驻。各平台独立构建和验证，校验和与具体测试范围见对应发布页。下方 Source code 是源码，不是安装包。

[旧 macOS 0.4.3 试用包](https://github.com/turnsolesama/portfolio/releases/tag/yingxu-v0.4.3) · [迁移说明](https://github.com/turnsolesama/yingxu/blob/main/MIGRATION.md)
''',encoding='utf-8')
        gh('release','edit','downloads-2026-09-12','--title','映序 YingXu 0.4.5 · Windows 与 macOS 下载','--notes-file',str(portal))

if __name__=='__main__':main()

