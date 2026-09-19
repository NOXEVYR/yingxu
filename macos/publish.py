"""Publish only after executing the downloaded preview ZIP; retain old releases."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'macos'))
from verify_release import verify

REPO='turnsolesama/yingxu'
TAG='yingxu-v0.4.16-mac.1'
NAME='YingXu-v0.4.16-mac.1-macOS-arm64'
ICON_REPAIR_COMMIT='fix: replace 0.4.6 icons [viewfinder-v1]'
ICON_REPAIR_WINDOWS_COMMIT=ICON_REPAIR_COMMIT+' [windows-only]'

def gh(*args,check=True):
    return subprocess.run(['gh',*args,'--repo',REPO],check=check,capture_output=True,encoding='utf-8')

def github_api(path,*args):
    output=subprocess.check_output(['gh','api',f'repos/{REPO}/{path}',*args],encoding='utf-8')
    return json.loads(output) if output.strip() else None

def icon_replacement_requested():
    if os.environ.get('GITHUB_REPOSITORY')!=REPO or os.environ.get('REPLACE_ICONS')!='true':return False
    event=os.environ.get('GITHUB_EVENT_NAME')
    if event=='workflow_dispatch':return True
    if event!='push' or os.environ.get('ICON_REPAIR_PUSH')!='true' or os.environ.get('GITHUB_REF')!='refs/heads/main':return False
    # Environment flags alone do not authorize a push-triggered overwrite.
    # Verify the immutable source commit through GitHub, with exact comparison.
    commit=os.environ.get('GITHUB_SHA')
    if not commit:return False
    message=github_api('commits/'+commit)['commit']['message']
    return message==ICON_REPAIR_COMMIT or (sys.platform=='win32' and message==ICON_REPAIR_WINDOWS_COMMIT)

def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def normalize_notes(text):
    # JSON preserves escaped CRLF, whereas Python text reads normalize it.
    # Normalize line endings only: spaces, blank lines and content stay exact.
    return (text or '').replace('\r\n','\n').replace('\r','\n')

def write_notes(path,text):
    path.write_text(normalize_notes(text),encoding='utf-8',newline='\n')

def replace_published_icons(tag,names,artifacts,notes,verifier,icon):
    """Verify staged assets before promotion; old remote assets remain intact."""
    if not icon_replacement_requested() or os.environ.get('GITHUB_REPOSITORY')!=REPO:
        raise RuntimeError('Icon replacement requires explicit workflow_dispatch or the exact approved main commit')
    if tag not in {'yingxu-v0.4.6','yingxu-v0.4.6-mac.1'}:
        raise RuntimeError('Icon replacement is limited to the approved 0.4.6 releases')
    commit=os.environ['GITHUB_SHA']
    manifest_name=next(name for name in names if name.endswith('-manifest.json'))
    verification_name=next(name for name in names if name.endswith('-verification.json'))
    archive_name=next(name for name in names if name.endswith('.zip'))
    manifest=json.loads((artifacts/manifest_name).read_text(encoding='utf-8-sig'))
    verification=json.loads((artifacts/verification_name).read_text(encoding='utf-8-sig'))
    if not (manifest.get('source_commit')==commit and manifest.get('icon_revision')=='viewfinder-v1'
            and manifest.get('icon_sha256')==digest(icon) and verification.get('ok') is True
            and manifest['sha256']==digest(artifacts/archive_name)
            and manifest['bytes']==(artifacts/archive_name).stat().st_size):
        raise RuntimeError('Replacement manifest, icon, source or ZIP verification is inconsistent')
    subprocess.run([sys.executable,'-B',str(verifier),str(artifacts/archive_name)],check=True)
    release=github_api('releases/tags/'+tag)
    if release['draft']:raise RuntimeError('Explicit icon replacement requires an existing published release')
    original_tag=github_api('git/ref/tags/'+tag)['object']
    assets={asset['name']:asset for asset in release['assets']}
    if any(name not in assets or not assets[name].get('digest','').startswith('sha256:') for name in names):
        raise RuntimeError('Original four assets and their GitHub SHA-256 digests are required')
    root=Path(tempfile.mkdtemp(prefix='yingxu-icon-stage-',dir=os.environ.get('RUNNER_TEMP')))
    suffix=commit[:12]+'-'+uuid.uuid4().hex[:8]
    stage_names={name:'icon-stage-'+suffix+'-'+name for name in names}
    backup_names={name:'icon-backup-'+suffix+'-'+name for name in names}
    upload=root/'upload';upload.mkdir()
    for name in names:
        try:os.link(artifacts/name,upload/stage_names[name])
        except OSError:shutil.copyfile(artifacts/name,upload/stage_names[name])
    original_note=root/'original-notes.md';write_notes(original_note,release.get('body'))
    (root/'original-release.json').write_text(json.dumps(release,indent=2),encoding='utf-8')
    print(f'Icon repair staging record: {root}; old remote assets retained without downloading',flush=True)
    repaired_note=root/'repaired-notes.md'
    write_notes(repaired_note,notes+'\n\n## 0.4.6 同版本图标修复\n\n'
        '任务栏、窗口和应用图标统一为四角取景框；图标修订 `viewfinder-v1`。原发布标签保持不变，附件由下列提交重新构建并完整验证。\n\n'
        f'- 附件源码提交：`{commit}`\n- 原标签对象（未移动）：`{original_tag["sha"]}`\n'
        f'- ZIP SHA-256：`{manifest["sha256"]}`\n')
    def current_assets():return {a['id']:a for a in github_api('releases/tags/'+tag)['assets']}
    def rename(asset_id,name):github_api(f'releases/assets/{asset_id}','--method','PATCH','-f','name='+name)
    def check_assets(expected):
        current=current_assets()
        for name,asset in expected.items():
            found=current.get(asset['id'],{})
            if found.get('name')!=name or any(found.get(key)!=asset[key] for key in ('digest','size')):
                raise RuntimeError('Release asset identity, name or digest changed: '+name)
    def clean_assets(expected):
        # Cleanup is outside the promotion transaction. Never restore old assets
        # after any backup has been deleted; report leftovers for later cleanup.
        leftovers=[]
        for name,asset in expected.items():
            try:
                found=current_assets().get(asset['id'])
                if found is None:continue
                if found['name']!=name or found['digest']!=asset['digest']:raise RuntimeError('asset changed')
                github_api(f'releases/assets/{asset["id"]}','--method','DELETE')
            except Exception as error:leftovers.append(f'{asset["id"]} ({name}): {error}')
        if leftovers:print('::warning::Icon repair cleanup leftovers; no rollback: '+'; '.join(leftovers),flush=True)
    staged={};promotion_started=False
    try:
        gh('release','upload',tag,*[str(upload/stage_names[n]) for n in names])
        uploaded={a['name']:a for a in current_assets().values()}
        for name in names:
            asset=uploaded[stage_names[name]]
            if asset['size']!=(artifacts/name).stat().st_size or asset['digest']!='sha256:'+digest(artifacts/name):
                raise RuntimeError('Staged asset digest differs: '+name)
            staged[name]=asset
        downloaded=root/'replacement-check'
        patterns=[arg for name in names for arg in ('--pattern',stage_names[name])]
        gh('release','download',tag,'--dir',str(downloaded),*patterns)
        for name in names:
            (downloaded/stage_names[name]).rename(downloaded/name)
            if digest(downloaded/name)!=digest(artifacts/name):raise RuntimeError('Replacement download checksum mismatch')
        subprocess.run([sys.executable,'-B',str(verifier),str(downloaded/archive_name)],check=True)
        check_assets({n:assets[n] for n in names})
        check_assets({stage_names[n]:staged[n] for n in names})
        if github_api('git/ref/tags/'+tag)['object']!=original_tag:raise RuntimeError('Original tag changed during replacement')
        promotion_started=True
        for name in names:rename(assets[name]['id'],backup_names[name])
        for name in names:rename(staged[name]['id'],name)
        check_assets(staged)
        check_assets({backup_names[n]:assets[n] for n in names})
        if github_api('git/ref/tags/'+tag)['object']!=original_tag:raise RuntimeError('Original tag changed during promotion')
        gh('release','edit',tag,'--notes-file',str(repaired_note))
        if normalize_notes(github_api('releases/tags/'+tag).get('body'))!=normalize_notes(repaired_note.read_text(encoding='utf-8')):
            raise RuntimeError('Updated release notes differ')
        if github_api('git/ref/tags/'+tag)['object']!=original_tag:
            raise RuntimeError('Original tag changed before promotion completed')
    except BaseException as error:
        if promotion_started:
            try:
                # Free canonical names first, including requests which succeeded
                # on GitHub but whose response was lost in transit.
                for name in names:
                    if staged[name]['id'] in current_assets():rename(staged[name]['id'],stage_names[name])
                for name in names:rename(assets[name]['id'],name)
                gh('release','edit',tag,'--notes-file',str(original_note))
                check_assets({n:assets[n] for n in names})
                if normalize_notes(github_api('releases/tags/'+tag).get('body'))!=normalize_notes(release.get('body')):
                    raise RuntimeError('Original release notes not restored')
            except BaseException as rollback:
                raise RuntimeError(f'Promotion rollback incomplete; retain all remote assets; record {root}: {rollback}') from error
        # Includes a partially failed upload; identify only our unique stage names.
        try:
            residual={a['name']:a for a in current_assets().values() if a['name'] in stage_names.values()}
            clean_assets(residual)
        except Exception as cleanup:print(f'::warning::Staging cleanup incomplete: {cleanup}; record {root}',flush=True)
        raise RuntimeError(f'Icon repair failed; original assets retained/restored; record {root}') from error
    # Promotion and notes are committed. Any cleanup error only leaves extra
    # assets and must never roll back to already-deleted backups.
    clean_assets({backup_names[n]:assets[n] for n in names})
    print(f'Icon repair published; original tag preserved; staging record {root}',flush=True)

def replace_windows_icons(note_path):
    names=['YingXu-v0.4.6-Windows-x64.zip','YingXu-v0.4.6-manifest.json',
           'YingXu-v0.4.6-SHA256.txt','YingXu-v0.4.6-verification.json']
    replace_published_icons('yingxu-v0.4.6',names,ROOT/'releases',Path(note_path).read_text(encoding='utf-8-sig'),
                            ROOT/'tools/verify_release.py',ROOT/'desktop/brand.ico')

def main():
    assert os.environ['GITHUB_REPOSITORY']==REPO
    commit=os.environ['GITHUB_SHA']
    artifacts=ROOT/'releases/macos-preview'
    names=[NAME+'.zip',NAME+'-manifest.json',NAME+'-SHA256.txt','YingXu-v0.4.16-mac.1-verification.json']
    manifest=json.loads((artifacts/names[1]).read_text())
    assert manifest['source_commit']==commit
    existing=gh('release','view',TAG,'--json','isDraft,targetCommitish',check=False)
    if icon_replacement_requested() and (existing.returncode!=0 or json.loads(existing.stdout)['isDraft']):
        raise RuntimeError('Explicit icon replacement requires an existing published preview')
    if existing.returncode==0:
        release=json.loads(existing.stdout)
        if not release['isDraft'] and not icon_replacement_requested():
            print('Published preview preserved; icon replacement was not explicitly requested.',flush=True)
            return
        if release['isDraft'] and release['targetCommitish']!=commit:
            raise RuntimeError('Existing draft belongs to another source commit')
    notes='''映序 0.4.16 macOS 试用版（0.4.16-mac.1），适用于 macOS 14+ 的 Apple Silicon / M 系列芯片。

## 本次更新

- 文件夹拖到项目库后，复制到设置的项目存放位置，归入当前项目分类，识别文本、角色、场景等目录后打开副本。
- 文件夹拖到资源区或具体分类，保留文件夹、子目录和空文件夹；原件保留，失败不自动重传。
- Windows 桌面复用原生路径与后台导入队列；浏览器目录上传保留层级。macOS 项目导入提供选择文件夹入口。
- 同一路径再次导入项目时打开已有副本，不覆盖修改。不新增运行依赖、启动扫描或常驻监控。

## 安装与边界

下载下方 macOS-arm64.zip，完整解压，将 YingXu.app 拖入“应用程序”。自带 Python、Pillow、FFmpeg 和本地编辑器，使用系统 WebKit，无新增模型，正常启动不下载组件。

这是未做 Apple Developer ID 签名、公证的试用包，不支持 Intel Mac。没有全局截图、菜单栏常驻、原生拖出和 Finder“打开方式”注册；人工输入法、权限提示和长期使用仍需试用反馈。更新前保存文稿并退出旧版，应用数据与项目目录保留。

最终 ZIP 在 macOS 构建机解压后验证签名、原生操作与真实 WKWebView，再上传、重新下载并重复验证。所有测试使用隔离合成数据。校验值、来源提交和测试范围见附件清单。

[Windows 发布列表](https://github.com/turnsolesama/yingxu/releases) · [完整更新记录](https://github.com/turnsolesama/yingxu#readme) · [Mac 安装说明](https://github.com/turnsolesama/yingxu/blob/main/macos/README.md)
'''
    if existing.returncode==0 and not release['isDraft']:
        replace_published_icons(TAG,names,artifacts,notes,ROOT/'macos/verify_release.py',ROOT/'desktop/brand.icns')
        return
    with tempfile.TemporaryDirectory(prefix='yingxu-release-publish-') as temporary:
        root=Path(temporary);note=root/'notes.md';write_notes(note,notes)
        if existing.returncode:
            gh('release','create',TAG,*[str(artifacts/n) for n in names],
               '--target',commit,'--title','映序 0.4.16 · 文件夹拖入与项目导入 · macOS M 系列试用版',
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
        write_notes(portal,'''映序 0.4.16 下载入口。Windows 正式包和 macOS 试用包分别选择，历史版本保留。

## 当前下载

- [Windows 完整包发布列表](https://github.com/turnsolesama/yingxu/releases)（以已发布附件为准）
- [macOS M 系列 0.4.16-mac.1 试用版与校验](https://github.com/turnsolesama/yingxu/releases/tag/yingxu-v0.4.16-mac.1)

## 0.4.16 更新

- 文件夹拖到项目库后，复制到设置的项目存放位置，归入当前项目分类，识别文本、角色、场景等目录后打开副本。
- 文件夹拖到资源区或具体分类，保留文件夹、子目录和空文件夹；原件保留，失败不自动重传。
- Windows 桌面复用原生路径与后台导入队列；浏览器目录上传保留层级。macOS 项目导入提供选择文件夹入口。
- 同一路径再次导入项目时打开已有副本，不覆盖修改。不新增运行依赖、启动扫描或常驻监控。
''')
        gh('release','edit','downloads-2026-09-12','--title','映序 YingXu 0.4.16 · Windows 与 macOS 下载','--notes-file',str(portal))

if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--replace-windows-icons':replace_windows_icons(sys.argv[2])
    elif len(sys.argv)==1:main()
    else:raise SystemExit('Unsupported publication arguments')
