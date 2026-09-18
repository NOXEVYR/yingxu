import io
import json
import subprocess
import sys
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError

from yingxu import updates
from yingxu.store import UserError


def response(payload, link=''):
    stream=io.BytesIO(payload);stream.headers={'Link':link};return stream


def release(version, mac=False, **extra):
    return dict(tag_name='yingxu-v'+version, draft=False, prerelease=mac,
                assets=[{'name':f'YingXu-v{version}-'+('macOS-arm64.zip' if mac else 'Windows-x64.zip'),
                         'size':123, 'state':'uploaded'}], **extra)


class UpdatesTests(unittest.TestCase):
    def setUp(self):
        updates._cache = None
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
        with patch.object(updates.sys,'platform','win32'),patch.object(updates,'build_opener',return_value=opener):
            result=updates.check_update();self.assertEqual(updates.check_update(),result)
        opener.open.assert_called_once()
        req=opener.open.call_args.args[0]
        self.assertEqual(req.full_url,updates.RELEASES)
        self.assertIsNone(req.data);self.assertNotIn('Authorization',req.headers)
        self.assertEqual(opener.open.call_args.kwargs['timeout'],8)
        self.assertIsNone(updates.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.invalid'))

    def test_network_errors_oversize_and_invalid_json_are_failures(self):
        for payload in [b'not json',b'x'*(updates.MAX_RESPONSE+1),b'{}']:
            opener=Mock();opener.open.return_value=response(payload)
            with patch.object(updates.sys,'platform','win32'),patch.object(updates,'build_opener',return_value=opener):
                with self.assertRaises(UserError):updates.check_update()
            self.assertIsNone(updates._cache)
        for error in [TimeoutError(),HTTPError(updates.RELEASES,403,'limited',{},None)]:
            opener=Mock();opener.open.side_effect=error
            with patch.object(updates.sys,'platform','win32'),patch.object(updates,'build_opener',return_value=opener):
                with self.assertRaises(UserError):updates.check_update()

    def test_bounded_pagination_does_not_follow_supplied_url(self):
        opener=Mock();opener.open.side_effect=[response(b'[]','<https://evil.invalid>; rel="next"'),response(json.dumps([release('0.4.12')]).encode())]
        with patch.object(updates.sys,'platform','win32'),patch.object(updates,'build_opener',return_value=opener):
            self.assertTrue(updates.check_update()['update_available'])
        self.assertEqual(opener.open.call_args.args[0].full_url,updates.RELEASES+'&page=2')
        updates._cache=None
        opener.open.side_effect=[response(b'[]','<x>; rel="next"') for _ in range(3)]
        with patch.object(updates.sys,'platform','win32'),patch.object(updates,'build_opener',return_value=opener),self.assertRaises(UserError):
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
