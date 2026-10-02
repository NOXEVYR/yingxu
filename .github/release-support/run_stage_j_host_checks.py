"""Run bounded Stage J UI checks against the exact built Windows host in synthetic state."""
import argparse
import ctypes
from ctypes import wintypes
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from build_identity import read_identity, require_native_identity
sys.path.insert(0, str(ROOT))
from yingxu.paths import instance_id

SDK_SHA256 = 'f492bbf547d0da329553b6727435b677579b1e9f91cc9e4a1ad029366d5f23d0'
SDK_MEMBERS = {
    'Microsoft.Web.WebView2.Core.dll': 'lib/net462/Microsoft.Web.WebView2.Core.dll',
    'Microsoft.Web.WebView2.WinForms.dll': 'lib/net462/Microsoft.Web.WebView2.WinForms.dll',
    'WebView2Loader.dll': 'runtimes/win-x64/native/WebView2Loader.dll',
}


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def request_json(port, path, *, token=None, payload=None, method=None):
    url = 'http://127.0.0.1:%d%s' % (port, path)
    headers = {'Accept': 'application/json', 'Origin': 'http://127.0.0.1:%d' % port}
    body = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if token:
        headers['X-YingXu-Token'] = token
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=8) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Synthetic API response exceeded the fixture bound')
        value = json.loads(raw.decode('utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Synthetic API response was not an object')
        return value


def process_metadata(pid):
    """Read exact image path and creation time for a PID using query-only access."""
    if os.name != 'nt':
        raise RuntimeError('Stage J production host checks require Windows')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise OSError(ctypes.get_last_error(), 'OpenProcess query failed')
    try:
        return process_metadata_from_handle(kernel, handle, pid)
    finally:
        kernel.CloseHandle(handle)


def process_metadata_from_handle(kernel, handle, pid):
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
    kernel.GetProcessTimes.restype = wintypes.BOOL
    capacity = wintypes.DWORD(32768)
    image = ctypes.create_unicode_buffer(capacity.value)
    if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(capacity)):
        raise OSError(ctypes.get_last_error(), 'QueryFullProcessImageNameW failed')
    creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
        raise OSError(ctypes.get_last_error(), 'GetProcessTimes failed')
    created = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    return {'pid': int(pid), 'image': os.path.normcase(os.path.abspath(image.value)), 'created_filetime': created}


def terminate_owned_server(server_record, expected, metadata):
    if not server_record:
        return {'status': 'no-owned-server-record'}
    record_path, record = server_record
    if (record.get('app') != 'yingxu' or int(record.get('pid', -1)) != expected['pid']
            or int(record.get('port', -1)) != expected['port']
            or os.path.normcase(os.path.abspath(record.get('root', ''))) != expected['root']):
        raise RuntimeError('Synthetic server pid record changed; refusing to stop it')
    current = process_metadata(expected['pid'])
    if current != metadata:
        raise RuntimeError('Synthetic server PID/image/creation time changed; refusing to stop it')
    health = request_json(expected['port'], '/api/health')
    if health != expected['health']:
        raise RuntimeError('Synthetic server health identity changed; refusing to stop it')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x0001 | 0x1000 | 0x00100000, False, expected['pid'])
    if not handle:
        raise OSError(ctypes.get_last_error(), 'OpenProcess terminate failed')
    try:
        same_handle = process_metadata_from_handle(kernel,handle,expected['pid'])
        if same_handle != metadata:
            raise RuntimeError('Final terminate handle belongs to a different PID/image/creation time; refusing to stop it')
        if not kernel.TerminateProcess(handle, 0):
            raise OSError(ctypes.get_last_error(), 'TerminateProcess failed for verified synthetic server')
        if kernel.WaitForSingleObject(handle, 8000) != 0:
            raise TimeoutError('Verified synthetic backend did not exit after termination request')
    finally:
        kernel.CloseHandle(handle)
    return {'status': 'terminated-verified-synthetic-backend', 'pid': expected['pid'],
            'image': current['image'], 'created_filetime': current['created_filetime'],
            'port': expected['port'], 'pid_record': str(record_path)}


