"""Real loopback HTTP Range fixtures, with no external requests or user data."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import tempfile
import threading
import urllib.request
from unittest.mock import patch
import zipfile

from yingxu.incremental_update import UpdateManager
from yingxu.range_zip import Network, REPOSITORY


def sha(value):
    return hashlib.sha256(value).hexdigest()


def manifest(files, version):
    return {'application': 'YingXu', 'version': version, 'root': 'YingXu/', 'architecture': 'Windows x64',
            'source_commit': 'a' * 40,
            'files': [dict(path=name, bytes=len(content), sha256=sha(content)) for name, content in files.items()]}


class Fixture:
    def __init__(self, new_version='0.4.19', extra_members=None, modify_info=None):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.install = self.root / 'install'
        self.data = self.root / 'data'
        self.old = {'YingXu.exe': b'exe fixture', 'server.py': b'old server', 'launcher.pyw': b'launcher',
                    'frontend/index.html': b'<p>old</p>', 'runtime/python313.zip': b'RUNTIME' * 400000,
                    'yingxu/obsolete.py': b'obsolete'}
        self.new = dict(self.old)
        self.new.pop('yingxu/obsolete.py')
        self.new.update({'server.py': b'new server', 'frontend/index.html': b'<p>new</p>', 'yingxu/new.py': b'new module'})
        for name, content in self.old.items():
            file = self.install / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(content)
        (self.install / 'RELEASE_MANIFEST.json').write_text(json.dumps(manifest(self.old, '0.4.18')), encoding='utf-8')
        self.version = new_version
        self.manifest_raw = json.dumps(manifest(self.new, new_version)).encode()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for name, content in [*self.new.items(), ('RELEASE_MANIFEST.json', self.manifest_raw), *(extra_members or [])]:
                info = zipfile.ZipInfo('YingXu/' + name)
                info.compress_type = zipfile.ZIP_STORED if name.startswith('runtime/') else zipfile.ZIP_DEFLATED
                if modify_info:
                    modify_info(info)
                archive.writestr(info, content)
        self.zip = buffer.getvalue()
        with zipfile.ZipFile(io.BytesIO(self.zip)) as archive:
            self.info = {item.filename[7:]: item for item in archive.infolist()}
        self.zip_name = f'YingXu-v{new_version}-Windows-x64.zip'
        self.manifest_name = f'YingXu-v{new_version}-manifest.json'
        self.tag = 'yingxu-v' + new_version
        self.base_url = f'https://github.com/{REPOSITORY}/releases/download/{self.tag}/'
        self.metadata = dict(file=self.zip_name, version=new_version, bytes=len(self.zip), sha256=sha(self.zip),
                             root='YingXu/', source_commit='a' * 40, release_manifest_sha256=sha(self.manifest_raw))
        self.refresh_metadata()
        self.requests = []
        self.range_mode = 'normal'
        self.failure_range = None
        self.etag = '"same-asset"'
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == '/releases':
                    body = json.dumps(outer.releases).encode()
                    outer.requests.append(('api', None, None))
                    self.send_response(200)
                elif self.path == '/manifest':
                    body = outer.external
                    outer.requests.append(('manifest', None, None))
                    self.send_response(200)
                else:
                    match = re.fullmatch(r'bytes=(\d+)-(\d+)', self.headers.get('Range', ''))
                    if match is None:
                        self.send_error(400)
                        return
                    start, end = map(int, match.groups())
                    outer.requests.append(('zip', start, end))
                    if outer.failure_range is not None and start == outer.failure_range:
                        self.send_error(503)
                        return
                    body = outer.zip[start:end + 1]
                    self.send_response(200 if outer.range_mode == 'unsupported' else 206)
                    self.send_header('Content-Range', f'bytes {start + (outer.range_mode == "wrong")}-{end}/{len(outer.zip)}')
                    self.send_header('ETag', outer.etag)
                    if outer.range_mode == 'truncated':
                        body = body[:-1]
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (ConnectionError, OSError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.managers = []

    def refresh_metadata(self):
        self.external = json.dumps(self.metadata).encode()
        assets = [dict(id=index + 1, name=name, state='uploaded', size=len(raw), digest='sha256:' + sha(raw),
                       browser_download_url=self.base_url + name)
                  for index, (name, raw) in enumerate(((self.zip_name, self.zip), (self.manifest_name, self.external)))]
        self.releases = [dict(tag_name=self.tag, draft=False, prerelease=False,
                              html_url=f'https://github.com/{REPOSITORY}/releases/tag/{self.tag}', assets=assets)]

    def __enter__(self):
        outer = self
        def local_open(network, request):
            url = request.full_url
            path = '/releases' if 'api.github.com' in url else '/manifest' if url.endswith('.json') else '/zip'
            local_request = urllib.request.Request(f'http://127.0.0.1:{outer.server.server_port}' + path,
                                                   headers=dict(request.header_items()))
            response = urllib.request.urlopen(local_request, timeout=3)
            # Production URL validation still sees the fixed trusted origin;
            # only the test transport maps its bytes to our loopback server.
            response.geturl = lambda: url
            return response
        self.patch = patch.object(Network, '_open', local_open)
        self.patch.start()
        return self

    def manager(self):
        manager = UpdateManager(self.data, self.install, '0.4.18')
        self.managers.append(manager)
        return manager

    def wait(self, manager):
        manager._worker.join(5)
        if manager._worker.is_alive():
            raise AssertionError('worker did not finish')
        return manager.status()

    def plan(self, manager):
        manager.plan()
        return self.wait(manager)

    def download(self, manager):
        manager.download(manager.status()['plan_id'])
        return self.wait(manager)

    def data_range(self, name):
        info = self.info[name]
        start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        return start, start + info.compress_size - 1

    def __exit__(self, *args):
        for manager in self.managers:
            manager.close()
            if manager._worker:
                manager._worker.join(5)
        self.patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temp.cleanup()
