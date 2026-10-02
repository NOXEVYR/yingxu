"""Auth rejection survives a late body without unbounded draining or writes.

All files, project names, credentials and loopback listeners are synthetic.
The socket harness sends only headers until a rejection status arrives.
"""
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import server as server_module
from server import Application, Handler, Server


class FakeClock:
    def __init__(self): self.current = 0.0
    def monotonic(self): return self.current


class RejectSocket:
    def __init__(self, events, clock, *, tick=0, chunk=16384, timeout=False, eof=False):
        self.events, self.clock = events, clock
        self.tick, self.chunk, self.timeout, self.eof = tick, chunk, timeout, eof
        self.original_timeout = 30
        self.timeouts = []
        self.read_sizes = []
        self.received = 0

    def gettimeout(self): return self.original_timeout
    def settimeout(self, value): self.timeouts.append(value)
    def shutdown(self, direction): self.events.append(('shutdown', direction))
    def recv(self, size):
        self.events.append(('recv', size))
        self.read_sizes.append(size)
        self.clock.current += self.tick
        if self.timeout: raise socket.timeout('synthetic drain timeout')
        if self.eof: return b''
        result = b'x' * min(self.chunk, size)
        self.received += len(result)
        return result


class HTTPRejectionUnitTests(unittest.TestCase):
    def invoke(self, headers=None, **socket_options):
        events, clock = [], FakeClock()
        connection = RejectSocket(events, clock, **socket_options)
        handler = SimpleNamespace(connection=connection, headers=headers or {}, close_connection=False)
        handler.json = Mock(side_effect=lambda data, status=200, extra=None: events.append(('json', status, data, extra)))
        handler.wfile = SimpleNamespace(flush=lambda: events.append(('flush',)))
        handler.rfile = SimpleNamespace(read=Mock(side_effect=AssertionError('must not parse or read request body')))
        with patch.object(server_module.time, 'monotonic', side_effect=clock.monotonic):
            Handler.reject_json(handler, {'error': 'synthetic denial'}, 403, {'Allow': 'POST'})
        return handler, events, connection, clock

    def test_flush_and_half_close_precede_drain_and_never_parse_body(self):
        handler, events, connection, _clock = self.invoke({'Content-Length': '2'}, eof=True)
        self.assertTrue(handler.close_connection)
        self.assertEqual([event[0] for event in events[:4]], ['json', 'flush', 'shutdown', 'recv'])
        self.assertEqual(events[2][1], socket.SHUT_WR)
        extra = events[0][3]
        self.assertEqual(extra['Connection'], 'close')
        self.assertEqual(extra['Allow'], 'POST')
        handler.rfile.read.assert_not_called()
        self.assertEqual(connection.timeouts[-1], connection.original_timeout)

    def test_byte_cap_handles_huge_length_and_chunked_transfer_without_decoding(self):
        for headers in ({'Content-Length': str(1024 ** 4)}, {'Transfer-Encoding': 'chunked'},
                        {'Content-Length': 'invalid'}, {'Content-Length': '-1'}):
            with self.subTest(headers=headers):
                handler, _events, connection, _clock = self.invoke(headers)
                self.assertLessEqual(connection.received, 128 * 1024)
                self.assertGreater(connection.received, 0)
                self.assertLessEqual(len(connection.read_sizes), 128)
                self.assertTrue(all(0 < size <= 128 * 1024 for size in connection.read_sizes))
                handler.rfile.read.assert_not_called()

    def test_continuous_trickle_is_limited_by_absolute_deadline(self):
        _handler, _events, connection, clock = self.invoke({'Content-Length': '999999999'}, tick=.02, chunk=1)
        self.assertLessEqual(len(connection.read_sizes), 3)
        self.assertLessEqual(clock.current, .061)
        self.assertLess(connection.received, 128 * 1024)
        self.assertTrue(all(0 < timeout <= .05 for timeout in connection.timeouts[:-1]))

    def test_receive_timeout_restores_timeout_and_returns(self):
        _handler, _events, connection, _clock = self.invoke({'Content-Length': '1'}, timeout=True)
        self.assertEqual(len(connection.read_sizes), 1)
        self.assertEqual(connection.timeouts[-1], connection.original_timeout)


class HTTPRejectionSocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix='yingxu-http-reject-')
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name).resolve()
        home = cls.root / 'home'
        home.mkdir()
        with patch('yingxu.skills.Path.home', return_value=home):
            cls.app = Application(cls.root / 'data', cls.root / 'projects')
        cls.addClassCleanup(cls.app.close)
        cls.app._skills_startup.result(timeout=15)
        cls.project = cls.app.store.create_project('合成鉴权拒绝测试')
        cls.server = Server(('127.0.0.1', 0), cls.app)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        cls.thread.start()
        def stop_server():
            cls.server.shutdown(); cls.server.server_close(); cls.thread.join(5)
        cls.addClassCleanup(stop_server)
        cls.port = cls.server.server_port
        cls.origin = f'http://127.0.0.1:{cls.port}'
        cls.app.mcp_listener.configure({'enabled': True, 'project_id': cls.project['id']})

    def rejection(self, path, status, *, headers=None, body=b'{}', send_body=True, verify_while_open=None):
        request_headers = {'Host': f'127.0.0.1:{self.port}', 'Content-Type': 'application/json',
                           'Content-Length': str(len(body)), 'Connection': 'close'}
        request_headers.update(headers or {})
        encoded = ('POST ' + path + ' HTTP/1.1\r\n' + ''.join(name + ': ' + value + '\r\n' for name, value in request_headers.items()) + '\r\n').encode('ascii')
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as connection:
            connection.settimeout(3)
            connection.sendall(encoded)  # Deliberately no body yet.
            received = b''
            while b'\r\n\r\n' not in received:
                part = connection.recv(4096)
                self.assertTrue(part, 'rejection headers must arrive before client body')
                received += part
            head, response = received.split(b'\r\n\r\n', 1)
            lines = head.decode('ascii').split('\r\n')
            self.assertEqual(int(lines[0].split()[1]), status)
            response_headers = dict(line.split(': ', 1) for line in lines[1:])
            length = int(response_headers['Content-Length'])
            self.assertEqual(response_headers['Connection'], 'close')
            if send_body:
                # The status is already visible. A late, short body must not
                # make Windows reset the unread error response.
                connection.sendall(body)
                connection.shutdown(socket.SHUT_WR)
            while len(response) < length:
                part = connection.recv(length - len(response))
                self.assertTrue(part, 'rejection JSON must not be truncated or reset')
                response += part
            result = json.loads(response[:length].decode('utf-8'))
            if verify_while_open is not None: verify_while_open(connection)
            return result

    def test_ui_missing_token_late_body_returns_complete_403_without_write(self):
        with patch.object(self.app.store, 'create_project', side_effect=AssertionError('unauthorized request must not write')) as create:
            result = self.rejection('/api/projects', 403, headers={'Origin': self.origin}, body=b'{"name":"must not create"}')
            create.assert_not_called()
        self.assertIn('error', result)

    def test_foreign_origin_late_body_returns_complete_403_without_write(self):
        with patch.object(self.app.store, 'create_project', side_effect=AssertionError('cross-origin request must not write')) as create:
            result = self.rejection('/api/projects', 403, headers={'Origin': 'https://outside.invalid', 'X-YingXu-Token': self.app.token}, body=b'{"name":"must not create"}')
            create.assert_not_called()
        self.assertIn('error', result)

    def test_wrong_mcp_bearer_late_body_returns_complete_401_without_dispatch(self):
        with patch.object(self.app.mcp, 'handle', side_effect=AssertionError('wrong Bearer must not dispatch')) as handle:
            result = self.rejection('/mcp', 401, headers={'Authorization': 'Bearer ' + 'x' * 43}, body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
            handle.assert_not_called()
        self.assertIn('error', result)
        self.assertEqual(result['jsonrpc'], '2.0')

    def test_slow_missing_body_returns_worker_within_bound(self):
        def verify_while_client_is_still_connected(_connection):
            deadline = time.monotonic() + 2
            ready = False
            # The main server's known 24-worker bound allows a direct proof
            # that the rejected worker returned; no polling of user resources.
            while time.monotonic() < deadline:
                acquired = 0
                try:
                    while acquired < 24 and self.server.slots.acquire(False): acquired += 1
                    if acquired == 24:
                        ready = True
                        break
                finally:
                    for _ in range(acquired): self.server.slots.release()
                threading.Event().wait(.005)
            self.assertTrue(ready, 'bodyless rejected worker must release its server slot')
        with patch.object(self.app.store, 'create_project', side_effect=AssertionError('slow unauthorized body must not write')) as create:
            self.rejection('/api/projects', 403, headers={'Origin': self.origin, 'Content-Length': str(1024 ** 4)}, send_body=False, verify_while_open=verify_while_client_is_still_connected)
            create.assert_not_called()

    def test_authorized_mcp_chunked_body_is_rejected_before_dispatch(self):
        bearer = self.app.mcp_listener.connection()['config']['mcpServers']['yingxu']['headers']['Authorization']
        with patch.object(self.app.mcp, 'handle', side_effect=AssertionError('unsupported transfer must not dispatch')) as handle:
            result = self.rejection('/mcp', 400, headers={'Authorization': bearer, 'Transfer-Encoding': 'chunked'}, body=b'0\r\n\r\n')
            handle.assert_not_called()
        self.assertIn('error', result)


if __name__ == '__main__':
    unittest.main()
