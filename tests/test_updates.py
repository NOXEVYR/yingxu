import io
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError

from yingxu import updates
from yingxu.store import UserError
from test_incremental_support import Fixture


def response(payload, link=''):
    stream=io.BytesIO(payload);stream.headers={'Link':link};return stream


def release(version, mac=False, **extra):
    return dict(tag_name='yingxu-v'+version, draft=False, prerelease=mac,
                assets=[{'name':f'YingXu-v{version}-'+('macOS-arm64.zip' if mac else 'Windows-x64.zip'),
                         'size':123, 'state':'uploaded'}], **extra)


class UpdatesTests(unittest.TestCase):
    def test_new_version_exposes_exact_bound_build_without_archive_requests(self):
        # The same release tag can be replaced after a previous plan was
        # cached: the check must expose the currently published build target.
        for build in ('workflow.2', 'workflow.3'):
            with self.subTest(build=build), Fixture(new_version='0.4.22', old_version='0.4.21',
                                                  old_build='workflow.1', new_build=build) as fixture:
                updates._cache = None
                opener = Mock()
                opener.open.return_value = response(json.dumps(fixture.releases).encode())
                with patch.object(updates.sys, 'platform', 'win32'), patch.object(updates, '__version__', '0.4.21'), \
                        patch.object(updates, '__build__', 'workflow.1'), patch.object(updates, 'update_opener', return_value=opener):
                    result = updates.check_update()
                self.assertTrue(result['update_available'])
                self.assertEqual(result['update_kind'], 'version')
                self.assertEqual(result['latest_version'], '0.4.22')
                self.assertEqual(result['latest_build'], build)
                self.assertEqual([kind for kind, _, _ in fixture.requests], ['manifest'])

    def test_new_version_legacy_contract_preserves_detection_with_unknown_build(self):
        with Fixture(new_version='0.4.22', old_version='0.4.21') as fixture:
            opener = Mock()
            opener.open.return_value = response(json.dumps(fixture.releases).encode())
            with patch.object(updates.sys, 'platform', 'win32'), patch.object(updates, '__version__', '0.4.21'), \
                    patch.object(updates, 'update_opener', return_value=opener):
                result = updates.check_update()
            self.assertTrue(result['update_available'])
            self.assertEqual(result['update_kind'], 'version')
            self.assertEqual(result['latest_build'], '')
            self.assertFalse(result['identity_verified'])
            self.assertIn('重新核对', result['message'])
            self.assertEqual([kind for kind, _, _ in fixture.requests], ['manifest'])

    def test_new_version_invalid_or_oversize_contract_cannot_supply_build(self):
        for oversized in (False, True):
            with self.subTest(oversized=oversized), Fixture(new_version='0.4.22', old_version='0.4.21',
                                                          new_build='workflow.3') as fixture:
                if oversized:
                    fixture.metadata['padding'] = 'x' * (1024 * 1024)
                else:
                    fixture.metadata['sha256'] = '0' * 64
                fixture.refresh_metadata()
                updates._cache = None
                opener = Mock()
                opener.open.return_value = response(json.dumps(fixture.releases).encode())
                with patch.object(updates.sys, 'platform', 'win32'), patch.object(updates, '__version__', '0.4.21'), \
                        patch.object(updates, 'update_opener', return_value=opener):
                    if oversized:
                        # The existing asset selector already rejects external
                        # manifests above its stricter 128 KiB limit.
                        result = updates.check_update()
                        self.assertTrue(result['update_available'])
                        self.assertEqual(result['latest_build'], '')
                        self.assertFalse(result['identity_verified'])
                    else:
                        with self.assertRaises(UserError):
                            updates.check_update()
                        self.assertIsNone(updates._cache)
                self.assertFalse(any(kind == 'zip' for kind, _, _ in fixture.requests))
                if oversized:
                    self.assertEqual(fixture.requests, [])

    def test_build_comparison_semantic_version_priority_and_manual_boundary(self):
        rows = [release('0.4.22')]
        for build, kind in [('workflow.3', 'build'), ('workflow.2', 'current'),
                            ('workflow.1', 'current'), ('patch.3', 'manual'), ('', 'manual'),
                            ('workflow.03', 'manual')]:
            result = updates.select_release(rows, 'win32', '0.4.22', 'workflow.2', build)
            self.assertEqual(result['update_kind'], kind)
            self.assertEqual(result['update_available'], kind == 'build')
        self.assertEqual(updates.select_release([release('0.4.23')], 'win32', '0.4.22',
                                               'workflow.9', 'patch.1')['update_kind'], 'version')
        self.assertFalse(updates.select_release([release('0.4.21')], 'win32', '0.4.22',
                                                'workflow.1', 'workflow.99')['update_available'])

    def test_check_update_current_build_and_verified_same_version_range(self):
        with Fixture(new_version='0.4.22', old_version='0.4.22', old_build='workflow.2', new_build='workflow.3') as fixture:
            opener = Mock()
            opener.open.return_value = response(json.dumps(fixture.releases).encode())
            with patch.object(updates.sys, 'platform', 'win32'), patch.object(updates, '__version__', '0.4.22'), \
                    patch.object(updates, '__build__', 'workflow.2'), patch.object(updates, 'update_opener', return_value=opener):
                result = updates.check_update()
                self.assertTrue(result['update_available'])
                self.assertEqual(result['update_kind'], 'build')
                self.assertEqual(result['current_build'], 'workflow.2')
                self.assertEqual(result['latest_build'], 'workflow.3')
                self.assertEqual(updates.check_update(), result)
            self.assertTrue(any(kind == 'zip' for kind, _, _ in fixture.requests))
            left, right = fixture.data_range('runtime/python313.zip')
            self.assertFalse(any(kind == 'zip' and start <= right and end >= left for kind, start, end in fixture.requests))

    def test_equal_version_unbound_release_is_manual_and_no_extra_request(self):
        opener = Mock()
        opener.open.return_value = response(json.dumps([release('0.4.11')]).encode())
        with patch.object(updates.sys, 'platform', 'win32'), patch.object(updates, '__build__', 'workflow.2'), \
                patch.object(updates, 'update_opener', return_value=opener):
            result = updates.check_update()
        self.assertFalse(result['update_available'])
        self.assertEqual(result['update_kind'], 'manual')
        self.assertIn('人工核对', result['message'])
        opener.open.assert_called_once()

    def test_equal_version_external_internal_build_mismatch_is_not_available(self):
        with Fixture(new_version='0.4.22', old_version='0.4.22', old_build='workflow.2', new_build='workflow.4') as fixture:
            fixture.metadata['build_revision'] = 'workflow.3'
            fixture.refresh_metadata()
            opener = Mock()
            opener.open.return_value = response(json.dumps(fixture.releases).encode())
            with patch.object(updates.sys, 'platform', 'win32'), patch.object(updates, '__version__', '0.4.22'), \
                    patch.object(updates, '__build__', 'workflow.2'), patch.object(updates, 'update_opener', return_value=opener):
                with self.assertRaises(UserError):
                    updates.check_update()
            self.assertIsNone(updates._cache)

    def setUp(self):
        updates._cache = None
        version=patch.object(updates,'__version__','0.4.11');version.start();self.addCleanup(version.stop)
        version = patch.object(updates, "__version__", "0.4.11")
        version.start(); self.addCleanup(version.stop)

    def test_numeric_platform_and_incomplete_release_selection(self):
        entries = [release('0.4.9'),release('0.4.12-mac.1',True),release('0.4.10')]
        incomplete = release('0.4.99');incomplete['assets']=[];entries.append(incomplete)
        draft = release('0.5.0');draft['draft']=True;entries.append(draft)
        preview = release('0.6.0');preview['prerelease']=True;entries.append(preview)
        result=updates.select_release(entries,'win32','0.4.9')
        self.assertEqual(result['latest_version'],'0.4.10');self.assertTrue(result['update_available'])
        self.assertFalse(updates.select_release(entries,'win32','0.4.11')['update_available'])
        self.assertEqual(updates.select_release(entries,'darwin','0.4.11-mac.1')['latest_version'],'0.4.12-mac.1')
        self.assertTrue(updates.select_release([release('0.4.11-mac.10',True)],'darwin','0.4.11-mac.2')['update_available'])

    def test_missing_assets_and_invalid_inputs_never_report_current(self):
        for rows,platform,current in [([], 'win32','0.4.11'),([release('0.4.11')],'darwin','0.4.11-mac.1'),([], 'linux','0.4.11'),([], 'win32','garbage')]:
            with self.assertRaises(UserError):updates.select_release(rows,platform,current)

    def test_fixed_public_request_timeout_cache_and_no_redirect(self):
        opener=Mock();opener.open.return_value=response(json.dumps([release('0.4.12')]).encode())
        with patch.object(updates.sys,'platform','win32'),patch.object(updates,'update_opener',return_value=opener) as factory:
            result=updates.check_update();self.assertEqual(updates.check_update(),result)
        factory.assert_called_once()
        active, deadline, redirect = factory.call_args.args
        self.assertIsNone(active())
        self.assertGreater(deadline, updates.time.monotonic())
        self.assertLessEqual(deadline, updates.time.monotonic() + 24)
        self.assertIsInstance(redirect, updates.NoRedirect)
        opener.open.assert_called_once()
        req=opener.open.call_args.args[0]
        self.assertEqual(req.full_url,updates.RELEASES)
        self.assertIsNone(req.data);self.assertNotIn('Authorization',req.headers)
        self.assertEqual(opener.open.call_args.kwargs['timeout'],8)
        self.assertIsNone(updates.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.invalid'))

    def test_network_errors_oversize_and_invalid_json_are_failures(self):
        for payload in [b'not json',b'x'*(updates.MAX_RESPONSE+1),b'{}']:
            opener=Mock();opener.open.return_value=response(payload)
            with patch.object(updates.sys,'platform','win32'),patch.object(updates,'update_opener',return_value=opener):
                with self.assertRaises(UserError):updates.check_update()
            self.assertIsNone(updates._cache)
        for error in [TimeoutError(),HTTPError(updates.RELEASES,403,'limited',{},None)]:
            opener=Mock();opener.open.side_effect=error
            with patch.object(updates.sys,'platform','win32'),patch.object(updates,'update_opener',return_value=opener):
                with self.assertRaises(UserError):updates.check_update()

    def test_real_drip_body_deadline_closes_response_and_releases_lock(self):
        paths = []
        peer_closed = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                paths.append(self.path)
                dripping = len(paths) == 1
                payload = b'[]     ' if dripping or len(paths) == 2 else json.dumps([release('0.4.12')]).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                if len(paths) == 2:
                    self.send_header('Link', '<https://evil.invalid>; rel="next"')
                self.end_headers()
                if not dripping:
                    self.wfile.write(payload)
                    return
                try:
                    for index, byte in enumerate(payload):
                        if index:
                            time.sleep(.08)
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                except ConnectionError:
                    peer_closed.set()
                # Observe the real client connection closing, without wrapping
                # or mocking the response returned by the production opener.
                self.connection.settimeout(1)
                try:
                    if self.connection.recv(1) == b'':
                        peer_closed.set()
                except ConnectionError:
                    peer_closed.set()
                except socket.timeout:
                    pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/releases?per_page=100'
            with patch.object(updates, 'RELEASES', url), patch.object(updates, 'RELEASE_LIST_SECONDS', .2):
                started = time.monotonic()
                with self.assertRaises(UserError) as failure:
                    updates.check_update()
                elapsed = time.monotonic() - started
                self.assertIsInstance(failure.exception.__cause__, TimeoutError)
                self.assertLess(elapsed, .4)
                self.assertIsNone(updates._cache)
                self.assertTrue(updates._lock.acquire(blocking=False))
                updates._lock.release()
                self.assertTrue(peer_closed.wait(2), 'The timed-out HTTP response must close its socket')
                result = updates.check_update()
                self.assertTrue(result['update_available'])
                self.assertEqual(updates.check_update(), result)
                self.assertEqual(paths, ['/releases?per_page=100', '/releases?per_page=100',
                                         '/releases?per_page=100&page=2'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_bounded_pagination_does_not_follow_supplied_url(self):
        opener=Mock();opener.open.side_effect=[response(b'[]','<https://evil.invalid>; rel="next"'),response(json.dumps([release('0.4.12')]).encode())]
        with patch.object(updates.sys,'platform','win32'),patch.object(updates,'update_opener',return_value=opener):
            self.assertTrue(updates.check_update()['update_available'])
        self.assertEqual(opener.open.call_args.args[0].full_url,updates.RELEASES+'&page=2')
        updates._cache=None
        opener.open.side_effect=[response(b'[]','<x>; rel="next"') for _ in range(3)]
        with patch.object(updates.sys,'platform','win32'),patch.object(updates,'update_opener',return_value=opener),self.assertRaises(UserError):
            updates.check_update()

    def test_singleflight_and_browser_target_validation(self):
        with updates._lock:
            with patch.object(updates.sys,'platform','win32'),self.assertRaises(UserError):updates.check_update()
        with patch.object(updates.sys,'platform','win32'),patch.object(updates.os,'startfile',create=True) as launch:
            for tag in ['https://evil.invalid','yingxu-v0.4.12/evil','yingxu-v0.4.12-mac.1',None]:
                with self.assertRaises(UserError):updates.open_release(tag)
            launch.assert_not_called();updates.open_release('yingxu-v0.4.12')
            launch.assert_called_once_with(updates.PAGE+'yingxu-v0.4.12')
        with patch.object(updates.sys,'platform','darwin'),patch.object(updates.subprocess,'Popen') as launch:
            updates.open_release('yingxu-v0.4.11-mac.2')
            self.assertEqual(launch.call_args.args[0],['open',updates.PAGE+'yingxu-v0.4.11-mac.2'])

    def test_server_import_does_not_load_checker(self):
        code="import sys; from pathlib import Path; sys.path.insert(0,str(Path.cwd())); import server; assert 'yingxu.updates' not in sys.modules"
        subprocess.run([sys.executable,'-B','-c',code],check=True)
