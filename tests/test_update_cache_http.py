from test_updates_http import UpdatesHttpTests


class UpdateCacheHttpTests(UpdatesHttpTests):
    def test_cache_preview_and_confirmation_require_auth_and_strict_parameters(self):
        for path, method in [('/api/updates/cache', 'GET'), ('/api/updates/cache/clean', 'POST')]:
            self.assertEqual(self.request(path, method, headers={'X-YingXu-Token':'wrong'})[0], 403)
            self.assertEqual(self.request(path, method, headers={'Origin':'https://evil.invalid'})[0], 403)
        self.assertEqual(self.request('/api/updates/cache?path=unknown', 'GET')[0], 400)
        self.assertEqual(self.request('/api/updates/cache/clean', body={'path':'unknown'})[0], 400)
        self.assertEqual(self.request('/api/updates/cache/clean', body={'preview_id':'a'*32})[0], 409)
        code, preview = self.request('/api/updates/cache', 'GET')
        self.assertEqual(code, 200)
        self.assertEqual(preview['cleanable_plans'], 0)
        self.assertEqual(self.request('/api/updates/cache/clean', body={'preview_id':preview['preview_id']})[0], 200)
        self.assertEqual(self.request('/api/updates/cache/clean', body={'preview_id':preview['preview_id']})[0], 409)

    def test_busy_and_prepared_installation_block_cleanup(self):
        manager = self.app.update_service._manager()
        manager._status['state'] = 'downloading'
        self.assertEqual(self.request('/api/updates/cache', 'GET')[0], 409)
        manager._status['state'] = 'idle'
        self.app.update_service.pending={'ticket':'b'*32,'expires':float('inf')}
        self.assertEqual(self.request('/api/updates/cache', 'GET')[0], 409)
        self.assertEqual(self.request('/api/updates/cache/clean', body={'preview_id':'a'*32})[0], 409)
        self.app.update_service.pending = None
