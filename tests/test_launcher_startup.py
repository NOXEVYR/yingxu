"""Loopback startup fast path: an absent listener is never a health proof."""
import ctypes
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


class LauncherStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="yingxu-launcher-test-")
        self.root = Path(self.temporary.name).resolve()
        self.environment = patch.dict(os.environ, {
            "YINGXU_DATA_DIR": str(self.root / "data"),
            "YINGXU_PROJECTS_DIR": str(self.root / "projects"),
        })
        self.environment.start()
        loader = importlib.machinery.SourceFileLoader(
            "launcher_startup_fixture", str(Path(__file__).resolve().parents[1] / "launcher.pyw"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.launcher = importlib.util.module_from_spec(spec)
        loader.exec_module(self.launcher)

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    @unittest.skipUnless(sys.platform == "win32", "Windows listener table")
    def test_real_listener_table_detects_bound_and_closed_port(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertIs(self.launcher.listening_port(port), True)
        self.assertIs(self.launcher.listening_port(port), False)

    def test_absent_listener_skips_both_socket_probes(self):
        with patch.object(self.launcher, "listening_port", return_value=False), \
                patch.object(self.launcher.http.client, "HTTPConnection") as http, \
                patch.object(self.launcher.socket, "socket") as connect:
            self.assertIsNone(self.launcher.health(12345))
            self.assertFalse(self.launcher.port_busy(12345))
            http.assert_not_called()
            connect.assert_not_called()

    def test_query_failure_falls_back_instead_of_guessing_absence(self):
        with patch.object(self.launcher.sys, "platform", "win32"), \
                patch.object(ctypes, "WinDLL", side_effect=OSError("unavailable"), create=True):
            self.assertIsNone(self.launcher.listening_port(12345))

    def test_present_or_unknown_listener_requires_real_http_identity(self):
        launcher = self.launcher
        body = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                for hint in (True, None):
                    with self.subTest(hint=hint), patch.object(launcher, "listening_port", return_value=hint):
                        body.update(app="foreign", ok=True, instance_id=launcher.instance_id(launcher.DATA))
                        self.assertIsNone(launcher.health(port))
                        body.update(app="yingxu", instance_id="other-data")
                        self.assertIsNone(launcher.health(port))
                        body.update(instance_id=launcher.instance_id(launcher.DATA), version=launcher.__version__)
                        self.assertEqual(launcher.require_current_service(launcher.health(port)), body)
                        body.update(version="0.0.0")
                        with self.assertRaises(RuntimeError):
                            launcher.require_current_service(launcher.health(port))
                        self.assertTrue(launcher.port_busy(port))
            finally:
                server.shutdown()
                thread.join(3)

    def test_foreign_service_racing_negative_hint_is_not_replaced(self):
        launcher = self.launcher
        # The first optimistic probe misses a concurrent listener. After taking
        # the launch mutex, the real port check must still block a new process.
        kernel = unittest.mock.Mock()
        with patch.object(launcher, "health", return_value=None), \
                patch.object(launcher, "acquire_mutex", return_value=(kernel, 123)), \
                patch.object(launcher, "port_busy", return_value=True), \
                patch.object(launcher.subprocess, "Popen") as start:
            with self.assertRaisesRegex(RuntimeError, "12345"):
                launcher.ensure_running(12345)
            start.assert_not_called()
            kernel.ReleaseMutex.assert_called_once_with(123)
            kernel.CloseHandle.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
