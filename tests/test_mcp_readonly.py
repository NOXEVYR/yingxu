"""Project-scoped MCP journeys against synthetic data and a loopback server.

Credentials stay in memory and are never included in assertion diagnostics.
These tests do not configure a real AI client or access the installed app.
"""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server import Application, Server
from yingxu.store import UserError


class ProjectMCPHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='yingxu-mcp-test-')
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / 'home'
        self.skill_file = self.home / '.codex/skills/synthetic/SKILL.md'
        self.skill_file.parent.mkdir(parents=True)
        self.skill_file.write_text('---\nname: synthetic\n---\n\nPinned instructions v1\n', encoding='utf-8')
        with patch('yingxu.skills.Path.home', return_value=self.home):
            self.app = Application(self.root / 'data', self.root / 'projects')
        self.app._skills_startup.result(timeout=10)
        self.project = self.app.store.create_project('授权的合成项目')
        self.other = self.app.store.create_project('另一个合成项目')
        self.item = self.make_item('已保存文稿', 'Saved version one\n')
        self.other_item = self.make_item('其他项目文稿', 'Other project private text\n', self.other)
        self.skill = self.app.skills.list()['skills'][0]
        self.server = Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}/mcp'
        self.bearer = ''

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.app.close()
        self.temporary.cleanup()

    def make_item(self, name, content, project=None):
        return self.app.store.create_item({'project_id': (project or self.project)['id'],
                                           'category': 'scripts', 'name': name, 'content': content})

    def request(self, path, body=None, method=None, headers=None, ui=False, raw=None):
        request_headers = {'Content-Type': 'application/json',
                           'Accept': 'application/json, text/event-stream'}
        if ui:
            request_headers.update({'X-YingXu-Token': self.app.token,
                                    'Origin': f'http://127.0.0.1:{self.server.server_port}'})
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        try:
            content = raw if raw is not None else (None if body is None else json.dumps(body).encode('utf-8'))
            connection.request(method or ('GET' if body is None and raw is None else 'POST'),
                               path, content, request_headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), json.loads(data) if data else None
        finally:
            connection.close()

    def ui(self, path, body=None, method=None):
        status, _headers, data = self.request(path, body, method, ui=True)
        return status, data

    def enable(self, project=None):
        status, data = self.ui('/api/mcp/configure', {'enabled': True,
                                                     'project_id': (project or self.project)['id']})
        self.assertEqual(status, 200)
        self.assertTrue(data['enabled'])
        status, data = self.ui('/api/mcp/connection', {})
        self.assertEqual(status, 200)
        config = data['config']
        entry = config['mcpServers']['yingxu']
        self.assertEqual(entry['url'], self.endpoint)
        self.bearer = entry['headers']['Authorization']
        self.assertTrue(self.bearer.startswith('Bearer '))
        self.assertFalse(self.bearer.endswith(self.app.token))
        return self.bearer

    def rpc(self, method, params=None, *, version='2025-11-25', headers=None, notification=False):
        body = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            body['id'] = 7
        if params is not None:
            body['params'] = params
        request_headers = {'Authorization': self.bearer, 'MCP-Protocol-Version': version}
        request_headers.update(headers or {})
        return self.request('/mcp', body, headers=request_headers)

    def tool(self, name, arguments=None):
        status, headers, data = self.rpc('tools/call', {'name': name, 'arguments': arguments or {}})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Type', '').split(';')[0], 'application/json')
        self.assertNotIn('error', data)
        return data['result']

    def tool_data(self, name, arguments=None):
        result = self.tool(name, arguments)
        self.assertFalse(result.get('isError', False), 'Read-only tool unexpectedly failed')
        if 'structuredContent' in result:
            return result['structuredContent']
        return json.loads(result['content'][0]['text'])

    def db_snapshot(self):
        with self.app.store.connection() as db:
            return '\n'.join(db.iterdump())

    def test_default_disabled_and_ui_routes_require_application_token(self):
        status, data = self.ui('/api/mcp/status')
        self.assertEqual(status, 200)
        self.assertFalse(data['enabled'])
        self.assertTrue(data['read_only'])
        self.assertNotIn('token', data)
        self.assertNotIn('config', data)
        for path, body in [('/api/mcp/status', None),
                           ('/api/mcp/configure', {'enabled': True, 'project_id': self.project['id']}),
                           ('/api/mcp/connection', {})]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path, body)[0], 403)
        self.assertNotEqual(self.ui('/api/mcp/connection', {})[0], 200)
        status, _headers, _data = self.rpc('tools/list')
        self.assertNotEqual(status, 200)

    def test_separate_read_only_credential_and_no_app_mutation_access(self):
        self.enable()
        self.assertEqual(self.rpc('tools/list', headers={'Authorization': 'Bearer ' + self.app.token})[0], 401)
        status, _headers, _data = self.request('/api/items', {'project_id': self.project['id'], 'name': 'Denied'},
                                              headers={'Authorization': self.bearer})
        self.assertEqual(status, 403)
        self.assertNotEqual(self.rpc('tools/list', headers={'Authorization': 'Bearer invalid'})[0], 200)

    def test_disable_rejects_existing_client_and_project_switch_rotates_key(self):
        original = self.enable()
        self.assertEqual(self.ui('/api/mcp/configure', {'enabled': False})[0], 200)
        self.assertNotEqual(self.rpc('tools/list')[0], 200)
        self.assertEqual(self.enable(), original)
        self.enable(self.other)
        self.assertFalse(self.bearer == original, 'Changing scope must rotate the credential')
        self.assertNotEqual(self.rpc('tools/list', headers={'Authorization': original})[0], 200)

    def test_invalid_configuration_preserves_scope_and_key(self):
        original = self.enable()
        for payload in ({'enabled': 'yes', 'project_id': self.project['id']},
                        {'enabled': True, 'project_id': 'missing'},
                        {'enabled': True, 'project_id': self.project['id'], 'path': str(self.root)},
                        {'enabled': True}, {'enabled': False, 'unknown': True}):
            self.assertNotEqual(self.ui('/api/mcp/configure', payload)[0], 200)
        status = self.ui('/api/mcp/status')[1]
        self.assertTrue(status['enabled'])
        self.assertEqual(status['project_id'], self.project['id'])
        self.assertEqual(self.enable(), original)

    def test_loopback_origin_content_type_and_request_limits(self):
        self.enable()
        self.assertEqual(self.rpc('tools/list', headers={'Origin': 'https://untrusted.example'})[0], 403)
        self.assertNotEqual(self.rpc('tools/list', headers={'Content-Type': 'text/plain'})[0], 200)
        status, _headers, _data = self.request('/mcp', raw=b'x' * (128 * 1024 + 1),
                                              headers={'Authorization': self.bearer})
        self.assertEqual(status, 413)
        for raw in (b'{broken', b'[]', b'null'):
            self.assertNotEqual(self.request('/mcp', raw=raw, headers={'Authorization': self.bearer})[0], 200)
        for method in ('GET', 'DELETE', 'HEAD'):
            self.assertEqual(self.request('/mcp', method=method, headers={'Authorization': self.bearer})[0], 405)

    def test_legacy_initialize_notification_ping_and_read_only_tool_catalog(self):
        self.enable()
        status, _headers, data = self.rpc('initialize', {'protocolVersion': '2025-11-25',
                                                         'capabilities': {},
                                                         'clientInfo': {'name': 'synthetic', 'version': '1'}})
        self.assertEqual(status, 200)
        self.assertEqual(data['result']['protocolVersion'], '2025-11-25')
        status, _headers, data = self.rpc('notifications/initialized', notification=True)
        self.assertEqual(status, 202)
        self.assertIsNone(data)
        self.assertEqual(self.rpc('ping')[2]['result'], {})
        tools = self.rpc('tools/list')[2]['result']['tools']
        self.assertEqual({tool['name'] for tool in tools},
                         {'get_project_summary', 'list_resources', 'read_resource', 'list_bound_skills', 'read_bound_skill','read_collaboration_task','read_run_skill'})
        for tool in tools:
            self.assertTrue(tool['annotations']['readOnlyHint'])
            self.assertFalse(tool['inputSchema'].get('additionalProperties', True))
        self.assertEqual(self.rpc('unknown/method')[2]['error']['code'], -32601)

    def test_unknown_tool_and_free_paths_cannot_extend_scope(self):
        self.enable()
        for name, args in [('run_shell', {'command': 'never execute'}),
                           ('read_resource', {'path': str(self.item['path'])}),
                           ('list_resources', {'project_id': self.other['id']})]:
            status, _headers, data = self.rpc('tools/call', {'name': name, 'arguments': args})
            self.assertEqual(status, 400)
            self.assertEqual(data['error']['code'], -32602)
        for name, args in [('read_resource', {'item_id': self.other_item['id']}),
                           ('read_bound_skill', {'skill_id': self.skill['id']})]:
            result = self.tool(name, args)
            self.assertTrue(result.get('isError', False), 'Unauthorized tool request must be rejected')
            self.assertNotIn('Other project private text', json.dumps(result))

    def test_latest_saved_file_reads_without_database_or_handoff_mutation(self):
        self.enable()
        baseline = self.app.handoffs.create(self.project['id'], 'synthetic', 'conversation', 'Synthetic task', [self.item['id']])
        self.app.handoffs.acknowledge(baseline['id'])
        before = self.db_snapshot()
        first = self.tool_data('read_resource', {'item_id': self.item['id']})
        self.assertIn('Saved version one', first['text'])
        Path(self.item['path']).write_text('Saved version two\n', encoding='utf-8')
        second = self.tool_data('read_resource', {'item_id': self.item['id']})
        self.assertIn('Saved version two', second['text'])
        self.assertNotIn('Saved version one', second['text'])
        self.tool_data('get_project_summary')
        self.tool_data('list_resources')
        self.tool_data('list_bound_skills')
        self.rpc('resources/list')
        self.assertEqual(self.db_snapshot(), before)
        self.assertEqual(Path(self.item['path']).read_text(encoding='utf-8'), 'Saved version two\n')

    def test_removed_and_private_registered_items_are_not_exposed(self):
        private = self.make_item('Synthetic credential', 'sensitive synthetic value')
        path = Path(private['path'])
        secret_path = path.parent / 'secrets' / path.name
        secret_path.parent.mkdir()
        path.rename(secret_path)
        removed = self.make_item('Removed synthetic item', 'Removed text')
        with self.app.store.connection() as db:
            db.execute('UPDATE items SET path=? WHERE id=?', (str(secret_path), private['id']))
            db.execute('UPDATE items SET removed=1 WHERE id=?', (removed['id'],))
        self.enable()
        listed = json.dumps(self.tool_data('list_resources'), ensure_ascii=False)
        self.assertNotIn(private['id'], listed)
        self.assertNotIn(removed['id'], listed)
        for item in (private, removed):
            self.assertTrue(self.tool('read_resource', {'item_id': item['id']}).get('isError', False))

    def test_replaced_symlink_is_rejected_at_read_time(self):
        self.enable()
        source = Path(self.item['path'])
        target = self.root / 'outside.md'
        target.write_text('Outside synthetic data', encoding='utf-8')
        original = source.read_bytes()
        source.unlink()
        try:
            source.symlink_to(target)
        except (OSError, NotImplementedError):
            source.write_bytes(original)
            self.skipTest('This Windows account cannot create symbolic links')
        try:
            self.assertTrue(self.tool('read_resource', {'item_id': self.item['id']}).get('isError', False))
        finally:
            source.unlink()
            source.write_bytes(original)

    def test_text_payload_and_file_size_are_bounded(self):
        self.enable()
        Path(self.item['path']).write_text('字' * 30000 + 'END_SHOULD_BE_TRUNCATED', encoding='utf-8')
        data = self.tool_data('read_resource', {'item_id': self.item['id']})
        self.assertLessEqual(len(data['text'].encode('utf-8')), 64 * 1024)
        self.assertNotIn('END_SHOULD_BE_TRUNCATED', data['text'])
        self.assertGreater(data['next_offset'], 0)
        Path(self.item['path']).write_bytes(b'x' * (1024 * 1024 + 1))
        self.assertTrue(self.tool('read_resource', {'item_id': self.item['id']}).get('isError', False))

    def test_bound_collection_reads_pinned_version_without_source_writes(self):
        preview = self.app.skills.collections.preview(self.skill['id'])
        first = self.app.skills.collections.collect(preview['token'])
        self.app.skills.bind(self.project['id'], self.skill['id'], True)
        self.skill_file.write_text('---\nname: synthetic\n---\n\nSource instructions v2\n', encoding='utf-8')
        self.app.skills.refresh()
        preview = self.app.skills.collections.preview(self.skill['id'])
        self.app.skills.collections.collect(preview['token'])
        self.enable()
        before = self.db_snapshot()
        data = self.tool_data('read_bound_skill', {'skill_id': self.skill['id']})
        self.assertIn('Pinned instructions v1', data['text'])
        self.assertNotIn('Source instructions v2', data['text'])
        self.assertEqual(data['version'], first['version'])
        self.assertEqual(self.db_snapshot(), before)
        self.assertIn('Source instructions v2', self.skill_file.read_text(encoding='utf-8'))

    def modern(self, method, params=None, *, version='2026-07-28', headers=None):
        params = dict(params or {})
        params['_meta'] = {'io.modelcontextprotocol/protocolVersion': version,
                           'io.modelcontextprotocol/clientCapabilities': {}}
        request_headers = {'Mcp-Method': method}
        if method == 'tools/call':
            request_headers['Mcp-Name'] = params.get('name', '')
        if method == 'resources/read':
            request_headers['Mcp-Name'] = params.get('uri', '')
        request_headers.update(headers or {})
        return self.rpc(method, params, version=version, headers=request_headers)

    def test_modern_discovery_and_header_metadata_binding(self):
        self.enable()
        status, _headers, data = self.modern('server/discover')
        self.assertEqual(status, 200)
        self.assertEqual(data['result']['resultType'], 'complete')
        status, _headers, data = self.modern('tools/list')
        self.assertEqual(status, 200)
        self.assertEqual(data['result']['cacheScope'], 'private')
        self.assertEqual(data['result']['ttlMs'], 0)
        status, _headers, data = self.modern('tools/call', {'name': 'read_resource',
                                                         'arguments': {'item_id': self.item['id']}})
        self.assertEqual(status, 200)
        self.assertEqual(data['result']['resultType'], 'complete')
        self.assertEqual(self.modern('tools/list', headers={'Mcp-Method': 'resources/list'})[2]['error']['code'], -32020)
        self.assertEqual(self.modern('tools/call', {'name': 'get_project_summary'},
                                     headers={'Mcp-Name': 'read_resource'})[2]['error']['code'], -32020)
        self.assertEqual(self.modern('tools/list', headers={'MCP-Protocol-Version': '2025-11-25'})[2]['error']['code'], -32020)
        self.assertEqual(self.rpc('tools/list', version='2026-07-28')[2]['error']['code'], -32602)
        self.assertEqual(self.modern('unknown/method')[0], 404)

    def test_unsupported_version_reports_supported_and_requested(self):
        self.enable()
        requested = '2099-01-01'
        status, _headers, data = self.modern('server/discover', version=requested)
        self.assertEqual(status, 400)
        self.assertEqual(data['error']['code'], -32022)
        self.assertEqual(data['error']['data']['requested'], requested)
        self.assertIn('2026-07-28', data['error']['data']['supported'])
        self.assertIn('2025-11-25', data['error']['data']['supported'])

    def test_resource_uris_are_scoped_and_read_latest_saved_text(self):
        self.enable()
        data = self.rpc('resources/list')[2]['result']
        uri = f"yingxu://project/{self.project['id']}/item/{self.item['id']}"
        self.assertIn(uri, {entry['uri'] for entry in data['resources']})
        status, _headers, data = self.rpc('resources/read', {'uri': uri})
        self.assertEqual(status, 200)
        self.assertIn('Saved version one', json.loads(data['result']['contents'][0]['text'])['text'])
        for denied in (f"yingxu://project/{self.other['id']}/item/{self.other_item['id']}",
                       'file://' + str(self.item['path']), uri + '/../outside'):
            self.assertEqual(self.rpc('resources/read', {'uri': denied})[2]['error']['code'], -32602)

    def test_invalid_pagination_and_request_ids_are_rejected(self):
        self.enable()
        for args in ({'limit': 49}, {'limit': True}, {'offset': -1}, {'q': 'x' * 201},
                     {'category': 'unknown'}, {'offset': 1000001}):
            self.assertEqual(self.rpc('tools/call', {'name': 'list_resources', 'arguments': args})[2]['error']['code'], -32602)
        for request_id in (True, None, {}, 'x' * 129):
            body = {'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/list'}
            self.assertEqual(self.request('/mcp', body, headers={'Authorization': self.bearer})[2]['error']['code'], -32600)

    def test_secret_values_in_json_and_authorization_are_redacted(self):
        secret_values = ['synthetic-json-api-key', 'synthetic-password', 'synthetic-bearer-credential']
        text = '{"api_key": "' + secret_values[0] + '", "password": "' + secret_values[1] + '"}\n'
        text += 'Authorization: Bearer ' + secret_values[2] + '\n'
        Path(self.item['path']).write_text(text, encoding='utf-8')
        self.enable()
        data = self.tool_data('read_resource', {'item_id': self.item['id']})
        for value in secret_values:
            self.assertFalse(value in data['text'], 'Synthetic secret was not redacted')
        self.assertIn('已隐藏', data['text'])
        self.assertEqual(Path(self.item['path']).read_text(encoding='utf-8'), text)

    def test_restart_defaults_off_and_reuses_only_same_project_credential(self):
        from yingxu.mcp import ProjectMCP
        self.assertFalse((self.root / 'data/mcp-access.json').exists())
        original = self.enable()
        restarted = ProjectMCP(self.app.store, self.app.skills)
        self.assertFalse(restarted.status(self.endpoint)['enabled'])
        with self.assertRaises(UserError):
            restarted.authorize(original)
        restarted.configure({'enabled': True, 'project_id': self.project['id']}, self.endpoint)
        restored = restarted.connection(self.endpoint)['config']['mcpServers']['yingxu']['headers']['Authorization']
        self.assertTrue(restored == original, 'Same project should retain its local client configuration')
        restarted.configure({'enabled': True, 'project_id': self.other['id']}, self.endpoint)
        changed = restarted.connection(self.endpoint)['config']['mcpServers']['yingxu']['headers']['Authorization']
        self.assertFalse(changed == original, 'Different project must rotate scope credential')
        restarted.close()

    def test_full_metadata_pages_shrink_to_budget_without_losing_items(self):
        expected = {self.item['id']}
        for index in range(47):
            item = self.make_item('资源' + str(index), 'Synthetic content')
            expected.add(item['id'])
        tags = ['标签' + str(index) + '字' * 34 for index in range(32)]
        with self.app.store.connection() as db:
            db.execute('UPDATE items SET tags=?,notes=? WHERE project_id=?',
                       (json.dumps(tags, ensure_ascii=False), '备注' * 200, self.project['id']))
        self.enable()
        found, offset, first_count = [], 0, None
        for _page in range(20):
            status, _headers, response = self.rpc('tools/call', {'name': 'list_resources', 'arguments': {'offset': offset}})
            self.assertEqual(status, 200)
            self.assertLessEqual(len(json.dumps(response, ensure_ascii=False).encode('utf-8')), 256 * 1024)
            data = response['result']['structuredContent']
            if first_count is None:
                first_count = len(data['items'])
            found.extend(item['id'] for item in data['items'])
            if 'next_offset' not in data:
                break
            self.assertGreater(data['next_offset'], offset)
            offset = data['next_offset']
        else:
            self.fail('Resource pagination did not finish')
        self.assertLess(first_count, 48)
        self.assertEqual(len(found), len(expected))
        self.assertEqual(set(found), expected)

    def test_slow_bodies_reserve_two_slots_then_third_is_rejected(self):
        self.enable()
        slow = []
        try:
            for _index in range(2):
                connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
                connection.putrequest('POST', '/mcp')
                connection.putheader('Authorization', self.bearer)
                connection.putheader('Content-Type', 'application/json')
                connection.putheader('Content-Length', '100')
                connection.endheaders(b'{')
                slow.append(connection)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with self.app.mcp._slots._cond:
                    if self.app.mcp._slots._value == 0:
                        break
                time.sleep(0.01)
            else:
                self.fail('Slow-body requests did not reserve MCP slots')
            self.assertEqual(self.rpc('tools/list')[0], 429)
        finally:
            for connection in slow:
                connection.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.app.mcp._slots._cond:
                if self.app.mcp._slots._value == 2:
                    break
            time.sleep(0.01)
        else:
            self.fail('Disconnected clients did not release MCP request slots')
        self.assertEqual(self.rpc('tools/list')[0], 200)


if __name__ == '__main__':
    unittest.main()
