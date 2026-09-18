"""Verify and run the actual extracted macOS delivery ZIP with synthetic data."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import posixpath
import stat
import subprocess
import sys
import tempfile
import zipfile


def run_report(executable, flag):
    completed = subprocess.run([str(executable), flag], capture_output=True, text=True, timeout=180)
    reports = []
    for line in completed.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and 'ok' in item:
            reports.append(item)
    if completed.returncode or not reports or not reports[-1]['ok']:
        raise RuntimeError(f'{flag} failed: {completed.stdout[-6000:]} {completed.stderr[-2000:]}')
    assert reports[-1]['real_user_data_used'] is False
    return reports[-1]


def verify(archive):
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise RuntimeError('Run package verification on Apple Silicon macOS')
    archive = archive.resolve(strict=True)
    manifest = json.loads(archive.with_name(archive.stem+'-manifest.json').read_text())
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    assert manifest['sha256']==digest and manifest['bytes']==archive.stat().st_size
    assert manifest['version']=='0.4.12-mac.1' and manifest['source_version']=='0.4.12'
    assert manifest['contains_user_data'] is False
    assert archive.with_name(archive.stem+'-SHA256.txt').read_text().split()==[digest,archive.name]
    checks=[]
    with zipfile.ZipFile(archive) as bundle:
        infos=bundle.infolist();names=[i.filename for i in infos]
        assert len(infos)<20000 and len(names)==len(set(names))
        assert sum(i.file_size for i in infos)<2*1024**3
        for entry in infos:
            path=PurePosixPath(entry.filename)
            assert not path.is_absolute() and '..' not in path.parts and path.parts[0]=='YingXu'
            assert '\\' not in entry.filename
            assert not entry.filename.endswith(('.sqlite3','.sqlite3-wal','.sqlite3-shm','.log'))
            if stat.S_ISLNK(entry.external_attr >> 16):
                target=bundle.read(entry).decode('utf-8')
                resolved=posixpath.normpath(posixpath.join(str(path.parent),target))
                assert not target.startswith('/') and resolved.startswith('YingXu/')
        assert bundle.testzip() is None
    checks.append('Delivery ZIP SHA-256, CRC, bounded members and contained symlinks')
    with tempfile.TemporaryDirectory(prefix='yingxu-macos-release-') as temporary:
        root=Path(temporary).resolve()
        subprocess.run(['ditto','-x','-k',str(archive),str(root)],check=True)
        delivery=root/'YingXu'
        assert sorted(p.name for p in root.iterdir())==['YingXu']
        for entry in delivery.rglob('*'):
            assert entry.resolve().is_relative_to(delivery)
        app=delivery/'YingXu.app';executable=app/'Contents/MacOS/YingXu'
        info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
        if manifest.get('icon_revision'):
            assert manifest['icon_revision']=='viewfinder-v1'
            icon_name=info['CFBundleIconFile']
            if not icon_name.endswith('.icns'):icon_name+='.icns'
            icon=app/'Contents/Resources'/icon_name
            assert icon.resolve().is_relative_to((app/'Contents/Resources').resolve())
            assert hashlib.sha256(icon.read_bytes()).hexdigest()==manifest['icon_sha256']
            checks.append('Extracted application viewfinder icon matches repair manifest')
        assert info['CFBundleShortVersionString']=='0.4.12' and info['CFBundleVersion']=='41201'
        assert info['LSMinimumSystemVersion']=='14.0'
        assert subprocess.check_output(['lipo','-archs',str(executable)],text=True).strip()=='arm64'
        subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True)
        checks.append('Extracted app version, arm64 executable and ad-hoc signature')
        native=run_report(executable,'--smoke-test')
        webkit=run_report(executable,'--ui-smoke-test')
        checks.extend(native['checks']);checks.extend(webkit['checks'])
    return {'ok':True,'version':manifest['version'],'source_commit':manifest['source_commit'],
            'archive':archive.name,'bytes':manifest['bytes'],'sha256':digest,
            'checks':checks,'real_user_data_used':False,'extracted_zip_executed':True,
            'manual_ime_permissions_long_term_tested':False,
            'icon_revision':manifest.get('icon_revision'), 'icon_sha256':manifest.get('icon_sha256')}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('archive',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    report=verify(args.archive)
    text=json.dumps(report,ensure_ascii=False,indent=2)+'\n'
    if args.output:
        args.output.write_text(text,encoding='utf-8')
    print(text)
