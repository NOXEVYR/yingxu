"""Build an isolated Apple Silicon preview; final ZIP is verified separately."""
from pathlib import Path
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from macos_app import PREVIEW
from yingxu import __version__


def main():
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('Build on an Apple Silicon macOS host.')
    import imageio_ffmpeg
    base = ROOT / '.release-work/macos'
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='build-', dir=base))
    media = work / 'ffmpeg'
    shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), media)
    media.chmod(0o755)
    output = work / 'dist'
    args = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
            '--name', 'YingXu', '--icon', str(ROOT / 'desktop/brand.icns'),
            '--osx-bundle-identifier', 'io.github.turnsolesama.yingxu.preview',
            '--distpath', str(output), '--workpath', str(work / 'build'), '--specpath', str(work),
            '--paths', str(ROOT), '--hidden-import', 'macos_smoke', '--hidden-import', 'macos_ui_smoke',
            '--hidden-import', 'webview.platforms.cocoa', '--collect-data', 'webview',
            '--copy-metadata', 'pywebview', '--add-data', str(ROOT / 'frontend') + ':frontend',
            '--add-binary', str(media) + ':runtime/ffmpeg/bin', str(ROOT / 'macos_app.py')]
    subprocess.run(args, cwd=ROOT, check=True)
    app = output / 'YingXu.app'
    info = app / 'Contents/Info.plist'
    values = plistlib.loads(info.read_bytes())
    values.update(CFBundleShortVersionString=__version__, CFBundleVersion='40801',
                  LSMinimumSystemVersion='14.0', NSHighResolutionCapable=True,
                  NSDocumentsFolderUsageDescription='选择和管理您明确指定的视频创作项目与素材。')
    info.write_bytes(plistlib.dumps(values))
    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(app)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    artifact = ROOT / 'releases/macos-preview'
    artifact.mkdir(parents=True, exist_ok=True)
    staging = work / 'delivery/YingXu'
    staging.mkdir(parents=True)
    shutil.copytree(app, staging / 'YingXu.app', symlinks=True)
    shutil.copy2(ROOT / 'macos/README.md', staging / 'README-macOS.md')
    shutil.copy2(ROOT / 'LICENSE', staging / 'LICENSE')
    # Locked local wheels produce file:// references in pip freeze. The public
    # inventory records installed names/versions; upstream hashes remain in lock.
    freeze = subprocess.check_output([sys.executable, '-m', 'pip', 'list', '--format=freeze', '--disable-pip-version-check'], text=True)
    if any('file:' in line or ' @ ' in line for line in freeze.splitlines()):
        raise RuntimeError('Local dependency paths are not allowed in public metadata')
    (staging / 'BUILD-DEPENDENCIES.txt').write_text(freeze, encoding='utf-8')
    licenses = staging / 'licenses'
    licenses.mkdir()
    for distribution in metadata.distributions():
        name = distribution.metadata['Name']
        for file in distribution.files or []:
            if any(part.lower().startswith(('license', 'copying')) for part in file.parts):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    shutil.copy2(source, licenses / (name + '-' + str(file).replace('/', '_')))
    archive = artifact / f'YingXu-v{PREVIEW}-macOS-arm64.zip'
    subprocess.run(['ditto', '-c', '-k', '--norsrc', '--keepParent', str(staging), str(archive)], check=True)
    digest = hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest()
    manifest = {'file': archive.name, 'bytes': archive.stat().st_size, 'sha256': digest,
                'version': PREVIEW, 'source_version': __version__, 'source_commit': os.environ.get('GITHUB_SHA', ''),
                'icon_revision': 'viewfinder-v1', 'icon_sha256': hashlib.sha256((ROOT/'desktop/brand.icns').read_bytes()).hexdigest(),
                'platform': 'macOS 14+', 'architecture': 'arm64', 'signing': 'ad-hoc; not notarized',
                'root': 'YingXu/', 'contains_user_data': False}
    (artifact / (archive.stem + '-SHA256.txt')).write_text(digest + '  ' + archive.name + '\n', encoding='utf-8')
    (artifact / (archive.stem + '-manifest.json')).write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(archive)


if __name__ == '__main__':
    main()
