import hashlib
import io
import json
import stat
import struct
import unittest
import urllib.request
import warnings
import zipfile
import zlib

from yingxu.range_zip import (RangeZip, UpdateError, Network, _Redirect, safe_url,
                             validate_manifest, MAX_MEMBER, version_key)
from test_incremental_support import Fixture, manifest

URL = 'https://github.com/NOXEVYR/yingxu/releases/download/yingxu-v0.4.19/YingXu-v0.4.19-Windows-x64.zip'


class MemoryNetwork:
    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def get(self, url, limit, start=None, end=None, total=None):
        if total != len(self.raw) or end - start + 1 != limit:
            raise AssertionError('invalid range request')
        self.calls.append((start, end))
        return self.raw[start:end + 1]

    def active(self):
        pass


def make_zip(entries, method=zipfile.ZIP_STORED, info_hook=None):
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(output, 'w') as archive:
            for name, content in entries:
                info = zipfile.ZipInfo(name)
                info.compress_type = method
                if info_hook:
                    info_hook(info)
                archive.writestr(info, content)
    return output.getvalue()


class RangeZipSecurityTests(unittest.TestCase):
    def reader(self, raw):
        return RangeZip(URL, len(raw), MemoryNetwork(raw))

    def test_unsafe_paths_duplicates_and_case_aliases(self):
        batches = [ [('YingXu/../escape.py', b'x')], [('YingXu/frontend/x.js:payload', b'x')],
                    [('YingXu/frontend/CON.txt', b'x')], [('YingXu/frontend/x.js.', b'x')],
                    [('YingXu/projects/private.txt', b'x')], [('YingXu/LOCAL_PATCH_MANIFEST.json', b'x')],
                    [('YingXu/server.py', b'a'), ('YingXu/server.py', b'b')],
                    [('YingXu/frontend/a.js', b'a'), ('YingXu/Frontend/b.js', b'b')],
                    [('YingXu/frontend/a.js', b'a'), ('YingXu/frontend/A.js', b'b')],
                    [('YingXu/frontend/a.js', b'a'), ('YingXu/frontend/a.js/b.js', b'b')]]
        for entries in batches:
            with self.subTest(entries=entries), self.assertRaises(UpdateError):
                self.reader(make_zip(entries))

    def test_symlinks_encryption_unsupported_compression_and_bombs(self):
        def symlink(info):
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(UpdateError):
            self.reader(make_zip([('YingXu/server.py', b'target')], info_hook=symlink))
        with self.assertRaises(UpdateError):
            self.reader(make_zip([('YingXu/server.py', b'a')], zipfile.ZIP_BZIP2))
        with self.assertRaises(UpdateError):
            self.reader(make_zip([('YingXu/server.py', b'A' * (3 * 1024**2))], zipfile.ZIP_DEFLATED))
        raw = make_zip([('YingXu/server.py', b'normal')])
        central = raw.index(b'PK\x01\x02')
        for field, code, value in [(central + 8, '<H', 1), (central + 24, '<I', MAX_MEMBER + 1),
                                   (central + 42, '<I', len(raw))]:
            broken = bytearray(raw)
            struct.pack_into(code, broken, field, value)
            with self.assertRaises(UpdateError):
                self.reader(bytes(broken))

    def test_local_name_and_header_must_match_directory(self):
        original = make_zip([('YingXu/server.py', b'payload')])
        for offset in (30, 8):
            raw = bytearray(original)
            raw[offset] ^= 1
            archive = self.reader(bytes(raw))
            with self.assertRaises(UpdateError):
                archive.extract(archive.members['YingXu/server.py'], io.BytesIO())

    def test_crc_sha_and_truncated_compression_reject_bad_payload(self):
        original = make_zip([('YingXu/server.py', b'repeat ' * 500)], zipfile.ZIP_DEFLATED)
        archive = self.reader(original)
        item = archive.members['YingXu/server.py']
        with self.assertRaises(UpdateError):
            archive.extract(item, io.BytesIO(), '0' * 64)
        corrupted = bytearray(original)
        corrupted[30 + len('YingXu/server.py')] ^= 0xff
        archive = self.reader(bytes(corrupted))
        with self.assertRaises((UpdateError, zlib.error)):
            archive.extract(archive.members['YingXu/server.py'], io.BytesIO())

    def test_valid_stored_deflated_and_empty_members(self):
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            for content in (b'', b'ordinary content' * 100):
                raw = make_zip([('YingXu/server.py', content)], method)
                archive = self.reader(raw)
                output = io.BytesIO()
                archive.extract(archive.members['YingXu/server.py'], output, hashlib.sha256(content).hexdigest())
                self.assertEqual(output.getvalue(), content)

    def test_manifest_rejects_unknown_paths_aliases_wrong_platform_and_version(self):
        files = {'YingXu.exe': b'x', 'server.py': b'y', 'launcher.pyw': b'z', 'frontend/index.html': b'html'}
        good = manifest(files, '0.4.19')
        self.assertEqual(validate_manifest(json.dumps(good).encode(), '0.4.19')[1]['server.py']['bytes'], 1)
        for field, value in [('architecture', 'macOS arm64'), ('application', 'Other'), ('version', '0.4.18')]:
            bad = dict(good, **{field: value})
            with self.assertRaises(UpdateError):
                validate_manifest(json.dumps(bad).encode(), '0.4.19')
        for name in ('../private', 'projects/notes.md', 'frontend/../notes.md', 'LOCAL_PATCH_MANIFEST.json'):
            with self.assertRaises(UpdateError):
                validate_manifest(json.dumps(manifest(dict(files, **{name: b'x'}), '0.4.19')).encode())

    def test_redirects_urls_and_numeric_version_are_restricted(self):
        for url in ('http://github.com/x', 'https://evil.example/', 'file:///a', 'https://github.com:444/',
                    'https://secret@github.com/', 'https://github.com.evil.example/', 'https://github.com/\nheader'):
            with self.assertRaises(UpdateError):
                safe_url(url)
        with self.assertRaises(UpdateError):
            _Redirect().redirect_request(urllib.request.Request(URL), None, 302, '', {}, 'https://evil.example/')
        self.assertGreater(version_key('0.10.0'), version_key('0.9.99'))
        for version in ('latest', '0.4.19-mac.1', '01.2.3', '1.2.3/evil'):
            with self.assertRaises(UpdateError):
                version_key(version)

    def test_etag_change_since_plan_and_cancelled_network_fail(self):
        with Fixture() as fixture:
            manager = fixture.manager()
            self.assertEqual(fixture.plan(manager)['state'], 'planned')
            fixture.etag = '"changed-asset"'
            status = fixture.download(manager)
            self.assertEqual(status['state'], 'error')
            self.assertIn('发生变化', status['message'])
        with self.assertRaises(UpdateError):
            Network(cancelled=lambda: True).get(URL, 22, 0, 21, 100)


if __name__ == '__main__':
    unittest.main()