def adopt_synthetic_backend(data, root, runtime_python, port, version, build, launched_at_ns):
    """Register cleanup only from the fresh fixture pid record; fail closed on ambiguity."""
    record_path = data / 'server.pid.json'
    if not record_path.exists() or record_path.is_symlink():
        return None, None, None, 'no-fresh-ordinary-server-pid-record'
    try:
        value = json.loads(record_path.read_text(encoding='utf-8'))
        if record_path.stat().st_mtime_ns < launched_at_ns:
            return (record_path,value), None, None, 'pid-record-predates-this-launch'
        owned = (record_path,value)
        if value.get('app') != 'yingxu' or int(value.get('port',-1)) != port or os.path.normcase(os.path.abspath(value.get('root',''))) != os.path.normcase(os.path.abspath(root)):
            return owned, None, None, 'pid-record-root-or-port-does-not-match-fixture'
        metadata = process_metadata(value['pid'])
        expected_image = os.path.normcase(os.path.abspath(runtime_python))
        if metadata['image'] != expected_image:
            return owned, metadata, None, 'pid-image-is-not-candidate-bundled-python'
        health = request_json(port,'/api/health')
        if (health.get('app')!='yingxu' or health.get('ok') is not True or health.get('version')!=version
                or health.get('build_revision')!=build or health.get('program_id')!=instance_id(root)
                or health.get('instance_id')!=instance_id(data)):
            return owned, metadata, None, 'live-health-identity-is-not-this-candidate-and-data-root'
        expected={'pid':int(value['pid']),'port':port,'root':os.path.normcase(os.path.abspath(root)),'health':health}
        return owned, metadata, expected, None
    except Exception as error:
        return None, None, None, 'pid-record-or-process-identity-unreadable: '+repr(error)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk-package', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('Stage J host checks require a disposable Windows runner.')
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sdk_dir = output / 'verified-sdk'
    native_dir = output / 'native'
    native_dir.mkdir()
    record = {'source_commit': os.environ.get('GITHUB_SHA', ''), 'version': None, 'build_revision': None,
              'private_data_used': False, 'checks': [], 'screenshots': {}, 'synthetic_paths': {}, 'steps': []}
    fixture = None
    owned = None
    server_metadata = None
    server_expected = None
    backend_start_attempted = False
    host_process = None
    error = None
    try:
        if not record['source_commit']:
            raise ValueError('GITHUB_SHA is required for a frozen candidate host run')
        if subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip() != record['source_commit']:
            raise ValueError('Candidate HEAD differs from GITHUB_SHA')
        identity = read_identity(ROOT)
        if (identity.version, identity.build_revision) != ('0.4.27', 'calls.3'):
            raise ValueError('Candidate is not the selected 0.4.27/calls.3 build')
        record.update(version=identity.version, build_revision=identity.build_revision)
        exe = ROOT / 'YingXu.exe'
        require_native_identity(exe, identity)
        record['checks'].append('final-native-exe-identity')
        record['exe_sha256'] = sha256(exe.read_bytes())
        sdk_path = args.sdk_package.resolve()
        if sha256(sdk_path.read_bytes()) != SDK_SHA256:
            raise ValueError('SDK package SHA-256 does not match desktop/build.py official pin')
        sdk_dir.mkdir()
        with zipfile.ZipFile(sdk_path) as archive:
            if archive.testzip() is not None:
                raise ValueError('Official SDK package ZIP integrity check failed')
            for name, member in SDK_MEMBERS.items():
                raw = archive.read(member)
                target = sdk_dir / name
                target.write_bytes(raw)
                record.setdefault('sdk_resources', {})[name] = {'sha256': sha256(raw), 'bytes': len(raw)}
        record['checks'].append('official-sdk-pinned-package-and-members')

        fixture_base = Path(os.environ.get('TEMP') or tempfile.gettempdir())
        fixture = Path(tempfile.mkdtemp(prefix='sj-' + uuid.uuid4().hex[:8] + '-', dir=str(fixture_base)))
        data, projects = fixture / 'data', fixture / 'projects'
        temp, home, local, roaming = fixture / 'temp', fixture / 'home', fixture / 'local', fixture / 'roaming'
        for path in (data, projects, temp, home, local, roaming):
            path.mkdir(parents=True, exist_ok=False)
        record['synthetic_paths'] = {key: str(value) for key, value in {'fixture':fixture,'data':data,'projects':projects,'temp':temp,'home':home,'local_appdata':local,'appdata':roaming}.items()}
        env = os.environ.copy()
        env.update(TEMP=str(temp), TMP=str(temp), USERPROFILE=str(home), HOME=str(home), LOCALAPPDATA=str(local), APPDATA=str(roaming),
                   YINGXU_DATA_DIR=str(data), YINGXU_PROJECTS_DIR=str(projects), YINGXU_WEBVIEW2_MODE='bundled', PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
        for key in ('YINGXU_PYTHON','YINGXU_RESUME_SESSION_TOKEN','PYTHONHOME','PYTHONPATH'):
            env.pop(key, None)
        runtime_python = ROOT / 'runtime/python.exe'
        launcher = ROOT / 'launcher.pyw'
        if not runtime_python.is_file() or not (ROOT / 'runtime/webview2/msedgewebview2.exe').is_file():
            raise FileNotFoundError('Candidate bundled Python/WebView2 runtime is missing')
        browser = ROOT / 'runtime/webview2/msedgewebview2.exe'
        runtime_manifest = json.loads((ROOT/'runtime/RUNTIME_MANIFEST.json').read_text(encoding='utf-8'))
        runtime_lock = json.loads((ROOT/'tools/runtime-lock.json').read_text(encoding='utf-8'))
        if runtime_manifest.get('sources') != runtime_lock:
            raise RuntimeError('Bundled runtime source lock differs from the pinned release lock')
        browser_member = next((item for item in runtime_manifest.get('files',[]) if item.get('path')=='webview2/msedgewebview2.exe'),None)
        if browser_member is None or browser_member.get('bytes')!=browser.stat().st_size or browser_member.get('sha256')!=sha256(browser.read_bytes()):
            raise RuntimeError('Bundled WebView2 executable does not match its official runtime manifest')
        record['runtime'] = {'python':str(runtime_python),'webview2':str(browser),'webview2_sha256':sha256(browser.read_bytes()),'manifest_sha256':browser_member['sha256']}
        record['checks'].append('bundled-webview2-runtime-manifest-identity')
        port = free_port()
        launcher_stdout, launcher_stderr = output / 'launcher.stdout.txt', output / 'launcher.stderr.txt'
        backend_start_attempted = True
        launch_started_ns = time.time_ns()
        launch_code = None
        with launcher_stdout.open('wb') as out, launcher_stderr.open('wb') as err:
            try:
                launch = subprocess.run([str(runtime_python), '-B', '-u', str(launcher), '--no-browser', '--port', str(port)],
                                        cwd=ROOT, env=env, stdout=out, stderr=err, timeout=35,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                launch_code = launch.returncode
            except subprocess.TimeoutExpired:
                time.sleep(2)
        owned, server_metadata, server_expected, adopt_error = adopt_synthetic_backend(
            data,ROOT,runtime_python,port,identity.version,identity.build_revision,launch_started_ns)
        record['backend_adoption']={'status':'verified' if server_expected else 'refused-to-adopt',
                                    'reason':adopt_error,'pid':server_metadata['pid'] if server_metadata else None,
                                    'image':server_metadata['image'] if server_metadata else None,
                                    'created_filetime':server_metadata['created_filetime'] if server_metadata else None}
        record['steps'].append({'name':'synthetic-backend-start','exit_code':launch_code,
                                'stdout':launcher_stdout.name,'stderr':launcher_stderr.name,'port':port})
        if launch_code is None:
            record['backend_adoption'].update(status='launcher-timeout')
            raise TimeoutError('Candidate bundled launcher exceeded its 35 second bound')
        if launch_code != 0:
            record['backend_adoption'].update(status='launcher-failed')
            raise RuntimeError('Candidate bundled launcher failed in synthetic workspace')
        launch_result = json.loads(launcher_stdout.read_text(encoding='utf-8'))
        pid_record_path = data / 'server.pid.json'
        pid_record = json.loads(pid_record_path.read_text(encoding='utf-8'))
        if launch_result.get('status') != 'started' or int(launch_result.get('pid', -1)) != int(pid_record.get('pid', -2)):
            raise RuntimeError('Backend was not started by this fresh synthetic fixture')
        health = request_json(port, '/api/health')
        expected_health = {'app':'yingxu','ok':True,'version':identity.version,'build_revision':identity.build_revision}
        if any(health.get(key) != value for key,value in expected_health.items()):
            raise RuntimeError('Synthetic backend health identity does not match the candidate')
        if os.path.normcase(os.path.abspath(pid_record.get('root',''))) != os.path.normcase(os.path.abspath(ROOT)) or int(pid_record.get('port',-1)) != port:
            raise RuntimeError('Synthetic backend pid record is not bound to this candidate and port')
        if server_expected is None or server_metadata is None:
            raise RuntimeError('Synthetic backend identity could not be safely adopted: '+str(adopt_error))
        if int(pid_record.get('pid',-1)) != server_expected['pid']:
            raise RuntimeError('Backend startup PID differs from its safely adopted process identity')
        record['backend'] = {'pid':server_metadata['pid'],'image':server_metadata['image'],
                             'created_filetime':server_metadata['created_filetime'],'health':health}
        record['checks'].extend(['owned-synthetic-backend-identity','health-version-build-program-data'])

        bootstrap = request_json(port, '/api/bootstrap')
        token = bootstrap.get('token')
        if not isinstance(token, str) or not token:
            raise RuntimeError('Synthetic bootstrap did not supply its request token')
        request_json(port, '/api/settings', token=token, method='PATCH',
                     payload={'capture_enabled':False,'automatic_update_check':False,
                              'automatic_update_download':False,'close_to_tray':False})
        record['checks'].append('synthetic-settings-disable-hotkeys-and-update-checks')
        suffix = uuid.uuid4().hex[:10]
        project_name = 'Stage J synthetic project ' + suffix
        skill_name = 'StageJ-Skill-' + suffix
        task_name = 'Stage J synthetic collaboration ' + suffix
        project = request_json(port, '/api/projects', token=token, payload={'name':project_name,'description':'Synthetic Stage J host validation only.'})
        skill = request_json(port, '/api/skills', token=token,
                             payload={'name':skill_name,'description':'Synthetic Stage J UI fixture.',
                                      'content':'# '+skill_name+'\n\nThis synthetic fixture is displayed only.\n'})
        request_json(port, '/api/skills/bind', token=token,
                     payload={'project_id':project['id'],'skill_id':skill['id'],'bound':True})
        task = request_json(port, '/api/ai-tasks', token=token,
                            payload={'project_id':project['id'],'title':task_name,'kind':'skill_test',
                                     'goal':'Display the synthetic task in the production collaboration workspace; no AI run is created.',
                                     'acceptance':['Synthetic page is rendered.'],'idempotency_key':uuid.uuid4().hex})
        if task.get('status') not in ('待开始','未开始','pending'):
            raise RuntimeError('Synthetic collaboration task was not left unstarted')
        record['synthetic_fixture'] = {'project_id':project['id'],'project_name':project_name,'skill_id':skill['id'],
                                       'skill_name':skill_name,'task_id':task['id'],'task_name':task_name,
                                       'ai_runs_created':0,'external_ai_calls':0}
        record['checks'].append('synthetic-project-skill-unrun-collaboration-task')

        compiler = Path(os.environ.get('WINDIR',r'C:\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        if not compiler.is_file():
            raise FileNotFoundError('Windows .NET Framework C# compiler was not found')
        source = ROOT / '.github/release-support/StageJHostChecks.cs'
        harness = native_dir / 'stage-j-host-checks.exe'
        compile_command = [str(compiler),'/nologo','/utf8output','/platform:x64','/target:exe','/out:'+str(harness),
                           '/reference:System.dll','/reference:System.Core.dll','/reference:System.Drawing.dll',
                           '/reference:System.Web.Extensions.dll','/reference:System.Windows.Forms.dll',str(source)]
        c_out, c_err = output / 'host-compile.stdout.txt', output / 'host-compile.stderr.txt'
        with c_out.open('wb') as out, c_err.open('wb') as err:
            compiled = subprocess.run(compile_command,cwd=ROOT,env=env,stdout=out,stderr=err,timeout=45,
                                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        record['steps'].append({'name':'compile-reflection-host-harness','exit_code':compiled.returncode,
                                'stdout':c_out.name,'stderr':c_err.name,'command':compile_command})
        if compiled.returncode != 0:
            raise RuntimeError('Stage J reflection harness did not compile; see host-compile output')
        args_native = [str(harness),str(ROOT),str(exe),str(sdk_dir),str(native_dir),str(port),str(data),str(projects),
                       project['id'],skill_name,task_name,identity.version,identity.build_revision]
        host_stdout, host_stderr = output / 'host.stdout.txt', output / 'host.stderr.txt'
        with host_stdout.open('wb') as out, host_stderr.open('wb') as err:
            host_process = subprocess.Popen(args_native,cwd=ROOT,env=env,stdout=out,stderr=err,
                                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                host_code = host_process.wait(timeout=180)
            except subprocess.TimeoutExpired:
                host_process.terminate()
                try: host_process.wait(timeout=8)
                except subprocess.TimeoutExpired: host_process.kill(); host_process.wait(timeout=5)
                raise TimeoutError('Stage J production host exceeded its 180 second bound')
        record['steps'].append({'name':'final-exe-production-studio-window','exit_code':host_code,
                                'stdout':host_stdout.name,'stderr':host_stderr.name,'command':args_native})
        native_result_path = native_dir / 'native-result.json'
        if not native_result_path.is_file():
            raise RuntimeError('Native host did not write its result record')
        native_result = json.loads(native_result_path.read_text(encoding='utf-8'))
        # Keep the bounded, synthetic-only startup evidence in the top-level
        # report even when pageReady fails. The C# harness strips URL query
        # strings and copies only allow-listed startup log fields.
        record['native_result'] = native_result
        record['host_diagnostics'] = native_result.get('host_diagnostics')
        if host_code != 0 or native_result.get('ok') is not True:
            raise RuntimeError('Final EXE StudioWindow validation failed; inspect native_result.host_diagnostics')
        record['checks'].extend(native_result.get('checks',[]))
        for name,digest in native_result.get('screenshots_sha256',{}).items():
            shot = native_dir / name
            if not shot.is_file() or sha256(shot.read_bytes()) != digest:
                raise RuntimeError('Synthetic screenshot hash/bytes do not match host result: '+name)
            record['screenshots'][name] = {'path':str(shot),'sha256':digest,'bytes':shot.stat().st_size}
        if len(record['screenshots']) != 5:
            raise RuntimeError('Expected four classic/focus pages and one narrow tools screenshot')
        record['checks'].append('screenshots-hashed-and-verified')
    except Exception as exc:
        error = exc
        record['error'] = str(exc)
        record['traceback'] = traceback.format_exc()
    finally:
        if host_process is not None and host_process.poll() is None:
            try: host_process.terminate(); host_process.wait(timeout=5)
            except Exception:
                try: host_process.kill(); host_process.wait(timeout=5)
                except Exception as cleanup_error: record['host_cleanup_error'] = repr(cleanup_error)
        try:
            if owned and server_expected and server_metadata:
                record['backend_cleanup'] = terminate_owned_server(owned,server_expected,server_metadata)
                record['checks'].append('verified-own-synthetic-backend-stopped')
            elif backend_start_attempted:
                record['backend_cleanup'] = {'status':'refused-or-incomplete-no-proven-identity',
                                             'reason':record.get('backend_adoption',{}).get('cleanup_identity','no-proven-pid-image-starttime-and-health')}
                if error is None: error=RuntimeError('Synthetic backend was not stopped because identity was not completely verified')
        except Exception as cleanup_error:
            record['backend_cleanup_error'] = traceback.format_exc()
            if error is None: error = cleanup_error
        record['ok'] = error is None and bool(record['screenshots']) and not record.get('backend_cleanup_error')
        record['completed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        write_json(output / 'result.json',record)
        print(json.dumps({'ok':record['ok'],'version':record['version'],'build_revision':record['build_revision'],
                          'source_commit':record['source_commit'],'checks':record['checks'],
                          'screenshots':list(record['screenshots']),'report':str(output)},ensure_ascii=False),flush=True)
    return 1 if error is not None or not record['ok'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
