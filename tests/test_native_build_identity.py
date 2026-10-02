"""Isolated identity drift checks; no native executable or application is run."""
import ast
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import build_identity as identities
from build_identity import (IdentityError, read_identity, generated_native_source,
                            generated_native_manifest, require_manifest_identity,
                            require_native_identity, native_metadata)


def load_tool(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='yingxu-identity-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'yingxu').mkdir()

    def source(self, value="__version__ = '1.2.4'\n__build__ = 'future.99'\n"):
        (self.root / 'yingxu/__init__.py').write_text(value, encoding='utf-8')
        return self.root

    def test_source_is_read_without_executing_python(self):
        sentinel = self.root / 'must-not-exist'
        self.source("__version__ = '1.2.4'\n__build__ = 'future.99'\n"
                    + "raise RuntimeError('must never execute')\n")
        identity = read_identity(self.root)
        self.assertEqual(('1.2.4', 'future.99'), (identity.version, identity.build_revision))
        self.assertFalse(sentinel.exists())

    def test_missing_dynamic_duplicate_and_nested_writes_are_rejected(self):
        for source in ("__version__='1.2.4'\n",
                       "__version__=str(1)\n__build__='future.99'\n",
                       "__version__='1.2.4'\n__version__='1.2.5'\n__build__='future.99'\n",
                       "__version__='1.2.4'\n__build__='future.99'\nif True:\n __build__='future.100'\n",
                       "__version__,other='1.2.4',0\n__build__='future.99'\n"):
            with self.subTest(source=source):
                self.source(source)
                with self.assertRaises(IdentityError): read_identity(self.root)

    def test_version_native_overflow_and_noncanonical_versions_rejected(self):
        for version in ('65535.1.1', '1.65535.1', '1.1.65535', '01.2.3',
                        '1.2', '1.2.3.4', '1.2.3-beta', '１.2.3', '1.2.3\n'):
            with self.subTest(version=version):
                self.source(f'__version__={version!r}\n__build__="future.99"\n')
                with self.assertRaises(IdentityError): read_identity(self.root)

    def test_build_injection_and_invalid_revision_rejected(self):
        for build in ('mcp.01', 'mcp.-1', 'Mcp.1', 'x.1000000000', 'x.1";bad',
                      'x.1\n', 'x'*33+'.1', '', 'foo', 'x.１'):
            with self.subTest(build=build):
                self.source(f'__version__="1.2.3"\n__build__={build!r}\n')
                with self.assertRaises(IdentityError): read_identity(self.root)

    def test_largest_native_version_and_revision_remain_accepted(self):
        self.source("__version__='65534.65534.65534'\n__build__='future.999999999'\n")
        self.assertEqual('65534.65534.65534.0', read_identity(self.root).assembly_version)

    def test_invalid_encoding_syntax_and_oversized_source_rejected(self):
        for value in (b'\xff\xfe', b'__version__ = !!!', b' '*65537):
            with self.subTest(size=len(value)):
                (self.root / 'yingxu/__init__.py').write_bytes(value)
                with self.assertRaises(IdentityError): read_identity(self.root)

    def test_generated_health_and_attributes_follow_future_source_version(self):
        identity = read_identity(self.source())
        generated = generated_native_source(identity).decode()
        self.assertIn('AssemblyVersion("1.2.4.0")', generated)
        self.assertIn('AssemblyInformationalVersion("1.2.4+future.99")', generated)
        self.assertIn('const string Version = "1.2.4"', generated)
        core = (ROOT / 'desktop/Core.cs').read_text(encoding='utf-8-sig')
        self.assertIn('(version as string) == BuildIdentity.Version', core)
        self.assertNotIn('(version as string) == "0.4.', core)

    def test_manifest_only_application_identity_changes(self):
        identity = read_identity(self.source())
        template = ROOT / 'desktop/app.manifest'
        result = ET.fromstring(generated_native_manifest(template, identity))
        ns = '{urn:schemas-microsoft-com:asm.v1}'
        self.assertEqual('1.2.4.0', result.find(ns+'assemblyIdentity').get('version'))
        dependency = result.find(ns+'dependency/'+ns+'dependentAssembly/'+ns+'assemblyIdentity')
        self.assertEqual('6.0.0.0', dependency.get('version'))
        self.assertIn(b'level="asInvoker"', generated_native_manifest(template, identity))

    def test_manifest_missing_or_wrong_placeholder_rejected(self):
        identity = read_identity(self.source())
        source = (ROOT / 'desktop/app.manifest').read_bytes()
        for value in (source.replace(b'@YINGXU_ASSEMBLY_VERSION@', b'0.4.25.0'),
                      source + b'@YINGXU_ASSEMBLY_VERSION@', b'bad XML'):
            (self.root / 'app.manifest').write_bytes(value)
            with self.assertRaises(IdentityError):
                generated_native_manifest(self.root / 'app.manifest', identity)

    def test_release_version_and_build_must_both_match(self):
        identity = read_identity(self.source())
        require_manifest_identity(dict(version='1.2.4', build_revision='future.99'), identity)
        for bad in (dict(version='0.4.25', build_revision='future.99'),
                    dict(version='1.2.4', build_revision='mcp.1'), {}, None):
            with self.assertRaises(IdentityError): require_manifest_identity(bad, identity)

    def test_verifier_rejects_other_service_version_and_build_before_writes(self):
        verify = load_tool('identity_verify', 'tools/verify_release.py')
        identity = read_identity(self.source())
        data, projects = self.root/'data', self.root/'projects'
        health = dict(version='0.4.25', instance_id=verify.data_identity(data))
        with patch.object(verify, 'wait_health', return_value=health), patch.object(verify, 'request') as request:
            with self.assertRaises(AssertionError):
                verify.check_server(1, data, projects, expected_identity=identity)
            request.assert_not_called()
        health.update(version=identity.version, build_revision=identity.build_revision,
                      program_id=verify.data_identity(ROOT))
        with patch.object(verify, 'wait_health', return_value=health), patch.object(verify, 'request', return_value={'build_revision':'mcp.1'}) as request:
            with self.assertRaises(AssertionError):
                verify.check_server(1, data, projects, expected_identity=identity)
            self.assertEqual(1, request.call_count)
            self.assertEqual('GET', request.call_args.args[1])

    def test_all_native_compiler_branches_receive_same_identity_without_executing(self):
        build = load_tool('identity_native_build', 'desktop/build.py')
        self.source()
        desktop = self.root/'desktop'
        desktop.mkdir()
        (desktop/'app.manifest').write_bytes((ROOT/'desktop/app.manifest').read_bytes())
        sdk = self.root/'sdk.zip'
        members = ('lib/net462/Microsoft.Web.WebView2.Core.dll',
                   'lib/net462/Microsoft.Web.WebView2.WinForms.dll',
                   'runtimes/win-x64/native/WebView2Loader.dll', 'LICENSE.txt')
        with zipfile.ZipFile(sdk, 'w') as archive:
            for name in members: archive.writestr(name, b'synthetic SDK fixture')
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            out = next((item[5:] for item in command if item.startswith('/out:')), None)
            if out:
                source = next(item for item in command if item.endswith('BuildIdentity.cs'))
                self.assertEqual(source, command[-1])
                first_input = next(index for index, value in enumerate(command) if value.endswith('.cs'))
                self.assertFalse(any(value.startswith(('/target:', '/out:')) for value in command[first_input+1:]))
                self.assertIn(b'1.2.4+future.99', Path(source).read_bytes())
                manifest = next((item.split(':',1)[1] for item in command if item.startswith('/win32manifest:')), None)
                if manifest: self.assertIn(b'version="1.2.4.0"', Path(manifest).read_bytes())
                Path(out).write_bytes(b'synthetic non-executable fixture')
            return SimpleNamespace(stdout='', stderr='', check_returncode=lambda: None)
        record = self.root/'build.json'
        arguments = ['build.py','--sdk-package',str(sdk),'--output',str(self.root/'candidate.exe'),
                     '--manifest-output',str(record),'--test']
        with patch.object(build, 'ROOT', self.root), patch.object(build, 'DESKTOP', desktop), \
             patch.object(build, 'SDK_SHA256', hashlib.sha256(sdk.read_bytes()).hexdigest()), \
             patch.object(build, 'require_native_identity', return_value=dict(file_version='1.2.4.0',product_version='1.2.4+future.99')), \
             patch.object(build.subprocess, 'run', side_effect=run), patch.object(sys, 'argv', arguments), redirect_stdout(io.StringIO()):
            build.main()
        compiles = [command for command in commands if any(item.startswith('/out:') for item in command)]
        self.assertEqual(8, len(compiles))
        self.assertEqual('future.99', json.loads(record.read_text())['build_revision'])
        self.assertEqual('1.2.4', json.loads(record.read_text())['version'])

    def test_packager_uses_current_source_identity_in_manifest_and_filenames(self):
        package = load_tool('identity_package', 'tools/package_release.py')
        self.source()
        (self.root/'desktop').mkdir()
        (self.root/'desktop/brand.ico').write_bytes(b'synthetic icon')
        member = self.root/'yingxu/__init__.py'
        target = self.root/'packages'
        with patch.object(package, 'ROOT', self.root), \
             patch.object(package, 'require_native_identity'), \
             patch.object(package, 'files_to_package', return_value=iter([member])), \
             patch.object(package, 'runtime_files', return_value=iter([])), \
             patch.object(sys, 'argv', ['package.py','--runtime-dir',str(self.root/'unused'), '--output-dir',str(target)]), redirect_stdout(io.StringIO()):
            package.main()
        archive_path = target/'YingXu-v1.2.4-Windows-x64.zip'
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read('YingXu/RELEASE_MANIFEST.json'))
        self.assertEqual('future.99', manifest['build_revision'])
        self.assertEqual('1.2.4', manifest['version'])
        self.assertTrue((target/'YingXu-v1.2.4-manifest.json').is_file())

    def test_build_record_binds_exact_identity_source_bytes(self):
        identity = read_identity(self.source())
        metadata = identity.metadata()
        self.assertEqual(hashlib.sha256((self.root/'yingxu/__init__.py').read_bytes()).hexdigest(),
                         metadata['identity_source_sha256'])
        self.source("# harmless source change\n__version__='1.2.4'\n__build__='future.99'\n")
        self.assertNotEqual(identity.source_sha256, read_identity(self.root).source_sha256)

    def test_old_native_version_or_build_rejected_even_with_current_source(self):
        identity = read_identity(self.source())
        for metadata in (dict(file_version='0.4.25.0',product_version='0.4.25+mcp.1'),
                         dict(file_version='1.2.4.0',product_version='1.2.4+future.98')):
            with patch.object(identities, 'native_metadata', return_value=metadata):
                with self.assertRaises(IdentityError): require_native_identity(self.root/'candidate.exe',identity)

    def test_missing_invalid_and_nonwindows_executable_fail_closed(self):
        identity = read_identity(self.source())
        with patch.object(identities.sys, 'platform', 'linux'):
            with self.assertRaisesRegex(IdentityError,'requires Windows'):
                require_native_identity(self.root/'candidate.exe',identity)
        if sys.platform == 'win32':
            with self.assertRaises(IdentityError): require_native_identity(self.root/'missing.exe',identity)
            path = self.root/'invalid.exe'
            path.write_bytes(b'not a PE executable')
            with self.assertRaises(IdentityError): require_native_identity(path,identity)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows read-only native version API')
    def test_actual_candidate_metadata_is_inspected_without_loading_executable(self):
        # Only the explicitly owned Stage I compiled artifact; never installed apps.
        exe = Path(os.environ.get('YINGXU_NATIVE_IDENTITY_TEST_EXECUTABLE', str(ROOT.parent/'compiled/YingXu-StageI-Candidate.exe')))
        if not exe.is_file():
            self.skipTest('No explicitly owned compiled fixture in this source-only candidate.')
        identity = read_identity(ROOT)
        metadata = require_native_identity(exe,identity)
        self.assertEqual(identity.version+'+'+identity.build_revision, metadata['product_version'])

    def test_packager_rejects_native_mismatch_before_creating_archive(self):
        package = load_tool('identity_package_mismatch','tools/package_release.py')
        self.source()
        target = self.root/'must-not-be-created'
        with patch.object(package,'ROOT',self.root), \
             patch.object(package,'require_native_identity',side_effect=IdentityError('native mismatch')), \
             patch.object(sys,'argv',['package.py','--output-dir',str(target)]):
            with self.assertRaises(IdentityError): package.main()
        self.assertFalse(target.exists())

    def test_verifier_rejects_same_version_other_build_or_program_before_writes(self):
        verify = load_tool('identity_verify_service','tools/verify_release.py')
        identity = read_identity(self.source())
        data,projects=self.root/'data',self.root/'projects'
        good=dict(version=identity.version,build_revision=identity.build_revision,
                  program_id=verify.data_identity(ROOT),instance_id=verify.data_identity(data))
        for field,value in (('build_revision','other.1'),('program_id','0'*64)):
            with self.subTest(field=field), patch.object(verify,'wait_health',return_value=dict(good,**{field:value})), \
                 patch.object(verify,'request') as request:
                with self.assertRaises(AssertionError): verify.check_server(1,data,projects,expected_identity=identity)
                request.assert_not_called()

    def test_real_health_binds_build_program_and_data_without_private_paths(self):
        import http.client
        import threading
        from server import Application,Server,ROOT as PROGRAM_ROOT,__version__,__build__
        from yingxu.paths import instance_id
        with patch('yingxu.skills.Path.home',return_value=self.root/'home'):
            app=Application(self.root/'data',self.root/'projects')
        server=Server(('127.0.0.1',0),app)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        try:
            connection.request('GET','/api/health')
            response=connection.getresponse()
            raw=response.read()
            self.assertEqual(200,response.status)
            body=json.loads(raw)
            self.assertEqual(__version__,body['version'])
            self.assertEqual(__build__,body['build_revision'])
            self.assertEqual(instance_id(PROGRAM_ROOT),body['program_id'])
            self.assertEqual(instance_id(self.root/'data'),body['instance_id'])
            self.assertNotIn(str(self.root).encode(),raw)
            self.assertNotIn(str(PROGRAM_ROOT).encode(),raw)
        finally:
            connection.close()
            server.shutdown();server.server_close();thread.join(3)
            app.close()


if __name__ == '__main__':
    unittest.main()
