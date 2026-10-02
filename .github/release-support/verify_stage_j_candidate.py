"""Verify the explicit Stage J candidate in a disposable Windows runner."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from build_identity import read_identity

IDENTITY = read_identity(ROOT)
assert (IDENTITY.version, IDENTITY.build_revision) == ('0.4.27', 'calls.3')
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip() == os.environ['GITHUB_SHA']
subprocess.run(['git', 'diff', '--exit-code', 'HEAD', '--'], cwd=ROOT, check=True)
REPORTS = ROOT / '.release-work/stage-j-verification'
REPORTS.mkdir(parents=True, exist_ok=False)
FIXTURE = Path('C:/yxj') / uuid.uuid4().hex[:8]
for name in ('temp', 'home', 'local', 'roaming', 'data', 'projects'):
    (FIXTURE / name).mkdir(parents=True, exist_ok=False)
ENV = os.environ.copy()
ENV.update(TEMP=str(FIXTURE / 'temp'), TMP=str(FIXTURE / 'temp'),
           USERPROFILE=str(FIXTURE / 'home'), HOME=str(FIXTURE / 'home'),
           LOCALAPPDATA=str(FIXTURE / 'local'), APPDATA=str(FIXTURE / 'roaming'),
           YINGXU_DATA_DIR=str(FIXTURE / 'data'), YINGXU_PROJECTS_DIR=str(FIXTURE / 'projects'),
           YINGXU_INSTALLER_TEST_RUNTIME=str(ROOT / 'runtime'),
           PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
for key in ('YINGXU_RESUME_SESSION_TOKEN', 'YINGXU_PYTHON', 'PYTHONHOME', 'PYTHONPATH'):
    ENV.pop(key, None)
RECORD = {'source_commit': os.environ['GITHUB_SHA'], 'version': IDENTITY.version,
          'build_revision': IDENTITY.build_revision, 'private_data_used': False,
          'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'steps': []}


def run(name, command, timeout=240):
    stdout, stderr = REPORTS / (name + '.stdout.txt'), REPORTS / (name + '.stderr.txt')
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with stdout.open('wb') as out, stderr.open('wb') as err:
        try:
            result = subprocess.run(command, cwd=ROOT, env=ENV, stdout=out, stderr=err,
                                    timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            code, timed_out = result.returncode, False
        except subprocess.TimeoutExpired:
            code, timed_out = None, True
    step = {'name': name, 'exit_code': code, 'timeout': timed_out, 'command': command,
            'started_at': started, 'stdout': stdout.name, 'stderr': stderr.name}
    RECORD['steps'].append(step)
    (REPORTS / 'result.json').write_text(json.dumps(RECORD, indent=2), encoding='utf-8')
    print(json.dumps(step), flush=True)
    return step


runtime_python = str(ROOT / 'runtime/python.exe')
modules = sorted((ROOT / 'tests').glob('test_*.py'))
for test in modules:
    step = run(test.stem, [runtime_python, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', test.name, '-v'], 180)
    raw = (REPORTS / step['stderr']).read_text(encoding='utf-8', errors='replace')
    ran = re.search(r'Ran (\d+) tests? in', raw)
    skipped = re.search(r'skipped=(\d+)', raw)
    step.update(tests=int(ran[1]) if ran else None, skipped=int(skipped[1]) if skipped else 0)
    if test.name == 'test_incremental_support.py' and step['tests'] == 0:
        step['kind'] = 'synthetic-fixture-support-module'
        # Python 3.13 returns 5 for an empty discovery. This file defines only
        # shared fixture helpers; keep its raw result without inventing a test.
        if step['exit_code'] == 5 and raw.strip().endswith('NO TESTS RAN'):
            step['raw_exit_code'] = 5
            step['exit_code'] = 0
    elif step['exit_code'] == 0 and not step['tests']:
        step['exit_code'] = 'missing-test-count'
failed = [s for s in RECORD['steps'] if s['exit_code'] != 0]
RECORD['backend'] = {'modules': len(modules), 'tests': sum(s['tests'] or 0 for s in RECORD['steps']),
                     'skipped': sum(s['skipped'] for s in RECORD['steps']), 'failed_modules': [s['name'] for s in failed]}
(REPORTS / 'result.json').write_text(json.dumps(RECORD, indent=2), encoding='utf-8')
if failed:
    for step in failed:
        print((REPORTS / step['stderr']).read_text(encoding='utf-8', errors='replace'))
    raise SystemExit('Backend candidate verification failed')
sdk = str(Path(os.environ['RUNNER_TEMP']) / 'yingxu-candidate-verify/webview2.nupkg')
for name, command, timeout in (
    ('native', [sys.executable, '-B', 'desktop/build.py', '--sdk-package', sdk, '--output', 'YingXu.exe', '--test', '--isolated-test-units'], 600),
    ('stage-j-host', [sys.executable, '-B', '.github/release-support/run_stage_j_host_checks.py', '--sdk-package', sdk, '--output-dir', str(REPORTS / 'host')], 240),
    ('package', [sys.executable, '-B', 'tools/package_release.py'], 600),
    ('complete-package', [sys.executable, '-B', 'tools/verify_release.py', 'releases/YingXu-v0.4.27-Windows-x64.zip'], 240),
):
    step = run(name, command, timeout)
    if step['exit_code'] != 0:
        print((REPORTS / step['stderr']).read_text(encoding='utf-8', errors='replace'))
        raise SystemExit(name + ' verification failed')
verification = json.loads((REPORTS / 'complete-package.stdout.txt').read_text(encoding='utf-8'))
assert verification['ok'] is True and verification['source_commit'] == os.environ['GITHUB_SHA']
subprocess.run(['git', 'diff', '--exit-code', 'HEAD', '--'], cwd=ROOT, check=True)
(ROOT / 'releases/YingXu-v0.4.27-verification.json').write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding='utf-8')
RECORD.update(ok=True, completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
              exe_sha256=hashlib.sha256((ROOT / 'YingXu.exe').read_bytes()).hexdigest())
(REPORTS / 'result.json').write_text(json.dumps(RECORD, indent=2), encoding='utf-8')
print(json.dumps(RECORD['backend']), flush=True)
