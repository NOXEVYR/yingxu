"""Real Windows helper/process handoff using only isolated synthetic installs.

Set YINGXU_INSTALLER_TEST_RUNTIME to an existing official runtime directory.
No downloads, process termination, real application data or production installs.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from yingxu import incremental_install as install


REPO = Path(__file__).resolve().parents[1]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


OLD_EXE = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
class SyntheticYingXu {
    [STAThread] static void Main() {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        File.WriteAllText(Path.Combine(root, "native-ready.marker"), Process.GetCurrentProcess().Id.ToString());
        DateTime deadline = DateTime.UtcNow.AddSeconds(90);
        while (!File.Exists(Path.Combine(root, "native-exit.marker")) && DateTime.UtcNow < deadline)
            Thread.Sleep(50);
    }
}
'''

NEW_EXE = r'''
using System;
using System.Diagnostics;
using System.IO;
class SyntheticYingXu {
    [STAThread] static void Main() {
        File.WriteAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "restarted.marker"),
                          Process.GetCurrentProcess().Id.ToString());
    }
}
'''

BACKEND = r'''
import json
from pathlib import Path
import sys
import time
import traceback

repo, root, data, plan_id, native_pid, control = sys.argv[1:]
sys.path.insert(0, repo)
from yingxu import incremental_install as installer
root, data, control = map(Path, (root, data, control))
prepared = None
committed = False

def wait_for(name):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if (control / 'abort.marker').exists():
            return False
        if (control / name).exists():
            return True
        time.sleep(.05)
    raise RuntimeError('synthetic backend marker timeout')

try:
    prepared = installer.prepare_install(data, root, plan_id, int(native_pid))
    installer.write_json(control / 'prepared.json', prepared)
    if wait_for('commit.marker'):
        receipt = installer.commit_install(data, root, prepared['ticket'])
        committed = True
        installer.write_json(control / 'committed.json', receipt)
        wait_for('backend-exit.marker')
except Exception:
    traceback.print_exc()
    installer.write_json(control / 'backend-error.json', {'error': traceback.format_exc()})
    sys.exit(1)
finally:
    if prepared and not committed:
        installer.cancel_install(data, root, prepared['ticket'])
'''


@unittest.skipUnless(os.name == 'nt', 'Real process handoff requires Windows')
class IncrementalProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        value = os.environ.get('YINGXU_INSTALLER_TEST_RUNTIME')
        if not value:
            raise unittest.SkipTest('Set YINGXU_INSTALLER_TEST_RUNTIME to an existing official read-only runtime')
        cls.runtime = Path(value).resolve()
        manifest = cls.runtime / 'RUNTIME_MANIFEST.json'
        if not manifest.is_file():
            raise unittest.SkipTest('Official runtime manifest is unavailable')
        cls.runtime_manifest = json.loads(manifest.read_bytes())
        cls.runtime_records = [entry for entry in cls.runtime_manifest['files']
                               if '/' not in entry['path'] and Path(entry['path']).suffix.lower() in
                               {'.exe', '.dll', '.pyd', '.zip', '._pth'}]
        if not cls.runtime_records or sum(entry['bytes'] for entry in cls.runtime_records) > 48 * 1024**2:
            raise unittest.SkipTest('Official minimal runtime is unavailable or exceeds helper limit')
        windir = Path(os.environ.get('WINDIR', r'C:\Windows'))
        compiler = windir / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        if not compiler.is_file():
            compiler = windir / 'Microsoft.NET/Framework/v4.0.30319/csc.exe'
        if not compiler.is_file():
            raise unittest.SkipTest('System .NET Framework compiler is unavailable')
        cls.build_temp = tempfile.TemporaryDirectory(prefix='yingxu-process-build-')
        cls.addClassCleanup(cls.build_temp.cleanup)
        cls.build = Path(cls.build_temp.name).resolve()
        for name, source in [('old', OLD_EXE), ('new', NEW_EXE)]:
            code = cls.build / (name + '.cs')
            code.write_text(source, encoding='utf-8')
            result = subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:x64',
                                     '/out:' + str(cls.build / (name + '.exe')), str(code)],
                                    capture_output=True, text=True, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise RuntimeError('Synthetic executable build failed: ' + result.stdout + result.stderr)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yingxu-process-handoff-')
        self.base = Path(self.temp.name).resolve()
        self.root, self.data, self.control = (self.base / name for name in ('program', 'data', 'control'))
        for path in (self.root, self.data, self.control):
            path.mkdir()
        self.processes = []
        self.helper = None
        self.job = None
        self.log = None
        self.plan_id = 'c' * 32
        self.folder = self.data / 'updates/incremental' / self.plan_id
        self.stage = self.folder / 'stage'
        self.stage.mkdir(parents=True)
        self._fixture()

    def tearDown(self):
        # All children are test-owned and have cooperative exit controls. Never
        # terminate/kill an application to make this test finish or pass.
        (self.root / 'native-exit.marker').touch()
        (self.control / 'backend-exit.marker').touch()
        (self.control / 'abort.marker').touch()
        if self.job and self.job.exists() and not (self.job / 'outcome.json').exists():
            install.write_json(self.job / 'cancel.json', {'ticket': self.job.name, 'cancelled': True})
        cleanup_errors = []
        for child in self.processes:
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                cleanup_errors.append('synthetic child did not exit cooperatively')
        if self.helper:
            try:
                self._wait(lambda: self.helper.exited(), 20, 'test helper exit')
            except AssertionError as error:
                cleanup_errors.append(str(error))
            finally:
                self.helper.close()
        if self.log:
            self.log.close()
        if cleanup_errors:
            # Leave the isolated directory intact if its test process remains
            # alive, rather than deleting files under a running process.
            self.temp._finalizer.detach()
            self.fail('; '.join(cleanup_errors))
        self.temp.cleanup()

    def _fixture(self):
        old_core = {'YingXu.exe': (self.build / 'old.exe').read_bytes(), 'server.py': b'old synthetic server',
                    'launcher.pyw': b'unchanged launcher', 'frontend/index.html': b'unchanged interface',
                    'frontend/obsolete.js': b'old obsolete program'}
        new_core = dict(old_core, **{'YingXu.exe': (self.build / 'new.exe').read_bytes(),
                                    'server.py': b'new synthetic server', 'yingxu/new.py': b'new module'})
        del new_core['frontend/obsolete.js']
        for name, raw in old_core.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        runtime = self.root / 'runtime'
        runtime.mkdir()
        runtime_entries = []
        for entry in self.runtime_records:
            source = self.runtime / entry['path']
            self.assertEqual(install.digest(source), entry['sha256'])
            self.assertEqual(source.stat().st_size, entry['bytes'])
            shutil.copy2(source, runtime / entry['path'])
            runtime_entries.append(dict(path='runtime/' + entry['path'], bytes=entry['bytes'], sha256=entry['sha256']))
        subset = dict(self.runtime_manifest, files=self.runtime_records)
        runtime_manifest = json.dumps(subset).encode('utf-8')
        (runtime / 'RUNTIME_MANIFEST.json').write_bytes(runtime_manifest)
        runtime_entries.append(dict(path='runtime/RUNTIME_MANIFEST.json', bytes=len(runtime_manifest), sha256=sha(runtime_manifest)))

        def release(core, version):
            entries = [dict(path=name, bytes=len(raw), sha256=sha(raw)) for name, raw in core.items()] + runtime_entries
            return json.dumps(dict(application='YingXu', root='YingXu/', architecture='Windows x64',
                                   version=version, files=entries)).encode('utf-8')

        old_manifest = release(old_core, '0.4.18')
        new_manifest = release(new_core, '0.4.19')
        (self.root / 'RELEASE_MANIFEST.json').write_bytes(old_manifest)
        changes, reused = [], list(runtime_entries)
        for name, raw in dict(new_core, **{'RELEASE_MANIFEST.json': new_manifest}).items():
            prior = old_manifest if name == 'RELEASE_MANIFEST.json' else old_core.get(name)
            entry = dict(path=name, bytes=len(raw), sha256=sha(raw))
            if prior == raw:
                reused.append(entry)
            else:
                entry['expected_old_sha256'] = sha(prior) if prior is not None else None
                changes.append(entry)
                destination = self.stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
        plan = dict(schema=1, id=self.plan_id, install_root=str(self.root), current_version='0.4.18', version='0.4.19',
                    state='ready', old_manifest_sha256=sha(old_manifest), manifest_sha256=sha(new_manifest),
                    changes=changes, reused=reused,
                    removed=[dict(path='frontend/obsolete.js', expected_old_sha256=sha(old_core['frontend/obsolete.js']))])
        install.write_json(self.folder / 'plan.json', plan)
        (self.root / 'private-note.md').write_bytes(b'untouched user fixture')
        self.old_exe = sha(old_core['YingXu.exe'])
        self.new_exe = sha(new_core['YingXu.exe'])
        self.reused_before = {row['path']: install.fingerprint(self.root / row['path']) for row in reused}
        self.backend_script = self.control / 'backend.py'
        self.backend_script.write_text(BACKEND, encoding='utf-8')

    def _wait(self, predicate, timeout, label):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if (self.control / 'backend-error.json').exists():
                self.fail('Backend failed: ' + (self.control / 'backend-error.json').read_text(encoding='utf-8'))
            time.sleep(.05)
        self.fail('Timeout waiting for ' + label)

    def _still_original(self, label):
        # Exercise the helper's 100 ms polling path repeatedly while exactly the
        # selected old process remains alive, instead of checking just one instant.
        deadline = time.monotonic() + .6
        while time.monotonic() < deadline:
            self.assertEqual(install.digest(self.root / 'YingXu.exe'), self.old_exe, label)
            self.assertEqual((self.root / 'server.py').read_bytes(), b'old synthetic server', label)
            self.assertFalse((self.root / 'restarted.marker').exists(), label)
            self.assertFalse((self.job / 'journal.json').exists(), label)
            time.sleep(.05)

    def _exercise(self, first):
        native = subprocess.Popen([str(self.root / 'YingXu.exe')], cwd=self.root, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        self.processes.append(native)
        self._wait(lambda: (self.root / 'native-ready.marker').exists(), 10, 'synthetic native startup')
        self.log = (self.control / 'backend.log').open('wb')
        # Match the complete distribution: both backend and helper use its
        # official bundled Python, including Windows filesystem identity ABI.
        backend = subprocess.Popen([str(self.root / 'runtime/python.exe'), '-I', '-B', str(self.backend_script), str(REPO), str(self.root),
                                    str(self.data), self.plan_id, str(native.pid), str(self.control)],
                                   cwd=self.control, stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        self.processes.append(backend)
        self._wait(lambda: (self.control / 'prepared.json').exists(), 25, 'independent helper ready')
        prepared = install.read_json(self.control / 'prepared.json')
        self.job = self.data / 'updates/incremental-install' / prepared['ticket']
        self.helper = install.ProcessGuard(prepared['helper_pid'], self.job / 'helper/python.exe')
        self.assertFalse(self.helper.exited())
        self.assertEqual(prepared['native_pid'], native.pid)
        self.assertEqual(prepared['backend_pid'], backend.pid)
        self.assertNotEqual(prepared['helper_pid'], backend.pid)
        (self.control / 'commit.marker').touch()
        self._wait(lambda: (self.control / 'committed.json').exists(), 10, 'install commit receipt')
        self.assertTrue(install.read_json(self.control / 'committed.json')['committed'])
        self._still_original('both old processes alive')

        if first == 'native':
            (self.root / 'native-exit.marker').touch()
            self.assertEqual(native.wait(timeout=10), 0)
            self.assertIsNone(backend.poll())
            self._still_original('native exited; backend still alive')
            (self.control / 'backend-exit.marker').touch()
            self.assertEqual(backend.wait(timeout=10), 0)
        else:
            (self.control / 'backend-exit.marker').touch()
            self.assertEqual(backend.wait(timeout=10), 0)
            self.assertIsNone(native.poll())
            self._still_original('backend exited; native still alive')
            (self.root / 'native-exit.marker').touch()
            self.assertEqual(native.wait(timeout=10), 0)

        self._wait(lambda: (self.root / 'restarted.marker').exists(), 25, 'new executable automatic startup')
        self._wait(lambda: self.helper.exited(), 10, 'independent helper completion')
        outcome = install.read_json(self.job / 'outcome.json')
        self.assertEqual(outcome['state'], 'installed', outcome)
        self.assertTrue(outcome['restarted'], outcome)
        self.assertEqual(install.digest(self.root / 'YingXu.exe'), self.new_exe)
        self.assertEqual((self.root / 'server.py').read_bytes(), b'new synthetic server')
        self.assertEqual((self.root / 'yingxu/new.py').read_bytes(), b'new module')
        self.assertFalse((self.root / 'frontend/obsolete.js').exists())
        self.assertEqual((self.root / 'private-note.md').read_bytes(), b'untouched user fixture')
        for name, previous in self.reused_before.items():
            self.assertEqual(install.fingerprint(self.root / name), previous, name)
        self.assertEqual(install.read_json(self.root / 'RELEASE_MANIFEST.json')['version'], '0.4.19')
        self.assertEqual(install.read_json(self.job / 'journal.json')['state'], 'installed')
        self.assertEqual(install.digest(self.job / 'backup/YingXu.exe'), self.old_exe)
        self._wait(lambda: not install.installation_processes(self.root), 10, 'synthetic restarted executable exit')
        print(json.dumps(dict(test='real-windows-incremental-handoff', first_exit=first,
                              native_pid=native.pid, backend_pid=backend.pid, helper_pid=prepared['helper_pid'],
                              state=outcome['state'], restarted=outcome['restarted'],
                              changed_files=outcome['changed_files'], unchanged_files=len(self.reused_before),
                              runtime_bytes=sum(row['bytes'] for row in self.runtime_records),
                              user_fixture_preserved=True, no_process_terminated=True)), flush=True)

    def test_native_exit_first_waits_for_backend_then_installs_and_restarts(self):
        self._exercise('native')

    def test_backend_exit_first_waits_for_native_then_installs_and_restarts(self):
        self._exercise('backend')


if __name__ == '__main__':
    unittest.main()
