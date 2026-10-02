"""Synthetic metadata only. Never model execution or real workspace access."""
import copy
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class HubFixture:
    capability_id = 'c' * 32

    def __init__(self):
        self.requests = []
        self.bad_snapshot = None
        self.pad = 0
        self.snapshot_status = 200
        self.describe_status = 200
        self.before_snapshot = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                owner.requests.append(('POST', self.path))
                self.send_response(405)
                self.end_headers()

            def do_GET(self):
                owner.requests.append(('GET', self.path))
                path = urlsplit(self.path)
                status = 404
                value = {'error': 'synthetic'}
                if path.path == '/api/interop/describe':
                    value, status = owner.describe, owner.describe_status
                elif path.path == '/api/interop/capability-snapshot':
                    if owner.before_snapshot:
                        owner.before_snapshot()
                    query = {k: v[-1] for k, v in parse_qs(path.query).items()}
                    value, status = owner.snapshot(), owner.snapshot_status
                    if (query.get('capability_id') != owner.capability_id
                            or query.get('connection_revision') != owner.describe['connection_revision']
                            or query.get('_workspace_root') != owner.describe['workspace_root']
                            or (query.get('expected_declaration_sha256')
                                and query['expected_declaration_sha256'] != value['declaration']['sha256'])):
                        status = 409
                    if owner.bad_snapshot is not None:
                        value = owner.bad_snapshot(value)
                raw = value if isinstance(value, bytes) else encode(value)
                if path.path.endswith('capability-snapshot'):
                    raw += b' ' * owner.pad
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.describe = {
            'protocol': 'aihub-interop/1', 'schema_version': 1,
            'identity': {'app': 'ai-hub', 'status': 'available', 'port': self.server.server_port,
                         'service_instance_id': 'a' * 32, 'control_protocol': 'ai-hub-local-control-v1',
                         'server_version': '2.13.9', 'install_root': 'C:\\fixtures\\app'},
            'workspace': {'status': 'available', 'root': 'C:\\fixtures\\workspace', 'binding_revision': 'b' * 64,
                          'revision_scope': 'local_path_and_directory_identity',
                          'full_configuration_revision': False, 'cross_machine_uuid': False},
            'workspace_root': 'C:\\fixtures\\workspace', 'connection_revision': 'd' * 64,
            'operations': {'read_only': ['interop_describe', 'interop_capability_snapshot'],
                           'queue_and_claim': ['capability_dispatch'], 'execution_mode': 'harness_queue',
                           'worker_required': True, 'direct_execution': False, 'native_cancel': False,
                           'cloud_upload': False, 'dispatch_supports_declaration_cas': False,
                           'routing': 'target_tool_not_declared_client'},
            'limits': {'declaration_bytes': 32768, 'response_bytes': 131072,
                       'report_bytes': 1048576, 'output_bytes': 67108864}, 'limitations': [],
        }
        self.declaration = {'key': 'fixture.video', 'name': '合成视频选型', 'kind': 'mcp_tool',
                            'inputs': {'type': 'object', 'properties': {'prompt': {'type': 'string'}}}}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def snapshot(self):
        text = encode(self.declaration)
        return {**{k: copy.deepcopy(self.describe[k]) for k in
                   ('protocol', 'schema_version', 'identity', 'workspace', 'workspace_root', 'connection_revision')},
                'snapshot_id': 'e' * 64,
                'selected': {'id': self.capability_id, 'key': self.declaration['key'],
                             'client_id': 'synthetic-worker', 'target_tool': 'codex', 'updated_at': '2026-10-02'},
                'declaration': {'origin': 'stored_normalized_declaration', 'encoding': 'utf-8',
                                'text': text.decode('utf-8'), 'bytes': len(text),
                                'sha256': hashlib.sha256(text).hexdigest(), 'parsed': copy.deepcopy(self.declaration)},
                'harness': {'status': 'registered', 'id': 'codex', 'enabled': True, 'revision': 1,
                            'connection_mode': 'mcp_stdio', 'origin': 'explicit'},
                'evidence': {'declaration': {'original_publisher_bytes_preserved': False},
                             'heartbeat': {'recent': True},
                             'actual_invocation': {'status': 'unverified', 'observed': False}},
                'execution': {'mode': 'harness_queue', 'worker_required': True, 'direct_execution': False,
                              'routing': 'target_tool_not_declared_client', 'dispatch_supports_declaration_cas': False},
                'limitations': []}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
