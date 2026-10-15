"""Prepare only pinned upstream archives; never copy an installed environment.

Downloads are an explicit developer build step, not application startup behavior.
The default is offline: put the lock file's archives in --cache first.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'tools' / 'runtime-lock.json'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def unpack(archive, target):
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            name = PurePosixPath(entry.filename)
            if name.is_absolute() or '..' in name.parts or '\\' in entry.filename or ':' in entry.filename:
                raise ValueError('Unsafe upstream archive member')
        if source.testzip():
            raise ValueError('Upstream ZIP checksum failed')
        source.extractall(target)


def prepare_ffmpeg(archive, extraction, target, lock_entry):
    """Copy the locked FFmpeg/ffprobe pair after checking its shared version."""
    extraction.mkdir(parents=True, exist_ok=True)
    unpack(archive, extraction)
    roots = list(extraction.iterdir())
    if len(roots) != 1 or not roots[0].is_dir() or roots[0].is_symlink():
        raise ValueError('Pinned FFmpeg archive must contain one application directory')
    source = roots[0]
    source_bin = source / 'bin'
    if source_bin.is_symlink() or not source_bin.is_dir():
        raise ValueError('Pinned FFmpeg archive has no safe bin directory')
    executables = [source_bin / name for name in ('ffmpeg.exe', 'ffprobe.exe')]
    for executable in executables:
        if executable.is_symlink() or not executable.is_file() or executable.stat().st_size <= 0:
            raise ValueError('Pinned FFmpeg archive is missing ffmpeg.exe or ffprobe.exe')
    match = re.search(r'/ffmpeg-([0-9]+(?:\.[0-9]+)+)-', str(lock_entry.get('url', '')))
    if not match:
        raise ValueError('Pinned FFmpeg version is not present in the source lock')
    expected_version = match.group(1)
    actual_versions = []
    environment = os.environ.copy()
    system_root = environment.get('SystemRoot')
    search_path = [str(source_bin)]
    if system_root:
        search_path.append(str(Path(system_root) / 'System32'))
    environment['PATH'] = ';'.join(search_path)
    for executable, tool in zip(executables, ('ffmpeg', 'ffprobe')):
        result = subprocess.run([str(executable), '-version'], check=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', timeout=20,
            cwd=str(source_bin), env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        output = result.stdout or ''
        if len(output) > 65536:
            raise ValueError('Pinned FFmpeg version output exceeds limit')
        version = re.search(r'^' + tool + r' version ([^\s]+)', output, re.MULTILINE)
        if not version or not (version.group(1) == expected_version or version.group(1).startswith(expected_version + '-')):
            raise ValueError('Pinned FFmpeg archive version does not match its lock')
        actual_versions.append(version.group(1))
    if actual_versions[0] != actual_versions[1]:
        raise ValueError('Pinned ffmpeg and ffprobe versions differ')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError('FFmpeg runtime target already exists')
    shutil.copytree(source, target)
    # Frame inspection needs the matching probe binary. ffplay is not shipped.
    (target / 'bin' / 'ffplay.exe').unlink(missing_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime')
    args = parser.parse_args()
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    args.cache.mkdir(parents=True, exist_ok=True)
    for name, entry in lock.items():
        archive = args.cache / entry['file']
        if not archive.is_file() and args.download:
            temporary = archive.with_suffix('.partial')
            request = urllib.request.Request(entry['url'], headers={'User-Agent': 'YingXu-release-builder'})
            with urllib.request.urlopen(request, timeout=180) as response, temporary.open('wb') as output:
                shutil.copyfileobj(response, output)
            temporary.replace(archive)
        if not archive.is_file() or archive.stat().st_size != entry['bytes'] or digest(archive) != entry['sha256']:
            raise ValueError('Missing or mismatched pinned archive: ' + name)
    target = args.output.resolve()
    if target.exists():
        raise ValueError('Use a new output directory; existing runtimes are never overwritten')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='yingxu-runtime-build-', dir=target.parent) as temporary:
        base = Path(temporary)
        stage = base / 'runtime'
        unpack(args.cache / lock['python']['file'], stage)
        # Explicit app-relative import paths, no registry, environment, user site or pip.
        pth = next(stage.glob('python*._pth'))
        pth.write_text(pth.stem + '.zip\n.\n..\nLib/site-packages\n', encoding='ascii')
        unpack(args.cache / lock['pillow']['file'], stage / 'Lib' / 'site-packages')
        prepare_ffmpeg(args.cache / lock['ffmpeg']['file'], base / 'ffmpeg-extract',
                       stage / 'ffmpeg', lock['ffmpeg'])
        browser = base / 'browser'
        browser.mkdir()
        subprocess.run(['expand.exe', str((args.cache / lock['webview2']['file']).resolve()), '-F:*', str(browser)],
                       check=True, stdout=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        browser_root = next(browser.rglob('msedgewebview2.exe')).parent
        shutil.copytree(browser_root, stage / 'webview2')
        shutil.copy2(ROOT / 'THIRD_PARTY_NOTICES.md', stage / 'THIRD_PARTY_NOTICES.md')
        inventory = [{'path': p.relative_to(stage).as_posix(), 'bytes': p.stat().st_size, 'sha256': digest(p)}
                     for p in sorted(stage.rglob('*')) if p.is_file()]
        (stage / 'RUNTIME_MANIFEST.json').write_text(json.dumps({'sources': lock, 'files': inventory}, indent=2), encoding='utf-8')
        shutil.move(str(stage), str(target))
    print(json.dumps({'runtime': str(target), 'files': len(inventory), 'bytes': sum(p['bytes'] for p in inventory)}))


if __name__ == '__main__':
    main()
