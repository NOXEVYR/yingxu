"""Connection racing is bounded and update-only; all real traffic is loopback."""
import errno
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import ssl
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.request
from unittest.mock import patch

from yingxu import update_network as network
from yingxu.range_zip import UpdateError, _Redirect


def address(family, host):
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
            (host, 443) if family == socket.AF_INET else (host, 443, 0, 0))


class Clock:
    value = 0

    def __call__(self):
        return self.value


class FakeSocket:
    def __init__(self, family, clock, code, sockets):
        self.family, self.clock, self.code, self.sockets = family, clock, code, sockets
        self.closed, self.bound, self.timeout = False, None, None

    def setblocking(self, value):
        self.blocking = value

    def bind(self, source):
        if source[1] and any(other is not self and not other.closed and other.bound == source for other in self.sockets):
            raise OSError(errno.EADDRINUSE, 'bound')
        self.bound = source

    def connect_ex(self, destination):
        self.destination, self.started = destination, self.clock()
        return self.code

    def settimeout(self, value):
        self.timeout = value

    def close(self):
        self.closed = True


class FakeSelector:
    def __init__(self, clock):
        self.clock, self.entries, self.closed = clock, set(), False

    def register(self, candidate, _events):
        self.entries.add(candidate)

    def unregister(self, candidate):
        self.entries.remove(candidate)

    def select(self, seconds):
        self.clock.value += seconds
        return []

    def close(self):
        self.closed = True


