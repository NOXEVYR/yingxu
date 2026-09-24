"""Review-only fallback: explicit Windows 0.4.19 release, no implicit dependencies.

Install at .github/release-support/cached_release.py after review. Running prepare
downloads fixed public inputs; running publish mutates a new GitHub release.
Both commands refuse execution outside an explicitly approved Actions event.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile

REPO = 'NOXEVYR/yingxu'
VERSION = '0.4.19'
TAG = 'yingxu-v' + VERSION
BASE_TAG = 'yingxu-v0.4.18'
BASE_NAME = 'YingXu-v0.4.18-Windows-x64.zip'
BASE_BYTES = 448152955
BASE_SHA = 'df7cb0df72940b121654eb34b915c3b6f9855fcc8090311b879c47d2e8ffcc0a'
SDK_URL = 'https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.4191.47/microsoft.web.webview2.1.0.4191.47.nupkg'
SDK_BYTES = 9259926
SDK_SHA = 'f492bbf547d0da329553b6727435b677579b1e9f91cc9e4a1ad029366d5f23d0'
REQUEST_PATH = '.github/release-requests/yingxu-0.4.19.json'
PUSH_TITLE = 'release: YingXu 0.4.19 cached runtime [cloud-approved-500MiB]'
SOURCE_TESTS = {'tests/' + name for name in ('frontend_live_markdown.cjs', 'frontend_live_tables.cjs',
                 'frontend_markdown_links.cjs', 'frontend_obsidian_images.cjs')}


def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): value.update(block)
    return value.hexdigest()


def command(args, *, capture=True):
    result = subprocess.run(args, check=False, text=True, encoding='utf-8',
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if result.returncode:
        # Do not print environment, auth headers, or credential-helper output.
        raise RuntimeError('Command failed: ' + str(args[0]) + ' (exit ' + str(result.returncode) + ')')
    return result.stdout.strip() if capture else ''


def gh_json(path):
    return json.loads(command(['gh', 'api', 'repos/' + REPO + '/' + path]))


def authorized(root, env=None, event=None, git=command):
    env = os.environ if env is None else env
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_REPOSITORY') != REPO or
            env.get('GITHUB_REF') != 'refs/heads/main' or
            not re.fullmatch('[a-f0-9]{40}', env.get('GITHUB_SHA', ''))):
        raise ValueError('This helper only runs in the explicitly approved official main workflow')
    commit = env['GITHUB_SHA']
    if git(['git', 'rev-parse', 'HEAD']) != commit: raise ValueError('Checkout differs from triggering commit')
    if event is None: event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    if env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch':
        inputs = event.get('inputs', {})
        if str(inputs.get('approve_download_and_publish', '')).lower() != 'true' or inputs.get('source_commit') != commit:
            raise ValueError('Manual approval and exact reviewed source SHA are required')
    elif env.get('GITHUB_EVENT_NAME') == 'push':
        if event.get('head_commit', {}).get('message') != PUSH_TITLE:
            raise ValueError('Push has no explicit release authorization marker')
        parents = git(['git', 'rev-list', '--parents', '-n', '1', 'HEAD']).split()
        changed = git(['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD']).splitlines()
        request = json.loads((root / REQUEST_PATH).read_text(encoding='utf-8'))
        if (len(parents) != 2 or changed != [REQUEST_PATH] or request.get('approved') is not True or
                request.get('reviewed_source_commit') != parents[1] or request.get('version') != VERSION or
                type(request.get('download_budget_mib')) is not int or request['download_budget_mib'] != 500):
            raise ValueError('Only a separately reviewed approval-file commit can trigger publication')
    else:
        raise ValueError('Automatic builds have not been authorized')
    return commit


def assert_new_release():
    # A current release/tag must never be overwritten or moved by this fallback.
    for endpoint in ('releases/tags/' + TAG, 'git/ref/tags/' + TAG):
        result = subprocess.run(['gh', 'api', 'repos/' + REPO + '/' + endpoint],
                                capture_output=True, text=True, encoding='utf-8')
        if result.returncode == 0: raise ValueError('Existing release/tag preserved; inspect it before retrying')
        if 'HTTP 404' not in result.stderr: raise ValueError('Cannot establish that the release/tag is absent')


def check_assets(release, expected):
    assets = release.get('assets', [])
    if len(assets) != len(expected): raise ValueError('Unexpected release attachment count')
    seen = set()
    for asset in assets:
        name = asset.get('name')
        if name in seen or name not in expected: raise ValueError('Duplicate/unexpected attachment')
        seen.add(name); size, digest = expected[name]
        if (type(asset.get('id')) is not int or asset['id'] <= 0 or asset.get('state') != 'uploaded' or
                asset.get('size') != size or asset.get('digest') != 'sha256:' + digest):
            raise ValueError('GitHub asset size/digest differs from the verified local attachment: ' + name)
    return {asset['name']: asset['id'] for asset in assets}


def download(url, path, size, digest):
    path = Path(path)
    if path.exists() and path.stat().st_size == size and sha(path) == digest: return
    if path.exists() and path.stat().st_size >= size: raise ValueError('Existing cached input is invalid; do not silently redownload')
    # One transfer per invocation; -C resumes a bounded partial file. No retry loop
    # silently expands the approved budget. GitHub handles only trusted fixed URLs.
    command(['curl.exe', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https',
             '--connect-timeout', '30', '--max-time', '1200', '--max-filesize', str(size),
             '--continue-at', '-', '--output', str(path), url], capture=False)
    if path.stat().st_size != size or sha(path) != digest: raise ValueError('Official input size/SHA-256 mismatch')


def safe_name(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('Unsafe archive path')
    parts = PurePosixPath(name).parts
    if name.startswith('/') or any(p in ('', '.', '..') or p.endswith((' ', '.')) or
            re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)', p) or
            any(ord(c) < 32 or c in '<>"|?*' for c in p) for p in name.split('/')):
        raise ValueError('Unsafe archive path')
    return parts


def validate_base(archive):
    infos = archive.infolist(); names = [i.filename for i in infos]
    if len(names) != len(set(n.casefold() for n in names)) or not 1 < len(names) < 20000:
        raise ValueError('Duplicate/invalid base archive members')
    for info in infos:
        safe_name(info.filename)
        if (not info.filename.startswith('YingXu/') or info.is_dir() or info.flag_bits & 1 or
                stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG)):
            raise ValueError('Unsupported base archive member')
    member = archive.getinfo('YingXu/RELEASE_MANIFEST.json')
    if member.file_size > 8 * 1024**2: raise ValueError('Oversize base manifest')
    manifest = json.loads(archive.read(member))
    if (manifest.get('application'), manifest.get('root'), manifest.get('version'), manifest.get('architecture')) != ('YingXu','YingXu/','0.4.18','Windows x64'):
        raise ValueError('Wrong base package identity')
    records = {}
    for row in manifest['files']:
        name = row['path']; safe_name(name)
        if name in records or name == 'RELEASE_MANIFEST.json' or type(row['bytes']) is not int or row['bytes'] < 0:
            raise ValueError('Invalid base manifest record')
        records[name] = row
    if set(names) != {'YingXu/' + n for n in records} | {'YingXu/RELEASE_MANIFEST.json'}:
        raise ValueError('Base member set differs from manifest')
    for name, row in records.items():
        info = archive.getinfo('YingXu/' + name)
        if info.file_size != row['bytes']: raise ValueError('Base member size mismatch')
        h = hashlib.sha256()
        with archive.open(info) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''): h.update(block)
        if h.hexdigest() != row['sha256']: raise ValueError('Base member SHA-256 mismatch: ' + name)
    return records


def frozen_editor(name):
    return (name in SOURCE_TESTS or name.startswith(('tools/markdown-editor/', 'tools/canvas-editor/',
            'frontend/canvas/', 'frontend/live-markdown')))


def prepare(root, cache, commit):
    assert_new_release()
    base_release = gh_json('releases/tags/' + BASE_TAG)
    asset = next((a for a in base_release.get('assets', []) if a.get('name') == BASE_NAME), {})
    if asset.get('size') != BASE_BYTES or asset.get('digest') != 'sha256:' + BASE_SHA or asset.get('state') != 'uploaded':
        raise ValueError('Published official baseline has changed; review before downloading')
    if gh_json('git/ref/heads/main')['object']['sha'] != commit: raise ValueError('Main advanced; review before spending download budget')
    cache.mkdir(parents=True, exist_ok=True)
    base = cache / BASE_NAME; sdk = cache / 'webview2.nupkg'
    download(f'https://github.com/{REPO}/releases/download/{BASE_TAG}/{BASE_NAME}', base, BASE_BYTES, BASE_SHA)
    with zipfile.ZipFile(base) as archive:
        records = validate_base(archive)
        baseline_lock = json.loads(archive.read('YingXu/tools/runtime-lock.json'))
        if json.loads((root/'tools/runtime-lock.json').read_text(encoding='utf-8')) != baseline_lock:
            raise ValueError('Runtime dependencies changed; this reuse strategy is not applicable')
        for name in records:
            if frozen_editor(name):
                current = (root/name).read_bytes(); original = archive.read('YingXu/' + name)
                # Git line endings may differ for source files; binary assets remain exact.
                if name.endswith(('.js','.mjs','.cjs','.jsx','.css','.html','.json','.yaml','.txt')):
                    current = current.replace(b'\r\n', b'\n'); original = original.replace(b'\r\n', b'\n')
                if current != original: raise ValueError('Editor source/bundle changed; no automatic dependency download: ' + name)
        for parent in ('tools/markdown-editor','tools/canvas-editor','frontend/canvas'):
            if any(p.is_file() and p.relative_to(root).as_posix() not in records for p in (root/parent).rglob('*')):
                raise ValueError('Unexpected new editor/build input')
        runtime = root/'runtime'
        if runtime.exists(): raise ValueError('Checkout already contains a runtime; refusing to mix environments')
        runtime.mkdir()
        for name in records:
            if not name.startswith('runtime/'): continue
            target = root.joinpath(*safe_name(name)); target.parent.mkdir(parents=True,exist_ok=True)
            with archive.open('YingXu/' + name) as src, target.open('xb') as dst: shutil.copyfileobj(src,dst,1024*1024)
    sys.path.insert(0,str(root))
    from tools.package_release import VERSION as package_version, runtime_files
    from yingxu import __version__
    if package_version != VERSION or __version__ != VERSION: raise ValueError('Source version is not the reviewed release')
    list(runtime_files(runtime))  # source lock, every SHA-256, and exact runtime file set
    download(SDK_URL,sdk,SDK_BYTES,SDK_SHA)
    report = {'base_zip_bytes':BASE_BYTES,'sdk_bytes':SDK_BYTES,'fixed_download_bytes':BASE_BYTES+SDK_BYTES,
              'source_commit':commit,'base_sha256':BASE_SHA,'runtime_files_verified':len(list(runtime.rglob('*'))),
              'editor_development_tests_not_run_on_cloud':sorted(SOURCE_TESTS)}
    (cache/'preparation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
    print(json.dumps(report,ensure_ascii=False))


def publish(root, cache, commit):
    assert_new_release()
    if gh_json('git/ref/heads/main')['object']['sha'] != commit: raise ValueError('Main advanced; do not publish stale source')
    prepared = json.loads((cache/'preparation.json').read_text(encoding='utf-8'))
    if prepared['source_commit'] != commit or prepared['base_sha256'] != BASE_SHA: raise ValueError('Unverified preparation')
    release_dir=root/'releases'; prefix='YingXu-v'+VERSION
    files=[release_dir/(prefix+suffix) for suffix in ('-Windows-x64.zip','-manifest.json','-SHA256.txt','-verification.json')]
    manifest=json.loads(files[1].read_text(encoding='utf-8'))
    verification=json.loads(files[3].read_text(encoding='utf-8-sig'))
    if (manifest.get('source_commit') != commit or manifest.get('version') != VERSION or
            manifest.get('file') != files[0].name or manifest.get('bytes') != files[0].stat().st_size or
            manifest.get('sha256') != sha(files[0]) or verification.get('ok') is not True or
            verification.get('source_commit') != commit or verification.get('archive') != files[0].name):
        raise ValueError('Local verified package is not bound to this source')
    with zipfile.ZipFile(files[0]) as archive:
        internal=archive.read('YingXu/RELEASE_MANIFEST.json')
        if hashlib.sha256(internal).hexdigest() != manifest.get('release_manifest_sha256'):
            raise ValueError('Missing/mismatched incremental manifest proof')
    notes=(f'Windows {VERSION}：修复通过目录联接打开 Markdown，并加入按需增量更新。\n\n'
           '- 检查差异后显示下载量，由用户确认下载；保存文稿并完整退出后安装。\n'
           '- 未变化文件复用；校验、退出保护、回退及中断恢复保留。Mac 继续使用独立完整包。\n'
           '- 截图工具栏按实际窗口宽度布局，修复小屏或高缩放下按钮越界，截图原始像素保留。构建 junction.2。\n'
           '- 官方 0.4.18 运行时经固定 ZIP 摘要和内部文件清单核验后复用，无新增运行依赖。\n\n'
           '云端执行后端、无开发依赖的前端、原生与完整包隔离验证。4 个依赖 esbuild 的编辑器源码测试本轮云端未运行；复用前核对相关源码、构建输入和产物与 0.4.18 一致。\n\n'
           '上传后按 GitHub 资产 ID、大小和 SHA-256 核对本地四附件；本轮没有再次完整下载新版 ZIP。\n'
           f'来源提交：{commit}\n')
    notes_file=cache/'release-notes.md'; notes_file.write_text(notes,encoding='utf-8',newline='\n')
    expected={p.name:(p.stat().st_size,sha(p)) for p in files}
    command(['gh','release','create',TAG,*map(str,files),'--repo',REPO,'--target',commit,'--draft',
             '--title',f'映序 {VERSION} · 增量更新与 Markdown 打开修复','--notes-file',str(notes_file)],capture=False)
    error=None
    for attempt in range(12):
        release=gh_json('releases/tags/'+TAG)
        try:
            asset_ids=check_assets(release,expected); break
        except ValueError as exc:
            error=exc
            if attempt==11: raise error
            time.sleep(2)
    # GitHub may defer creating the tag until a draft becomes public.
    if (release.get('draft') is not True or release.get('target_commitish') != commit or
            release.get('tag_name') != TAG or release.get('body', '').replace('\r\n','\n').replace('\r','\n') != notes):
        raise ValueError('Draft or tag identity changed; leave all assets untouched')
    command(['gh','release','edit',TAG,'--repo',REPO,'--draft=false','--latest'],capture=False)
    final=gh_json('releases/tags/'+TAG)
    if (final.get('draft') is not False or check_assets(final,expected)!=asset_ids or
            gh_json('git/ref/tags/'+TAG)['object']['sha'] != commit):
        raise ValueError('Final publication confirmation differs; inspect release before retrying')
    report={'published':True,'tag':TAG,'source_commit':commit,'asset_ids':asset_ids,
            'verification':'GitHub asset size and digest equal locally verified bytes; no full redownload',
            'expected_assets':expected}
    (cache/'publication.json').write_text(json.dumps(report,indent=2),encoding='utf-8',newline='\n')
    print(json.dumps(report))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=('prepare','publish'));args=parser.parse_args()
    root=Path(os.environ.get('GITHUB_WORKSPACE','.')).resolve()
    os.chdir(root)
    commit=authorized(root)
    if os.name!='nt' or not os.environ.get('GH_TOKEN'): raise ValueError('Official Windows runner token required')
    cache=Path(os.environ['RUNNER_TEMP']).resolve()/'yingxu-cached-release'
    (prepare if args.mode=='prepare' else publish)(root,cache,commit)


if __name__=='__main__':main()
