import http.client
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server import Server


class StaticRootTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='yingxu-static-root-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bundle = self.root/'Bundle'
        self.bundle.mkdir()
        self.resources = self.root/'Resources'/'frontend'
        self.resources.mkdir(parents=True)
        (self.resources/'index.html').write_bytes(b'<h1>synthetic app</h1>')
        (self.resources/'canvas').mkdir()
        (self.resources/'canvas'/'app.js').write_bytes(b'const synthetic = true;')
        self.outside = self.root/'outside.js'
        self.outside.write_bytes(b'private synthetic data')
        self.root_patch = patch('server.ROOT', self.bundle)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        # Static routes need no application database, scan or worker pool.
        self.server = Server(('127.0.0.1', 0), object())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def symlink(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError) as error:
            self.skipTest('Symlink creation unavailable: '+str(error))

    def request(self, path, method='GET'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_bundled_frontend_root_symlink_serves_index_and_nested_assets(self):
        self.symlink(self.bundle/'frontend', self.resources)
        for url, expected in [('/', b'<h1>synthetic app</h1>'), ('/canvas/app.js', b'const synthetic = true;')]:
            status, body, headers = self.request(url)
            self.assertEqual(status, 200)
            self.assertEqual(body, expected)
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.request('/', 'HEAD')[:2], (200, b''))

    def test_bundled_root_still_rejects_leaf_and_nested_symlinks_outside_frontend(self):
        self.symlink(self.bundle/'frontend', self.resources)
        self.symlink(self.resources/'leak.js', self.outside)
        self.symlink(self.resources/'escape', self.root)
        for url in ['/leak.js', '/escape/outside.js', '/../../outside.js']:
            status, body, _ = self.request(url)
            self.assertEqual(status, 404, url)
            self.assertNotIn(b'private synthetic data', body)

    def test_plain_frontend_root_keeps_traversal_and_suffix_guards(self):
        frontend = self.bundle/'frontend'
        frontend.mkdir()
        (frontend/'index.html').write_bytes(b'plain synthetic app')
        (frontend/'private.txt').write_bytes(b'not a static asset')
        (self.bundle/'outside.js').write_bytes(b'private synthetic data')
        self.assertEqual(self.request('/')[:2], (200, b'plain synthetic app'))
        for url in ['/../outside.js', '/%2e%2e/outside.js', '/private.txt', '/missing.js']:
            self.assertEqual(self.request(url)[0], 404, url)


if __name__ == '__main__':
    unittest.main()
