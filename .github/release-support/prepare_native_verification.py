"""Prepare only public pinned inputs for disposable Windows native verification."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path(os.environ['RUNNER_TEMP']) / 'yingxu-candidate-verify'
CACHE.mkdir(exist_ok=False)
BASE_SHA = 'f572b591fe8a7ea227d0db49cc2510132ceea836ca23b2ce4dadf1f044ff8f7c'
MANIFEST_SHA = 'b63ef28a6e717ea444c3769e7e1d308f57b4241b28b00bd6fd6e46e5f8cf0156'
SDK_SHA = 'f492bbf547d0da329553b6727435b677579b1e9f91cc9e4a1ad029366d5f23d0'


def download(url, destination, size, digest):
    request = urllib.request.Request(url, headers={'User-Agent': 'YingXu-candidate-native-verification'})
    count = 0
    fingerprint = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=180) as response, destination.open('xb') as output:
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            if count > size:
                raise ValueError('Input exceeds the pinned byte size')
            fingerprint.update(chunk)
            output.write(chunk)
    assert count == size and fingerprint.hexdigest() == digest


archive = CACHE / 'YingXu-v0.4.22-Windows-x64.zip'
download('https://github.com/NOXEVYR/yingxu/releases/download/yingxu-v0.4.22/YingXu-v0.4.22-Windows-x64.zip',
         archive, 448336960, BASE_SHA)
runtime = ROOT / 'runtime'
assert not runtime.exists()
with zipfile.ZipFile(archive) as package:
    names = set()
    for item in package.infolist():
        relative = PurePosixPath(item.filename)
        assert not relative.is_absolute() and '..' not in relative.parts and relative.parts[0] == 'YingXu'
        assert ':' not in item.filename and '\\' not in item.filename
        assert item.filename.casefold() not in names
        assert not stat.S_ISLNK(item.external_attr >> 16)
        names.add(item.filename.casefold())
    assert package.testzip() is None
    release_bytes = package.read('YingXu/RELEASE_MANIFEST.json')
    assert hashlib.sha256(release_bytes).hexdigest() == MANIFEST_SHA
    manifest_bytes = package.read('YingXu/runtime/RUNTIME_MANIFEST.json')
    manifest = json.loads(manifest_bytes)
    assert manifest['sources'] == json.loads((ROOT / 'tools/runtime-lock.json').read_text(encoding='utf-8'))
    runtime.mkdir()
    for item in manifest['files']:
        relative = PurePosixPath(item['path'])
        assert not relative.is_absolute() and '..' not in relative.parts
        assert ':' not in item['path'] and '\\' not in item['path']
        data = package.read('YingXu/runtime/' + item['path'])
        assert len(data) == item['bytes'] and hashlib.sha256(data).hexdigest() == item['sha256']
        destination = runtime.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    (runtime / 'RUNTIME_MANIFEST.json').write_bytes(manifest_bytes)
download('https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.4191.47/microsoft.web.webview2.1.0.4191.47.nupkg',
         CACHE / 'webview2.nupkg', 9259926, SDK_SHA)
record = {'base_package_sha256': BASE_SHA, 'sdk_sha256': SDK_SHA,
          'runtime_files_verified': len(manifest['files']), 'private_data_used': False,
          'source_commit': os.environ['GITHUB_SHA']}
(ROOT / 'native-preparation.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
print(json.dumps(record))
