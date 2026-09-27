"""Explicit new Mac preview, with release-ID verification and full ZIP readback."""
from pathlib import Path
import json
import os
import subprocess
import sys

import cached_release as release

ROOT = Path(__file__).resolve().parents[2]
PREVIEW = '0.4.21-mac.1'
release.TAG = 'yingxu-v' + PREVIEW
release.PRERELEASE = True
release.RELEASE_TITLE = '映序 0.4.21 · 工作流稳定性修复 · macOS M 系列试用版'


def authorized():
    commit = release.authorized(ROOT)
    if sys.platform != 'darwin': raise ValueError('A macOS runner is required')
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    if os.environ['GITHUB_EVENT_NAME'] == 'push':
        request = json.loads((ROOT / release.REQUEST_PATH).read_text(encoding='utf-8'))
        if request.get('mac_download_budget_mib') != 300:
            raise ValueError('Mac download budget has not been approved')
    elif event.get('inputs', {}).get('mac_download_budget_mib') != '300':
        raise ValueError('Manual Mac approval must include its 300 MiB budget')
    return commit


def publish(commit):
    release.assert_new_release()
    sys.path.insert(0, str(ROOT / 'macos'))
    from verify_release import verify
    artifacts = ROOT / 'releases/macos-preview'
    name = 'YingXu-v' + PREVIEW + '-macOS-arm64'
    files = [artifacts / (name + suffix) for suffix in ('.zip','-manifest.json','-SHA256.txt')]
    files += [artifacts / ('YingXu-v' + PREVIEW + '-verification.json')]
    manifest = json.loads(files[1].read_text(encoding='utf-8'))
    result = json.loads(files[3].read_text(encoding='utf-8'))
    if (manifest.get('source_commit') != commit or manifest.get('version') != PREVIEW or
            manifest.get('source_version') != '0.4.21' or result.get('ok') is not True or
            result.get('source_commit') != commit or manifest.get('bytes') != files[0].stat().st_size or
            manifest.get('sha256') != release.sha(files[0])):
        raise ValueError('Mac package and verification are not bound to this source')
    cache = ROOT / '.release-work/macos-publication'
    cache.mkdir(parents=True, exist_ok=True)

    def verify_uploaded(draft, ids):
        # Read every uploaded asset by immutable ID, not a draft tag URL.
        downloaded = cache / 'downloaded'; downloaded.mkdir(exist_ok=False)
        for path in files:
            target = downloaded / path.name
            with target.open('xb') as stream:
                subprocess.run(['gh','api',f'repos/{release.REPO}/releases/assets/{ids[path.name]}',
                                '-H','Accept: application/octet-stream'], stdout=stream, check=True)
            if target.stat().st_size != path.stat().st_size or release.sha(target) != release.sha(path):
                raise ValueError('Mac uploaded attachment differs from the local verified file')
        result = verify(downloaded / files[0].name)
        if result.get('ok') is not True or result.get('source_commit') != commit:
            raise ValueError('Downloaded Mac package failed isolated native/WebKit verification')
        (cache / 'download-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')

    notes = '\n'.join([
        '映序 0.4.21 macOS 试用版（0.4.21-mac.1），适用于 macOS 14+、Apple Silicon。', '',
        '- 同步文稿异步载入与关闭时的草稿保护、迁移和跨项目移动文件身份维护、源副本残留管理。',
        '- 保留完整合法导入文件名，修复切换项目时旧资源重现和搜索范围残留。',
        '- 使用系统 WebKit，复用既有锁定依赖与无文字启动动效，不增加模型、字体或动画库。', '',
        '本包未做 Apple Developer ID 签名或公证，不支持 Intel Mac；Windows 截图、目录联接打开与增量安装不属于 Mac 功能。人工输入法、权限提示、多显示器和长期稳定性仍需试用反馈，不标记为稳定版。', '',
        '最终 ZIP 经解压、临时签名、原生与 WKWebView 合成检查，再按附件 ID 下载、校验 SHA-256 并重复验证后公开；测试仅使用隔离合成数据。',
        f'来源提交：{commit}', ''])
    release.publish_draft(cache,commit,files,notes,verify_uploaded=verify_uploaded)


if __name__ == '__main__':
    commit = authorized()
    if sys.argv[1:] == ['preflight']: release.assert_new_release()
    elif sys.argv[1:] == ['publish']: publish(commit)
    else: raise SystemExit('Expected preflight or publish')
