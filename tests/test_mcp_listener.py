"""Listener lifecycle tests; only synthetic folders and loopback sockets."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from yingxu.mcp import ProjectMCP
from yingxu.mcp_listener import MCPListener, normalize_endpoint
from yingxu.store import Store, UserError


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class BoundServer:
    def __init__(self, port):
        self.socket = socket.socket()
        try:
            self.socket.bind(('127.0.0.1', port))
            self.socket.listen(4)
        except BaseException:
            self.socket.close()
            raise
        self.server_port = self.socket.getsockname()[1]
        self.stop = threading.Event()
        self.started = threading.Event()
        self.closed = False
        self.shutdown_hook = None

    def serve_forever(self, poll_interval=.1):
        self.started.set()
        self.stop.wait()

    def shutdown(self):
        if self.shutdown_hook: self.shutdown_hook()
        self.stop.set()

    def server_close(self):
        self.closed = True
        self.socket.close()


class MCPListenerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yingxu-listener-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / 'data', self.root / 'projects')
        self.project = self.store.create_project('合成本机监听')
        self.other = self.store.create_project('另一合成项目')
        self.mcp = ProjectMCP(self.store, None)
        self.listener = MCPListener(self.mcp, self.store.data_root)
        self.addCleanup(self.listener.close)
        self.main_port = free_port()
        self.servers = []
        def factory(port):
            server = BoundServer(port)
            self.servers.append(server)
            return server
        self.factory = factory
        self.listener.attach(self.main_port, factory)

    def enable(self, endpoint=None, project=None):
        data = {'enabled': True, 'project_id': (project or self.project)['id']}
        if endpoint is not None: data['endpoint'] = endpoint
        return self.listener.configure(data)

    def url(self, port, path='/custom/mcp'):
        return f'http://127.0.0.1:{port}{path}'

    def token(self):
        return self.listener.connection()['config']['mcpServers']['yingxu']['headers']['Authorization']

    def test_default_is_off_same_port_enable_starts_no_extra_server(self):
        status = self.listener.status()
        self.assertFalse(status['enabled'])
        self.assertFalse(status['custom_endpoint'])
        self.assertEqual(status['endpoint'], self.url(self.main_port, '/mcp'))
        with self.assertRaises(UserError): self.listener.connection()
        self.assertTrue(self.enable()['enabled'])
        self.assertEqual(self.servers, [])
        self.assertTrue(self.listener.matches(self.main_port, '/mcp'))
        self.assertFalse(self.listener.matches(self.main_port, '/mcp/'))

    def test_strict_endpoint_validation_and_localhost_normalization(self):
        self.assertEqual(normalize_endpoint('http://localhost:18791/tool/mcp'), 'http://127.0.0.1:18791/tool/mcp')
        invalid = [None, 123, 'https://127.0.0.1:18791/mcp', 'http://0.0.0.0:18791/mcp',
                   'http://example.org:18791/mcp', 'http://127.0.0.2:18791/mcp', 'http://[::1]:18791/mcp',
                   'http://user@127.0.0.1:18791/mcp', 'http://127.0.0.1:80/mcp',
                   'http://127.0.0.1:65536/mcp', 'http://127.0.0.1:18791/',
                   'http://127.0.0.1:18791/../mcp', 'http://127.0.0.1:18791/mcp/',
                   'http://127.0.0.1:18791/a//mcp', 'http://127.0.0.1:18791/%6dcp',
                   'http://127.0.0.1:18791/mcp?', 'http://127.0.0.1:18791/mcp#',
                   'http://127.0.0.1:18791/api/mcp', 'http://127.0.0.1:18791/API',
                   'http://127.0.0.1:18791/static/custom', 'http://127.0.0.1:18791/中文',
                   'http://127.0.0.1:18791/' + 'a' * 128]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(UserError): normalize_endpoint(value)

    def test_custom_path_same_main_port_and_reset_default(self):
        self.enable(self.url(self.main_port, '/custom/read'))
        self.assertEqual(self.servers, [])
        self.assertTrue(self.listener.matches(self.main_port, '/custom/read'))
        self.assertFalse(self.listener.matches(self.main_port, '/mcp'))
        self.enable('')
        self.assertFalse(self.listener.status()['custom_endpoint'])
        self.assertTrue(self.listener.matches(self.main_port, '/mcp'))

    def test_independent_port_enable_reuse_path_change_disable_release_and_reenable(self):
        port = free_port()
        endpoint = self.url(port)
        self.enable(endpoint)
        server = self.servers[0]
        self.assertTrue(server.started.wait(2))
        bearer = self.token()
        self.enable(self.url(port, '/other/route'))
        self.assertEqual(len(self.servers), 1)
        self.assertTrue(self.listener.matches(port, '/other/route'))
        self.assertFalse(self.listener.matches(port, '/custom/mcp'))
        self.assertTrue(self.token() == bearer)
        self.listener.configure({'enabled': False})
        self.assertTrue(server.closed)
        self.assertFalse(self.listener.status()['enabled'])
        self.assertTrue(self.listener.matches(port, '/other/route'))
        with self.assertRaises(UserError): self.listener.dispatch(port, '/other/route', lambda: None)
        with socket.socket() as check: check.bind(('127.0.0.1', port))
        self.enable()
        self.assertEqual(len(self.servers), 2)
        self.assertTrue(self.token() == bearer)

    def test_binding_conflict_keeps_old_scope_credential_listener_and_public_preference(self):
        old_port = free_port()
        self.enable(self.url(old_port))
        old_server = self.servers[0]
        old_token = self.token()
        before = self.listener.preferences_path.read_bytes()
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0)); occupied.listen(1)
            port = occupied.getsockname()[1]
            with self.assertRaises(UserError): self.enable(self.url(port), self.other)
        self.assertFalse(old_server.closed)
        self.assertTrue(self.token() == old_token)
        self.assertEqual(self.listener.status()['project_id'], self.project['id'])
        self.assertEqual(self.listener.preferences_path.read_bytes(), before)
        self.assertTrue(self.listener.matches(old_port, '/custom/mcp'))

    def test_preference_save_failure_closes_candidate_without_rotating_project_or_credentials(self):
        old_port = free_port()
        self.enable(self.url(old_port))
        old_token = self.token()
        before = self.listener.preferences_path.read_bytes()
        with patch.object(self.listener, '_save_preference', side_effect=OSError('synthetic save failure')):
            with self.assertRaises(UserError): self.enable(self.url(free_port()), self.other)
        self.assertTrue(self.servers[-1].closed)
        self.assertFalse(self.servers[0].closed)
        self.assertTrue(self.token() == old_token)
        self.assertEqual(self.listener.status()['project_id'], self.project['id'])
        self.assertEqual(self.listener.preferences_path.read_bytes(), before)

    def test_mcp_enable_failure_rolls_back_preference_and_candidate(self):
        self.enable(self.url(self.main_port, '/old'))
        before = self.listener.preferences_path.read_bytes()
        old_token = self.token()
        with patch.object(self.mcp, 'configure', side_effect=UserError('synthetic scope failure', 409)):
            with self.assertRaises(UserError): self.enable(self.url(free_port()), self.other)
        self.assertEqual(self.listener.preferences_path.read_bytes(), before)
        self.assertTrue(self.servers[-1].closed)
        self.assertTrue(self.token() == old_token)
        self.assertEqual(self.listener.status()['endpoint'], self.url(self.main_port, '/old'))

    def test_rollback_failure_is_explicit_and_old_active_address_survives(self):
        self.enable(self.url(self.main_port, '/old'))
        with patch.object(self.mcp, 'configure', side_effect=UserError('synthetic scope failure', 409)), patch.object(self.listener, '_restore_preference', side_effect=OSError('synthetic rollback failure')):
            with self.assertRaises(UserError) as error: self.enable(self.url(free_port()), self.other)
        self.assertIn('回退失败', str(error.exception))
        self.assertIn('回退失败', self.listener.status()['address_error'])
        self.assertTrue(self.listener.matches(self.main_port, '/old'))
        self.assertTrue(self.servers[-1].closed)

    def test_restart_loads_address_only_and_remains_disabled(self):
        endpoint = self.url(free_port())
        self.enable(endpoint)
        restarted_mcp = ProjectMCP(self.store, None)
        with patch.object(restarted_mcp, '_credential', side_effect=AssertionError('startup must not read credentials')):
            restarted = MCPListener(restarted_mcp, self.store.data_root)
            self.addCleanup(restarted.close)
            restarted.attach(self.main_port, lambda port: self.fail('startup must not bind'))
        self.assertEqual(restarted.status()['endpoint'], endpoint)
        self.assertFalse(restarted.status()['enabled'])
        self.assertTrue(restarted.matches(int(endpoint.split(':')[2].split('/')[0]), '/custom/mcp'))
        with self.assertRaises(UserError): restarted.request_generation(int(endpoint.split(':')[2].split('/')[0]), '/custom/mcp', 'invalid')
        saved = json.loads(self.listener.preferences_path.read_text('utf-8'))
        self.assertEqual(set(saved), {'schema', 'endpoint'})

    def test_invalid_public_preferences_fail_closed_and_can_be_replaced_explicitly(self):
        self.listener.preferences_path.write_text('{bad', encoding='utf-8')
        restarted = MCPListener(ProjectMCP(self.store, None), self.store.data_root)
        self.addCleanup(restarted.close)
        restarted.attach(self.main_port, self.factory)
        self.assertFalse(restarted.status()['enabled'])
        self.assertTrue(restarted.status()['address_error'])
        restarted.configure({'enabled': False, 'endpoint': ''})
        self.assertEqual(restarted.status()['address_error'], '')

    def test_invalid_configuration_preserves_previous_state(self):
        self.enable()
        for data in ({'enabled': True, 'project_id': self.project['id'], 'host': '0.0.0.0'},
                     {'enabled': False, 'project_id': self.project['id']}, {'enabled': 1},
                     {'enabled': True}, {'enabled': True, 'project_id': 'missing'},
                     {'enabled': False, 'endpoint': 'http://outside:18791/mcp'}):
            with self.subTest(data=data):
                with self.assertRaises(UserError): self.listener.configure(data)
        self.assertTrue(self.listener.status()['enabled'])
        self.assertEqual(self.servers, [])

    def test_dispatch_rechecks_route_and_serializes_scope_switch(self):
        self.enable()
        entered, release, switched = threading.Event(), threading.Event(), threading.Event()
        def callback():
            entered.set()
            release.wait(2)
            return self.mcp.status(self.url(self.main_port, '/mcp'))['project_id']
        output = []
        thread = threading.Thread(target=lambda: output.append(self.listener.dispatch(self.main_port, '/mcp', callback)))
        thread.start(); self.assertTrue(entered.wait(2))
        switch = threading.Thread(target=lambda: (self.enable(self.url(self.main_port, '/new'), self.other), switched.set()))
        switch.start()
        self.assertFalse(switched.wait(.05))
        release.set(); thread.join(2); switch.join(2)
        self.assertEqual(output, [self.project['id']])
        self.assertTrue(switched.is_set())
        with self.assertRaises(UserError): self.listener.dispatch(self.main_port, '/mcp', lambda: self.fail('stale route must not dispatch'))

    def test_origin_host_cross_site_and_session_token_are_separate(self):
        self.enable()
        expected = f'127.0.0.1:{self.main_port}'
        self.listener.check_origin(self.main_port, {'Host': expected})
        self.listener.check_origin(self.main_port, {'Host': expected, 'Origin': 'http://' + expected})
        for headers in ({'Host': f'localhost:{self.main_port}'}, {'Host': expected, 'Origin': 'null'},
                        {'Host': expected, 'Origin': 'http://outside'}, {'Host': expected, 'Sec-Fetch-Site': 'cross-site'}):
            with self.assertRaises(UserError): self.listener.check_origin(self.main_port, headers)
        with self.assertRaises(UserError): self.mcp.authorize('Bearer synthetic-workbench-session-token')

    def test_shutdown_runs_outside_configuration_lock_and_close_is_final(self):
        self.enable(self.url(free_port()))
        checks = []
        def hook():
            worker = threading.Thread(target=lambda: checks.append(self.listener.matches(self.main_port, '/mcp')))
            worker.start(); worker.join(1)
            self.assertFalse(worker.is_alive(), 'shutdown must not hold the listener lock')
        self.servers[0].shutdown_hook = hook
        self.listener.close()
        self.assertEqual(checks, [False])
        self.listener.close()
        self.assertTrue(self.servers[0].closed)
        with self.assertRaises(UserError): self.enable()
        with self.assertRaises(UserError): self.listener.connection()

    def test_request_generation_rejects_same_address_disable_reenable_and_path_aba(self):
        self.enable()
        generation = self.listener.request_generation(self.main_port, '/mcp', self.token())
        self.listener.configure({'enabled': False})
        self.enable()
        with self.assertRaises(UserError): self.listener.dispatch(self.main_port, '/mcp', lambda: self.fail('old body must not dispatch'), generation=generation)
        current = self.listener.request_generation(self.main_port, '/mcp', self.token())
        self.enable(self.url(self.main_port, '/changed'))
        self.enable('')
        with self.assertRaises(UserError): self.listener.dispatch(self.main_port, '/mcp', lambda: self.fail('ABA body must not dispatch'), generation=current)
        latest = self.listener.request_generation(self.main_port, '/mcp', self.token())
        self.assertEqual(self.listener.dispatch(self.main_port, '/mcp', lambda: 'ok', generation=latest), 'ok')

    def test_removed_project_rejects_generation_and_dispatch_and_status_releases_socket(self):
        port = free_port()
        self.enable(self.url(port))
        bearer = self.token()
        generation = self.listener.request_generation(port, '/custom/mcp', bearer)
        with self.store.connection() as db: db.execute('UPDATE projects SET removed=1 WHERE id=?', (self.project['id'],))
        with self.assertRaises(UserError): self.listener.request_generation(port, '/custom/mcp', bearer)
        with self.assertRaises(UserError): self.listener.dispatch(port, '/custom/mcp', lambda: self.fail('removed scope must not dispatch'), generation=generation)
        self.assertFalse(self.listener.status()['enabled'])
        self.assertTrue(self.servers[0].closed)
        with socket.socket() as check: check.bind(('127.0.0.1', port))

    def test_configure_does_not_read_project_after_publishing_scope(self):
        original = self.store.get_project
        calls = []
        def only_preflight(pid):
            calls.append(pid)
            if len(calls) > 2: raise OSError('synthetic post-commit database failure')
            return original(pid)
        with patch.object(self.store, 'get_project', side_effect=only_preflight):
            result = self.enable()
        self.assertTrue(result['enabled'])
        self.assertEqual(len(calls), 2)
        self.assertTrue(self.listener.status()['enabled'])
