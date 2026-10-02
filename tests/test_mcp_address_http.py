"""Actual loopback HTTP for custom MCP addresses; synthetic projects only."""
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from server import Application, Server


class MCPAddressHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='yingxu-mcp-address-')
        self.root = Path(self.temporary.name).resolve()
        (self.root / 'home').mkdir()
        with patch('yingxu.skills.Path.home', return_value=self.root / 'home'):
            self.app = Application(self.root / 'data', self.root / 'projects')
        self.app._skills_startup.result(timeout=10)
        self.project = self.app.store.create_project('合成 MCP 项目')
        self.server = Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.app.close()
        self.temporary.cleanup()

    @staticmethod
    def unused_port():
        with socket.socket() as handle:
            handle.bind(('127.0.0.1', 0))
            return handle.getsockname()[1]

    def request(self, path, body=None, *, port=None, headers=None, method=None, ui=False):
        connection = http.client.HTTPConnection('127.0.0.1', port or self.port, timeout=10)
        fields = {'Content-Type': 'application/json'}
        if ui:
            fields.update({'X-YingXu-Token': self.app.token,
                           'Origin': f'http://127.0.0.1:{self.port}'})
        fields.update(headers or {})
        try:
            connection.request(method or ('GET' if body is None else 'POST'), path,
                               json.dumps(body).encode() if body is not None else None, fields)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
        finally:
            connection.close()

    def configure(self, **fields):
        return self.request('/api/mcp/configure', fields, ui=True)

    def credentials(self):
        code, data = self.request('/api/mcp/connection', {}, ui=True)
        self.assertEqual(code, 200)
        return data['config']['mcpServers']['yingxu']

    def rpc(self, path, *, port=None, token=None, headers=None):
        fields = {'Authorization': token or self.credentials()['headers']['Authorization'],
                  'MCP-Protocol-Version': '2025-11-25'}
        fields.update(headers or {})
        return self.request(path, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                            port=port, headers=fields)

    def test_save_while_off_then_enable_actual_port_and_export(self):
        port = self.unused_port()
        code, status = self.configure(enabled=False, endpoint=f'http://localhost:{port}/ai/project')
        self.assertEqual(code, 200)
        self.assertFalse(status['enabled'])
        self.assertEqual(status['endpoint'], f'http://127.0.0.1:{port}/ai/project')
        with self.assertRaises(OSError):
            self.request('/ai/project', port=port)
        code, status = self.configure(enabled=True, project_id=self.project['id'])
        self.assertEqual(code, 200)
        self.assertTrue(status['enabled'])
        self.assertTrue(status['custom_endpoint'])
        self.assertEqual(self.credentials()['url'], status['endpoint'])
        code, result = self.rpc('/ai/project', port=port)
        self.assertEqual(code, 200)
        self.assertEqual(len(result['result']['tools']), 7)
        self.assertIn(self.rpc('/mcp')[0], (403, 404))
        self.assertEqual(self.request('/api/health')[0], 200)

    def test_custom_listener_never_exposes_workbench_or_control_routes(self):
        port = self.unused_port()
        self.assertEqual(self.configure(enabled=True, project_id=self.project['id'],
                                       endpoint=f'http://127.0.0.1:{port}/link/mcp')[0], 200)
        for route in ('/', '/index.html', '/app.js', '/api/bootstrap', '/api/mcp/status', '/api/ai-tasks'):
            self.assertEqual(self.request(route, port=port, ui=True)[0], 404)
        self.assertEqual(self.request('/api/mcp/configure', {'enabled': False}, port=port, ui=True)[0], 404)
        self.assertEqual(self.rpc('/link/mcp', port=port, token='Bearer ' + self.app.token)[0], 401)
        for fields in ({'Host': 'evil.example'}, {'Origin': 'http://evil.example'},
                       {'Origin': f'http://127.0.0.1:{self.port}'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.rpc('/link/mcp', port=port, headers=fields)[0], 403)
        self.assertEqual(self.rpc('/link/mcp?extra=1', port=port)[0], 400)

    def test_same_port_custom_path_then_restore_default(self):
        custom = f'http://127.0.0.1:{self.port}/agent/yingxu'
        self.assertEqual(self.configure(enabled=True, project_id=self.project['id'], endpoint=custom)[0], 200)
        first = self.credentials()
        self.assertEqual(first['url'], custom)
        self.assertEqual(self.rpc('/agent/yingxu')[0], 200)
        self.assertIn(self.rpc('/mcp')[0], (403, 404))
        code, status = self.configure(enabled=True, project_id=self.project['id'], endpoint='')
        self.assertEqual(code, 200)
        self.assertFalse(status['custom_endpoint'])
        self.assertEqual(status['endpoint'], status['default_endpoint'])
        self.assertTrue(first['headers'] == self.credentials()['headers'])
        self.assertEqual(self.rpc('/mcp')[0], 200)
        self.assertIn(self.rpc('/agent/yingxu')[0], (403, 404))

    def test_conflicting_port_preserves_working_scope_credentials_and_address(self):
        self.assertEqual(self.configure(enabled=True, project_id=self.project['id'])[0], 200)
        original = self.credentials()
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            other = self.app.store.create_project('另一个合成授权项目')
            code, _ = self.configure(enabled=True, project_id=other['id'],
                                     endpoint=f'http://127.0.0.1:{occupied.getsockname()[1]}/project')
        self.assertEqual(code, 409)
        code, status = self.request('/api/mcp/status', ui=True)
        self.assertEqual(code, 200)
        self.assertTrue(status['enabled'])
        self.assertEqual(status['project_id'], self.project['id'])
        self.assertTrue(original == self.credentials())
        self.assertEqual(self.rpc('/mcp')[0], 200)

    def test_invalid_endpoints_rejected_without_changing_active_server(self):
        self.assertEqual(self.configure(enabled=True, project_id=self.project['id'])[0], 200)
        original = self.credentials()
        for endpoint in ('http://0.0.0.0:9001/mcp', 'http://192.168.1.1:9001/mcp',
                         'https://127.0.0.1:9001/mcp', 'http://user@127.0.0.1:9001/mcp',
                         'http://127.0.0.1:9001/mcp?x=1', 'http://127.0.0.1:9001/mcp#x',
                         'http://127.0.0.1:9001/api/mcp', 'http://127.0.0.1:9001/app.js',
                         'http://127.0.0.1:9001/a/../mcp', 'http://127.0.0.1:9001/%6dcp',
                         'http://127.0.0.1:1/mcp'):
            self.assertEqual(self.configure(enabled=True, project_id=self.project['id'], endpoint=endpoint)[0], 400)
        self.assertTrue(original == self.credentials())
        self.assertEqual(self.rpc('/mcp')[0], 200)

    def test_disable_releases_custom_socket_and_reenable_reuses_project_config(self):
        port = self.unused_port()
        self.assertEqual(self.configure(enabled=True, project_id=self.project['id'],
                                       endpoint=f'http://127.0.0.1:{port}/custom')[0], 200)
        original = self.credentials()
        self.assertEqual(self.configure(enabled=False)[0], 200)
        with self.assertRaises(OSError):
            self.request('/custom', port=port)
        self.assertEqual(self.configure(enabled=True, project_id=self.project['id'])[0], 200)
        self.assertTrue(original == self.credentials())
        self.assertEqual(self.rpc('/custom', port=port)[0], 200)

    def test_authorization_switch_has_no_post_commit_database_read(self):
        self.assertEqual(self.configure(enabled=True, project_id=self.project['id'])[0], 200)
        other = self.app.store.create_project('新的合成授权项目')
        port = self.unused_port()
        original = self.app.store.get_project
        reads = []
        def preflight_only(pid):
            reads.append(pid)
            if len(reads) > 2:
                raise OSError('Synthetic post-commit database failure')
            return original(pid)
        with patch.object(self.app.store, 'get_project', side_effect=preflight_only):
            code, status = self.configure(enabled=True, project_id=other['id'],
                                          endpoint=f'http://127.0.0.1:{port}/new/project')
        self.assertEqual(code, 200)
        self.assertEqual(len(reads), 2)
        self.assertEqual(status['project_id'], other['id'])
        self.assertEqual(self.credentials()['url'], status['endpoint'])
        self.assertEqual(self.rpc('/new/project', port=port)[0], 200)

    def test_removing_authorized_project_releases_listener_on_status_read(self):
        port = self.unused_port()
        self.assertEqual(self.configure(enabled=True, project_id=self.project['id'],
                                       endpoint=f'http://127.0.0.1:{port}/project')[0], 200)
        with self.app.store.connection() as db:
            db.execute('UPDATE projects SET removed=1 WHERE id=?', (self.project['id'],))
        code, status = self.request('/api/mcp/status', ui=True)
        self.assertEqual(code, 200)
        self.assertFalse(status['enabled'])
        with self.assertRaises(OSError):
            self.request('/project', port=port)

    def test_old_body_cannot_dispatch_after_path_or_authorization_generation_switch(self):
        for change in ('path', 'close-reopen', 'path-return'):
            with self.subTest(change=change):
                self.slow_body_switch(change)

    def slow_body_switch(self, change):
        self.assertEqual(self.configure(enabled=True, project_id=self.project['id'], endpoint='')[0], 200)
        original = self.credentials()
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).encode()
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        accepted = threading.Event()
        request_generation = self.app.mcp_listener.request_generation
        def mark_accepted(*args):
            result = request_generation(*args)
            accepted.set()
            return result
        ticket_patch = patch.object(self.app.mcp_listener, 'request_generation', side_effect=mark_accepted)
        ticket_patch.start()
        try:
            connection.putrequest('POST', '/mcp')
            connection.putheader('Authorization', original['headers']['Authorization'])
            connection.putheader('Content-Type', 'application/json')
            connection.putheader('Content-Length', str(len(body)))
            connection.putheader('MCP-Protocol-Version', '2025-11-25')
            connection.endheaders()
            connection.send(body[:1])
            self.assertTrue(accepted.wait(5), 'Request must enter authorization before the switch')
            if change == 'close-reopen':
                self.assertEqual(self.configure(enabled=False)[0], 200)
                self.assertEqual(self.configure(enabled=True, project_id=self.project['id'])[0], 200)
            else:
                self.assertEqual(self.configure(enabled=True, project_id=self.project['id'],
                                               endpoint=f'http://127.0.0.1:{self.port}/new/mcp')[0], 200)
                if change == 'path-return':
                    self.assertEqual(self.configure(enabled=True, project_id=self.project['id'], endpoint='')[0], 200)
            connection.send(body[1:])
            response = connection.getresponse()
            self.assertIn(response.status, (403, 409))
            response.read()
        finally:
            ticket_patch.stop()
            connection.close()


if __name__ == '__main__':
    unittest.main()
