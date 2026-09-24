"""Offline synthetic tests only: no network, subprocess or release mutation."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('cached_release', Path(__file__).with_name('cached_release.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sha, self.parent = 'a' * 40, 'b' * 40
        self.env = dict(GITHUB_ACTIONS='true', GITHUB_REPOSITORY=m.REPO,
                        GITHUB_REF='refs/heads/main', GITHUB_SHA=self.sha, GITHUB_EVENT_NAME='workflow_dispatch',
                        GITHUB_RUN_ATTEMPT='1')
        self.event = {'inputs': {'approve_download_and_publish': 'true', 'source_commit': self.sha}}
        self.changed = [m.REQUEST_PATH]
        self.request = dict(approved=True, reviewed_source_commit=self.parent, version=m.VERSION, download_budget_mib=500)
        p = self.root / m.REQUEST_PATH
        p.parent.mkdir(parents=True)
        self.write_request()

    def write_request(self):
        (self.root / m.REQUEST_PATH).write_text(json.dumps(self.request), encoding='utf-8')

    def git(self, args):
        if args[1] == 'rev-parse': return self.sha
        if args[1] == 'rev-list': return self.sha + ' ' + self.parent
        if args[1] == 'diff-tree': return '\n'.join(self.changed)
        raise AssertionError(args)

    def run_guard(self):
        return m.authorized(self.root, self.env, self.event, self.git)

    def push(self):
        self.env['GITHUB_EVENT_NAME'] = 'push'
        self.event = {'head_commit': {'message': m.PUSH_TITLE}}

    def test_dispatch_requires_exact_opt_in(self):
        self.assertEqual(self.run_guard(), self.sha)
        for value in ('false', '', False):
            self.event['inputs']['approve_download_and_publish'] = value
            with self.assertRaises(ValueError): self.run_guard()

    def test_dispatch_wrong_sha(self):
        self.event['inputs']['source_commit'] = self.parent
        with self.assertRaises(ValueError): self.run_guard()

    def test_rerun_cannot_reuse_single_download_approval(self):
        for attempt in ('2', ''):
            self.env['GITHUB_RUN_ATTEMPT'] = attempt
            with self.assertRaises(ValueError): self.run_guard()

    def test_exact_fixed_inputs_and_budget(self):
        self.assertEqual(m.BASE_BYTES + m.SDK_BYTES, 457478112)
        self.assertEqual(m.DOWNLOAD_BUDGET, 524288000)
        self.assertLess(m.BASE_BYTES + m.SDK_BYTES, m.DOWNLOAD_BUDGET)
        self.assertEqual(m.BASE_TAG, 'yingxu-v0.4.19')
        self.assertEqual(m.BASE_SHA, 'ba78f3e858a57491cef2937bbcfa1f09d76c39bd5e42a9cddb2181f1d9ed7e4f')
        with patch.object(m, 'BASE_BYTES', m.DOWNLOAD_BUDGET):
            with self.assertRaises(ValueError): self.run_guard()

    def test_wrong_repo_ref_actions(self):
        for field, value in [('GITHUB_REPOSITORY','other/yingxu'),('GITHUB_REF','refs/heads/dev'),('GITHUB_ACTIONS','false')]:
            saved = self.env[field]; self.env[field] = value
            with self.assertRaises(ValueError): self.run_guard()
            self.env[field] = saved

    def test_marked_approval_only_push(self):
        self.push()
        self.assertEqual(self.run_guard(), self.sha)
        self.changed.append('server.py')
        with self.assertRaises(ValueError): self.run_guard()

    def test_unmarked_push(self):
        self.push(); self.event['head_commit']['message'] = 'fix: ordinary source update'
        with self.assertRaises(ValueError): self.run_guard()

    def test_false_or_wrong_parent_or_budget(self):
        self.push()
        for key, value in [('approved',False), ('reviewed_source_commit',self.sha), ('download_budget_mib',501), ('version','0.4.19')]:
            saved = self.request[key]; self.request[key] = value; self.write_request()
            with self.assertRaises(ValueError): self.run_guard()
            self.request[key] = saved

    def test_default_request_disabled(self):
        request = json.loads(Path(__file__).with_name('yingxu-0.4.20.json').read_text(encoding='utf-8'))
        self.assertIs(request['approved'], False)


class IntegrityTests(unittest.TestCase):
    def test_exact_asset_size_digest_and_id(self):
        expected={'a.zip':(123,'c'*64)}
        release={'assets':[dict(name='a.zip',id=17,size=123,digest='sha256:'+'c'*64,state='uploaded')]}
        self.assertEqual(m.check_assets(release,expected),{'a.zip':17})
        for key, value in [('id',0),('size',124),('digest','sha256:'+'d'*64),('state','starter'),('name','b.zip')]:
            wrong=copy.deepcopy(release);wrong['assets'][0][key]=value
            with self.assertRaises(ValueError):m.check_assets(wrong,expected)
        with self.assertRaises(ValueError):m.check_assets({'assets':release['assets']*2},expected)

    def archive(self, *, wrong_hash=False, extra=None):
        raw=b'offline synthetic runtime'
        manifest={'application':'YingXu','root':'YingXu/','version':'0.4.19','architecture':'Windows x64',
                  'files':[{'path':'runtime/example.bin','bytes':len(raw),'sha256':('0'*64 if wrong_hash else hashlib.sha256(raw).hexdigest())}]}
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            z.writestr('YingXu/runtime/example.bin',raw)
            z.writestr('YingXu/RELEASE_MANIFEST.json',json.dumps(manifest))
            if extra:z.writestr(extra,b'extra')
        out.seek(0)
        return zipfile.ZipFile(out)

    def test_internal_manifest_full_hash(self):
        with self.archive() as z:self.assertEqual(list(m.validate_base(z)),['runtime/example.bin'])
        with self.archive(wrong_hash=True) as z:
            with self.assertRaisesRegex(ValueError,'SHA-256'):m.validate_base(z)

    def test_traversal_and_unlisted_member(self):
        for extra in ('YingXu/../escape','YingXu/unlisted.txt'):
            with self.archive(extra=extra) as z:
                with self.assertRaises(ValueError):m.validate_base(z)

    def test_windows_unsafe_paths(self):
        for name in ('../a','/a','a\\b','a:stream','CON','a/./b','a//b','a./b'):
            with self.assertRaises(ValueError):m.safe_name(name)

    def test_existing_draft_blocks_before_tag_query(self):
        with patch.object(m, 'gh_json', return_value=[{'id': 777, 'tag_name': m.TAG, 'draft': True}]), \
                patch.object(m.subprocess, 'run') as subprocess_run:
            with self.assertRaisesRegex(ValueError, 'draft preserved'): m.assert_new_release()
            subprocess_run.assert_not_called()


class DraftPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.commit = 'a' * 40
        self.notes = '项目迁移状态修复。\n\n合成验证，尚未完成反馈者实机验收。\n'
        self.files = []
        for name in ('fixture.zip', 'fixture-manifest.json', 'fixture-SHA256.txt', 'fixture-verification.json'):
            path = self.root/name
            path.write_bytes(('合成附件：' + name + '\n').encode('utf-8'))
            self.files.append(path)
        self.release = dict(id=789, tag_name=m.TAG, target_commitish=self.commit,
                            draft=True, prerelease=False, body=self.notes, assets=[])
        self.endpoints = []
        self.published = False
        self.failure = None

    def command(self, args, **kwargs):
        self.assertEqual(args[:2], ['gh', 'api'])
        endpoint = args[2]
        self.endpoints.append(endpoint)
        method = args[args.index('--method') + 1]
        raw = Path(args[args.index('--input') + 1]).read_bytes()
        if endpoint == 'repos/' + m.REPO + '/releases':
            body = json.loads(raw)
            self.assertNotIn(b'\r', raw)
            self.assertEqual(body['body'].encode('utf-8'), self.notes.encode('utf-8'))
            self.assertTrue(body['draft'])
            self.assertEqual(body['target_commitish'], self.commit)
            return json.dumps(self.release)
        if endpoint.startswith('https://uploads.github.com/repos/' + m.REPO + '/releases/789/assets?name='):
            name = endpoint.split('name=')[1]
            self.assertEqual(method, 'POST')
            self.assertEqual(raw, (self.root/name).read_bytes())
            asset = dict(id=100 + len(self.release['assets']), name=name, size=len(raw),
                         state='uploaded', digest='sha256:' + hashlib.sha256(raw).hexdigest())
            self.release['assets'].append(asset)
            return json.dumps(asset)
        self.assertEqual(endpoint, 'repos/' + m.REPO + '/releases/789')
        self.assertEqual(method, 'PATCH')
        self.assertEqual(json.loads(raw), {'draft': False, 'make_latest': 'true'})
        self.published = True
        self.release['draft'] = False
        return json.dumps(self.release)

    def gh_json(self, endpoint):
        self.endpoints.append(endpoint)
        # The old draft lookup would return 404. It must never be attempted.
        if endpoint.startswith('releases/tags/'):
            raise AssertionError('Draft tag lookup is unavailable: HTTP 404')
        if endpoint == 'releases/789':
            release = copy.deepcopy(self.release)
            if self.failure == 'digest': release['assets'][0]['digest'] = 'sha256:' + '0'*64
            if self.failure == 'asset_id': release['assets'][0]['id'] = 999
            if self.failure == 'release_id': release['id'] = 999
            if self.failure == 'notes': release['body'] += 'unexpected edit'
            return release
        if endpoint == 'git/ref/heads/main':
            return {'object': {'sha': 'b'*40 if self.failure == 'main' else self.commit}}
        if endpoint == 'git/ref/tags/' + m.TAG:
            self.assertTrue(self.published)
            return {'object': {'sha': self.commit}}
        raise AssertionError(endpoint)

    def run_publish(self):
        with patch.object(m, 'command', side_effect=self.command), \
                patch.object(m, 'gh_json', side_effect=self.gh_json), \
                patch.object(m.time, 'sleep'), patch('builtins.print'):
            m.publish_draft(self.root, self.commit, self.files, self.notes)

    def test_create_upload_verify_and_publish_by_id_when_draft_tag_is_404(self):
        self.run_publish()
        self.assertTrue(self.published)
        report = json.loads((self.root/'publication.json').read_bytes())
        self.assertEqual(report['release_id'], 789)
        self.assertEqual(len(report['asset_ids']), 4)
        self.assertFalse(any('releases/tags/' in endpoint for endpoint in self.endpoints))

    def test_changed_digest_or_asset_identity_never_publishes(self):
        for failure in ('digest', 'asset_id', 'release_id', 'notes', 'main'):
            with self.subTest(failure=failure):
                self.release['assets'] = []
                self.failure = failure
                with self.assertRaises(ValueError): self.run_publish()
                self.assertFalse(self.published)
                self.assertFalse((self.root/'publication.json').exists())
                identity = json.loads((self.root/'draft-identity.json').read_bytes())
                self.assertEqual(identity['release_id'], 789)

    def test_failed_upload_leaves_draft_without_publication_or_delete(self):
        original = self.command
        def failed(args, **kwargs):
            if args[2].startswith('https://uploads.github.com/'):
                raise RuntimeError('synthetic upload failure')
            return original(args, **kwargs)
        with patch.object(self, 'command', side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, 'synthetic'): self.run_publish()
        self.assertFalse(self.published)
        self.assertTrue((self.root/'draft-identity.json').exists())


if __name__=='__main__':unittest.main()
