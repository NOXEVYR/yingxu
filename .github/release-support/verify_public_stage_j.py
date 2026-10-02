"""Read-only checks of an already published exact Stage J package on Windows CI."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_COMMIT = os.environ['GITHUB_SHA']
SOURCE = 'd226f639e52113c019ac23615edd1eb63f58fcda'
ZIP_SHA = '9dd3eebf7e7225a619bb3ea5f6581b0fcf9b6f5abe981a39f39ab5036eaa2dac'
EXE_SHA = 'fe5d30f61f81987e61b9bf667a5098d02fb545ec19f7a18d80d6eca6b52a4db4'
REPORTS = ROOT / '.release-work/public-stage-j'
REPORTS.mkdir(parents=True, exist_ok=False)
helpers = {name: (ROOT / '.github/release-support' / name).read_bytes()
           for name in ('StageJHostChecks.cs', 'run_stage_j_host_checks.py')}
subprocess.run(['git', 'fetch', 'origin', SOURCE], cwd=ROOT, check=True)
subprocess.run(['git', 'checkout', '--detach', SOURCE], cwd=ROOT, check=True)
for name, data in helpers.items():
    destination = ROOT / '.github/release-support' / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    assert not destination.exists()
    destination.write_bytes(data)
os.environ['GITHUB_SHA'] = SOURCE
cache = Path(os.environ['RUNNER_TEMP']) / ('yx-public-' + uuid.uuid4().hex[:8])
cache.mkdir()

def download(url, name, size, fingerprint):
    path = cache / name
    request = urllib.request.Request(url, headers={'User-Agent': 'YingXu-public-package-verification'})
    actual, count = hashlib.sha256(), 0
    with urllib.request.urlopen(request, timeout=180) as response, path.open('xb') as output:
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            assert count <= size
            actual.update(chunk)
            output.write(chunk)
    assert count == size and actual.hexdigest() == fingerprint
    return path

package = download('https://github.com/NOXEVYR/yingxu/releases/download/yingxu-v0.4.27/YingXu-v0.4.27-Windows-x64.zip',
                   'YingXu-v0.4.27-Windows-x64.zip', 448637401, ZIP_SHA)
download('https://github.com/NOXEVYR/yingxu/releases/download/yingxu-v0.4.27/YingXu-v0.4.27-manifest.json',
         'YingXu-v0.4.27-manifest.json', 594, 'f364dd6a8c1925979829ac8655ff87673b77cbc7c6224fbdc7019c74fab13088')
sdk = download('https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.4191.47/microsoft.web.webview2.1.0.4191.47.nupkg',
               'webview2.nupkg', 9259926, 'f492bbf547d0da329553b6727435b677579b1e9f91cc9e4a1ad029366d5f23d0')
with zipfile.ZipFile(package) as archive:
    names = set()
    source_eol_conversions = []
    for item in archive.infolist():
        parts = PurePosixPath(item.filename).parts
        assert parts and parts[0] == 'YingXu' and len(parts) > 1
        assert not item.is_dir() and not stat.S_ISLNK(item.external_attr >> 16)
        assert not PurePosixPath(item.filename).is_absolute() and '..' not in parts
        assert ':' not in item.filename and '\\' not in item.filename
        assert item.filename.casefold() not in names
        names.add(item.filename.casefold())
    assert archive.testzip() is None
    manifest = json.loads(archive.read('YingXu/RELEASE_MANIFEST.json'))
    assert (manifest['version'], manifest['build_revision'], manifest['source_commit']) == ('0.4.27', 'calls.3', SOURCE)
    assert len(manifest['files']) + 1 == len(names)
    for row in manifest['files']:
        data = archive.read('YingXu/' + row['path'])
        assert len(data) == row['bytes'] and hashlib.sha256(data).hexdigest() == row['sha256']
        if row['path'] == 'YingXu.exe' or row['path'].startswith('runtime/'):
            destination = ROOT / row['path']
            assert not destination.exists()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        else:
            # Bind every distributed source byte to the actual release commit.
            blob = subprocess.check_output(['git', 'show', SOURCE + ':' + row['path']], cwd=ROOT)
            if blob != data:
                attribute = subprocess.check_output(['git', 'check-attr', 'text', '--', row['path']], cwd=ROOT, text=True).strip()
                assert not attribute.endswith(': unset') and b'\0' not in blob and b'\0' not in data, row['path']
                blob.decode('utf-8')
                data.decode('utf-8')
                assert blob.replace(b'\r\n', b'\n') == data.replace(b'\r\n', b'\n'), row['path']
                source_eol_conversions.append(row['path'])
            # Exercise distributed bytes, including the publisher's text EOLs.
            (ROOT / row['path']).write_bytes(data)
assert hashlib.sha256((ROOT / 'YingXu.exe').read_bytes()).hexdigest() == EXE_SHA
fixture = Path('C:/yxpub') / uuid.uuid4().hex[:8]
for name in ('temp', 'home', 'local', 'roaming', 'data', 'projects'):
    (fixture / name).mkdir(parents=True)
env = os.environ.copy()
for name in list(env):
    if name.startswith('ACTIONS_') or name in ('GITHUB_TOKEN', 'GH_TOKEN', 'GIT_CONFIG_PARAMETERS'):
        env.pop(name, None)
env.update(TEMP=str(fixture / 'temp'), TMP=str(fixture / 'temp'), HOME=str(fixture / 'home'),
           USERPROFILE=str(fixture / 'home'), LOCALAPPDATA=str(fixture / 'local'), APPDATA=str(fixture / 'roaming'),
           YINGXU_DATA_DIR=str(fixture / 'data'), YINGXU_PROJECTS_DIR=str(fixture / 'projects'),
           YINGXU_NATIVE_IDENTITY_TEST_EXECUTABLE=str(ROOT / 'YingXu.exe'),
           YINGXU_INSTALLER_TEST_RUNTIME=str(ROOT / 'runtime'))
for name in ('PYTHONHOME', 'PYTHONPATH', 'YINGXU_PYTHON', 'YINGXU_RESUME_SESSION_TOKEN'):
    env.pop(name, None)
record = dict(source_commit=SOURCE, verification_helper_commit=WORKFLOW_COMMIT,
              version='0.4.27', build_revision='calls.3', package_sha256=ZIP_SHA,
              exe_sha256=EXE_SHA, private_data_used=False, external_ai_calls=0, steps=[])
record['source_eol_conversions'] = source_eol_conversions

def run(name, command, timeout):
    with (REPORTS / (name + '.stdout.txt')).open('wb') as out, (REPORTS / (name + '.stderr.txt')).open('wb') as err:
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=out, stderr=err, timeout=timeout)
            code, timed_out = result.returncode, False
        except subprocess.TimeoutExpired:
            code, timed_out = None, True
    record['steps'].append(dict(name=name, exit_code=code, timeout=timed_out))
    (REPORTS / 'result.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    if code != 0:
        print((REPORTS / (name + '.stderr.txt')).read_text(encoding='utf-8', errors='replace'))
        print(name + ' failed; collecting independent package checks', flush=True)
    return code == 0

for module in ('test_native_build_identity.py', 'test_release.py', 'test_incremental_range_zip.py',
               'test_incremental_install.py', 'test_incremental_package_binding.py'):
    run(module[:-3], [str(ROOT / 'runtime/python.exe'), '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', module, '-v'], 120)
test_source = ROOT / 'desktop/Tests.cs'
test_bytes = test_source.read_bytes()
fixture_marker = b'            string folder = Path.Combine(Path.GetTempPath(),'
assert test_bytes.count(fixture_marker) == 1
record['native_test_fixture_adjustment'] = 'Initialize Hub.Root from the existing test argument before health identity checks; restore original Tests.cs before host/package checks'
try:
    # Tests.Main uses Hub.Root for health identity, whereas Program.Main normally
    # initializes it. Supply that missing fixture initialization without changing
    # any assertion or the published EXE. Keep original distributed bytes below.
    test_source.write_bytes(test_bytes.replace(fixture_marker,
        b'            Hub.Root = Hub.NormalizeRoot(args[0]);\r\n' + fixture_marker, 1))
    run('native-full-and-isolated-units', [sys.executable, '-B', 'desktop/build.py', '--sdk-package', str(sdk),
        '--output', str(REPORTS / 'rebuilt-test-host.exe'), '--manifest-output', str(REPORTS / 'rebuilt-build.json'),
        '--test', '--isolated-test-units'], 600)
finally:
    test_source.write_bytes(test_bytes)
assert hashlib.sha256((ROOT / 'YingXu.exe').read_bytes()).hexdigest() == EXE_SHA
run('public-stage-j-host', [sys.executable, '-B', '.github/release-support/run_stage_j_host_checks.py',
    '--sdk-package', str(sdk), '--output-dir', str(REPORTS / 'host')], 240)
run('complete-public-package', [sys.executable, '-B', 'tools/verify_release.py', str(package)], 240)
complete = json.loads((REPORTS / 'complete-public-package.stdout.txt').read_text(encoding='utf-8'))
assert complete['ok'] is True and complete['source_commit'] == SOURCE
with package.open('rb') as verified_file:
    assert hashlib.file_digest(verified_file, 'sha256').hexdigest() == ZIP_SHA
subprocess.run(['git', '-c', 'core.autocrlf=true', 'diff', '--exit-code', 'HEAD', '--'], cwd=ROOT, check=True)
assert hashlib.sha256((ROOT / 'YingXu.exe').read_bytes()).hexdigest() == EXE_SHA
record['ok'] = all(step['exit_code'] == 0 for step in record['steps'])
(REPORTS / 'result.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
print(json.dumps(record))
raise SystemExit(0 if record['ok'] else 1)