class UpdateNetworkTests(unittest.TestCase):
    def race(self, records, codes, clock=None, **kwargs):
        clock = clock or Clock()
        opened = []
        selector = FakeSelector(clock)

        def make_socket(family, kind, protocol):
            self.assertEqual(kind, socket.SOCK_STREAM)
            candidate = FakeSocket(family, clock, codes[len(opened)], opened)
            opened.append(candidate)
            return candidate

        with patch.object(network.socket, 'getaddrinfo', return_value=records), \
                patch.object(network.socket, 'socket', side_effect=make_socket), \
                patch.object(network.selectors, 'DefaultSelector', return_value=selector), \
                patch.object(network.time, 'monotonic', clock):
            try:
                result = network.create_connection(('official.invalid', 443), timeout=2, **kwargs)
            except Exception as error:
                return error, opened, selector
        return result, opened, selector

    def test_unreachable_first_address_yields_other_family_at_250_ms(self):
        records = [address(socket.AF_INET, '192.0.2.1'), address(socket.AF_INET, '192.0.2.2'),
                   address(socket.AF_INET6, '2001:db8::1')]
        result, opened, selector = self.race(records, [errno.EINPROGRESS, 0])
        self.assertIs(result, opened[1])
        self.assertEqual([candidate.family for candidate in opened], [socket.AF_INET, socket.AF_INET6])
        self.assertEqual(opened[0].started, 0)
        self.assertAlmostEqual(opened[1].started, .25)
        self.assertTrue(opened[0].closed)
        self.assertFalse(opened[1].closed)
        self.assertTrue(selector.closed)
        result.close()

    def test_system_preferred_ipv6_is_preserved_and_immediate_failure_does_not_wait(self):
        records = [address(socket.AF_INET6, '2001:db8::1'), address(socket.AF_INET, '192.0.2.1')]
        result, opened, _ = self.race(records, [errno.ENETUNREACH, 0])
        self.assertEqual([candidate.family for candidate in opened], [socket.AF_INET6, socket.AF_INET])
        self.assertEqual(opened[1].started, 0)
        self.assertTrue(opened[0].closed)
        result.close()

    def test_all_errors_deadline_and_cancellation_close_every_socket(self):
        records = [address(socket.AF_INET, '192.0.2.1'), address(socket.AF_INET6, '2001:db8::1')]
        result, opened, selector = self.race(records, [errno.ECONNREFUSED]*2)
        self.assertIsInstance(result, OSError)
        self.assertEqual(result.errno, errno.ECONNREFUSED)
        self.assertTrue(all(candidate.closed for candidate in opened))
        self.assertTrue(selector.closed)
        clock = Clock()
        result, opened, _ = self.race(records, [errno.EINPROGRESS]*2, clock, deadline=.1)
        self.assertIsInstance(result, TimeoutError)
        self.assertAlmostEqual(clock.value, .1)
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].closed)
        clock = Clock()

        def cancelled():
            if clock.value >= .1:
                raise UpdateError('cancelled')

        result, opened, selector = self.race(records, [errno.EINPROGRESS]*2, clock, active=cancelled)
        self.assertIsInstance(result, UpdateError)
        self.assertTrue(all(candidate.closed for candidate in opened))
        self.assertTrue(selector.closed)

    def test_resolution_is_checked_against_budget_before_any_socket_is_opened(self):
        clock = Clock()

        def resolve(*_args):
            clock.value = 1
            return [address(socket.AF_INET, '192.0.2.1')]

        with patch.object(network.time, 'monotonic', clock), \
                patch.object(network.socket, 'getaddrinfo', side_effect=resolve), \
                patch.object(network.socket, 'socket') as create:
            with self.assertRaises(TimeoutError):
                network.create_connection(('official.invalid', 443), timeout=.5)
            create.assert_not_called()

    def test_source_binding_is_preserved_and_fixed_port_releases_prior_attempt(self):
        records = [address(socket.AF_INET, '192.0.2.1'), address(socket.AF_INET, '192.0.2.2')]
        result, opened, _ = self.race(records, [errno.EINPROGRESS, 0], source_address=('127.0.0.1', 12345))
        self.assertIs(result, opened[1])
        self.assertEqual([candidate.bound for candidate in opened], [('127.0.0.1', 12345)]*2)
        self.assertTrue(opened[0].closed)
        result.close()

    def test_real_loopback_connection_preserves_source_and_restores_blocking_timeout(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            connection = network.create_connection(listener.getsockname(), timeout=2,
                                                   source_address=('127.0.0.1', 0))
            try:
                with listener.accept()[0] as peer:
                    connection.sendall(b'ping')
                    self.assertEqual(peer.recv(4), b'ping')
                    self.assertEqual(connection.getsockname()[0], '127.0.0.1')
                    self.assertGreater(connection.gettimeout(), 0)
                    self.assertTrue(connection.getblocking())
            finally:
                connection.close()

    def test_update_handlers_keep_tls_validation_and_do_not_modify_global_connection(self):
        original = socket.create_connection
        handler = network.UpdateHTTPSHandler(lambda: None, time.monotonic()+3)
        connection = handler._connection('api.github.com', timeout=2)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(connection._context.check_hostname)
        self.assertIs(connection._create_connection.func, network.create_connection)
        self.assertIs(socket.create_connection, original)
        self.assertIsNot(http.client.HTTPConnection('localhost')._create_connection, connection._create_connection)

    def test_proxy_connect_uses_proxy_endpoint_and_retains_origin_sni(self):
        calls, sni = [], []

        class Proxy(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args):
                pass

            def do_CONNECT(self):
                calls.append(('CONNECT', self.path))
                self.send_response(200)
                self.end_headers()
                self.close_connection = False

            def do_GET(self):
                calls.append(('GET', self.path))
                self.send_response(200)
                self.send_header('Content-Length', '4')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(b'ping')

        # TLS bytes are deliberately stubbed only in this endpoint/SNI test.
        # Default production certificate validation is checked separately.
        context = SimpleNamespace(verify_mode=ssl.CERT_REQUIRED, check_hostname=True,
                                  wrap_socket=lambda candidate, server_hostname: (sni.append(server_hostname), candidate)[1])
        server = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            handler = network.UpdateHTTPSHandler(lambda: None, time.monotonic()+3, context=context)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': f'http://127.0.0.1:{server.server_port}'}), handler)
            with patch('urllib.request.proxy_bypass', return_value=False), \
                    patch.object(network.socket, 'getaddrinfo', wraps=socket.getaddrinfo) as resolve:
                with opener.open('https://api.github.com/fixture', timeout=2) as response:
                    self.assertEqual(response.read(), b'ping')
                self.assertEqual(resolve.call_args.args[:2], ('127.0.0.1', server.server_port))
            self.assertEqual(calls, [('CONNECT', 'api.github.com:443'), ('GET', '/fixture')])
            self.assertEqual(sni, ['api.github.com'])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_opener_retains_standard_system_proxy_handler(self):
        with patch('urllib.request.getproxies', return_value={'https': 'http://proxy.invalid:8080'}):
            opener = network.update_opener(lambda: None, time.monotonic()+3, _Redirect())
        proxy = next(handler for handler in opener.handlers if isinstance(handler, urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {'https': 'http://proxy.invalid:8080'})


if __name__ == '__main__':
    unittest.main()
