"""Official redirect caching uses synthetic responses, never external traffic."""
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from yingxu.range_zip import Network, UpdateError, MAX_REDIRECT_CACHE, REDIRECT_CACHE_SECONDS

URL = 'https://github.com/NOXEVYR/yingxu/releases/download/yingxu-v0.4.19/YingXu-v0.4.19-Windows-x64.zip'
SIGNED = 'https://release-assets.githubusercontent.com/asset?signature=synthetic-private-value'


class Response(io.BytesIO):
    def __init__(self, url=SIGNED, status=206, content=b'abcd', etag='"asset"', start=0):
        super().__init__(content)
        self.status = status
        self.headers = {'Content-Length': str(len(content)), 'Content-Range': f'bytes {start}-{start+len(content)-1}/100',
                        'ETag': etag}
        self.url = url

    def geturl(self):
        return self.url


class IncrementalNetworkTests(unittest.TestCase):
    def get(self, network, start=0):
        return network.get(URL, 4, start, start+3, 100)

    def test_ranges_reuse_final_official_url_and_keep_public_etag_identity(self):
        network = Network()
        requests = []

        def respond(request):
            requests.append(request)
            return Response(start=4 if len(requests) == 2 else 0)

        with patch.object(network, '_open', side_effect=respond):
            self.assertEqual(self.get(network), b'abcd')
            self.assertEqual(self.get(network, 4), b'abcd')
        self.assertEqual([request.full_url for request in requests], [URL, SIGNED])
        self.assertEqual(requests[1].get_header('Range'), 'bytes=4-7')
        self.assertEqual(requests[1].get_header('If-match'), '"asset"')
        self.assertEqual(network.etags, {URL: '"asset"'})
        self.assertEqual((network.requests, network.received), (2, 8))
        self.assertNotIn('synthetic-private-value', repr(network.etags))

    def test_expired_cached_signature_refreshes_public_url_once_with_same_range(self):
        for status in (401, 403):
            with self.subTest(status=status):
                network = Network()
                error_body = io.BytesIO(b'not a range')
                expired = HTTPError(SIGNED, status, 'expired', {}, error_body)
                renewed = SIGNED.replace('synthetic-private-value', 'renewed')
                with patch.object(network, '_open', side_effect=[Response(), expired, Response(renewed, start=4)]) as opened:
                    self.get(network)
                    self.assertEqual(self.get(network, 4), b'abcd')
                requests = [call.args[0] for call in opened.call_args_list]
                self.assertEqual([request.full_url for request in requests], [URL, SIGNED, URL])
                self.assertEqual([request.get_header('Range') for request in requests[1:]], ['bytes=4-7']*2)
                self.assertEqual([request.get_header('If-match') for request in requests[1:]], ['"asset"']*2)
                self.assertTrue(error_body.closed)
                self.assertEqual(network.requests, 3)
                self.assertEqual(network.received, 8)
                self.assertEqual(network._redirects[URL][0], renewed)

    def test_failed_refresh_and_other_http_errors_do_not_loop_or_fallback_to_full_archive(self):
        for status in (403, 429, 500):
            with self.subTest(status=status):
                network = Network()
                errors = [HTTPError(SIGNED, status, 'failure', {}, None)]
                if status == 403:
                    errors.append(HTTPError(URL, 403, 'failure', {}, None))
                with patch.object(network, '_open', side_effect=[Response(), *errors]) as opened:
                    self.get(network)
                    with self.assertRaises(HTTPError):
                        self.get(network)
                self.assertEqual(opened.call_count, 3 if status == 403 else 2)
                self.assertTrue(all(call.args[0].get_header('Range') == 'bytes=0-3' for call in opened.call_args_list))

    def test_cache_is_bounded_expires_and_never_accepts_untrusted_final_urls(self):
        network = Network()
        with patch('yingxu.range_zip.time.monotonic', return_value=0), \
                patch.object(network, '_open', side_effect=lambda request: Response()):
            for index in range(MAX_REDIRECT_CACHE+1):
                network.get(URL + str(index), 4, 0, 3, 100)
        self.assertEqual(len(network._redirects), MAX_REDIRECT_CACHE)
        self.assertNotIn(URL + '0', network._redirects)
        with patch.object(network, '_open', return_value=Response('https://evil.invalid/private?signature=hidden')):
            with self.assertRaises(UpdateError):
                self.get(network)
        self.assertNotIn(URL, network._redirects)
        with patch('yingxu.range_zip.time.monotonic', return_value=0), patch.object(network, '_open', return_value=Response()):
            self.get(network)
        with patch('yingxu.range_zip.time.monotonic', return_value=REDIRECT_CACHE_SECONDS+1), \
                patch.object(network, '_open', return_value=Response()) as opened:
            self.get(network)
            self.assertEqual(opened.call_args.args[0].full_url, URL)

    def test_cached_response_still_enforces_range_etag_size_and_transfer_budget(self):
        for invalid in ('no-range', 'wrong-range', 'etag', 'budget'):
            with self.subTest(invalid=invalid):
                network = Network(max_bytes=7 if invalid == 'budget' else 100)
                bad = Response(status=200 if invalid == 'no-range' else 206,
                               etag='"changed"' if invalid == 'etag' else '"asset"')
                if invalid == 'wrong-range':
                    bad.headers['Content-Range'] = 'bytes 1-4/100'
                with patch.object(network, '_open', side_effect=[Response(), bad]) as opened:
                    self.get(network)
                    with self.assertRaises(UpdateError):
                        self.get(network)
                self.assertEqual(opened.call_count, 2)

    def test_cached_expiry_retry_respects_request_budget_and_cancellation(self):
        network = Network()
        with patch.object(network, '_open', return_value=Response()):
            self.get(network)
        network.requests = 49999
        with patch.object(network, '_open', side_effect=HTTPError(SIGNED, 403, 'expired', {}, None)) as opened:
            with self.assertRaises(UpdateError):
                self.get(network)
            self.assertEqual(opened.call_count, 1)
        network = Network()
        with patch.object(network, '_open', return_value=Response()):
            self.get(network)
        network.cancelled = lambda: True
        with patch.object(network, '_open') as opened:
            with self.assertRaises(UpdateError):
                self.get(network)
            opened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
