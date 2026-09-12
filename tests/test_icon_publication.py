"""Publication guards and rollback use synthetic files and mocked GitHub only."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('icon_publication_under_test', ROOT/'macos/publish.py')
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class IconPublicationTests(unittest.TestCase):
    def test_api_uses_utf8_for_chinese_and_empty_delete_response(self):
        actual_check_output = publish.subprocess.check_output
        payload = json.dumps({'name': '映序落'}, ensure_ascii=False).encode('utf-8')
        def utf8_child(command, **kwargs):
            self.assertEqual(kwargs.get('encoding'), 'utf-8')
            return actual_check_output([publish.sys.executable, '-c',
                'import sys;sys.stdout.buffer.write('+repr(payload)+')'], **kwargs)
        with patch.object(publish.subprocess, 'check_output', side_effect=utf8_child):
            self.assertEqual(publish.github_api('synthetic'), {'name': '映序落'})
        with patch.object(publish.subprocess, 'check_output', return_value=''):
            self.assertIsNone(publish.github_api('synthetic', '--method', 'DELETE'))

    def test_gh_uses_utf8(self):
        with patch.object(publish.subprocess, 'run') as run:
            publish.gh('release', 'view', 'synthetic')
            self.assertEqual(run.call_args.kwargs.get('encoding'), 'utf-8')

    def test_windows_retry_marker_is_platform_scoped(self):
        environment = {'GITHUB_REPOSITORY': publish.REPO, 'REPLACE_ICONS': 'true',
                       'GITHUB_EVENT_NAME': 'push', 'ICON_REPAIR_PUSH': 'true',
                       'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': 'synthetic'}
        with patch.dict(os.environ, environment), patch.object(publish, 'github_api',
                return_value={'commit': {'message': publish.ICON_REPAIR_WINDOWS_COMMIT}}):
            with patch.object(publish.sys, 'platform', 'win32'):
                self.assertTrue(publish.icon_replacement_requested())
            with patch.object(publish.sys, 'platform', 'darwin'):
                self.assertFalse(publish.icon_replacement_requested())

    def exercise(self, scenario):
        names = ['fixture.zip', 'fixture-manifest.json', 'fixture-SHA256.txt', 'fixture-verification.json']
        with tempfile.TemporaryDirectory(prefix='yingxu-publish-test-') as temporary:
            root = Path(temporary).resolve()
            artifacts = root/'artifacts'; artifacts.mkdir()
            icon = root/'icon'; icon.write_bytes(b'icon')
            manifest = {'source_commit': 'newcommit', 'icon_revision': 'viewfinder-v1',
                        'icon_sha256': sha(b'icon'), 'bytes': 3, 'sha256': sha(b'ZIP')}
            if scenario == 'bad-manifest': manifest['icon_revision'] = 'wrong'
            content = {names[0]: b'ZIP', names[1]: json.dumps(manifest).encode(),
                       names[2]: b'checksum', names[3]: b'{"ok":true}'}
            for name, data in content.items(): (artifacts/name).write_bytes(data)
            original = {name: ('old '+name).encode() for name in names}
            remote = {i+1: {'name': name, 'data': data} for i, (name, data) in enumerate(original.items())}
            body = ['old notes']; events = []; tag_calls = [0]; interrupted = [False]
            mutations = [0]; verifications = [0]

            def metadata(asset_id):
                asset = remote[asset_id]
                return {'id': asset_id, 'name': asset['name'], 'size': len(asset['data']), 'digest': 'sha256:'+sha(asset['data'])}

            def api(endpoint, *args):
                events.append(('api', endpoint, args))
                if endpoint.startswith('commits/'):
                    message = publish.ICON_REPAIR_COMMIT
                    if scenario == 'forged-marker': message = 'ordinary change'
                    if scenario == 'marker-extra-line': message += '\nextra text'
                    if scenario == 'marker-case': message = message.upper()
                    return {'commit': {'message': message}}
                if endpoint.startswith('git/'):
                    tag_calls[0] += 1
                    return {'object': {'sha': 'changed' if scenario == 'tag-changed' and tag_calls[0] > 1 else 'oldcommit', 'type': 'commit'}}
                if endpoint.startswith('releases/assets/'):
                    asset_id = int(endpoint.rsplit('/', 1)[1])
                    method = args[args.index('--method')+1]
                    if method == 'PATCH':
                        name = args[args.index('-f')+1].removeprefix('name=')
                        self.assertFalse(any(a['name'] == name and i != asset_id for i, a in remote.items()))
                        remote[asset_id]['name'] = name; mutations[0] += 1
                        if scenario == 'promotion-'+str(mutations[0]) and not interrupted[0]:
                            interrupted[0] = True
                            raise RuntimeError('synthetic lost rename response')
                        return metadata(asset_id)
                    self.assertEqual(method, 'DELETE')
                    if scenario == 'cleanup-failure' and asset_id == 2: raise RuntimeError('synthetic delete failure')
                    del remote[asset_id]; return None
                result = [metadata(asset_id) for asset_id in remote]
                if scenario == 'stage-digest':
                    for asset in result:
                        if asset['name'].startswith('icon-stage-'): asset['digest'] = 'sha256:bad'
                return {'draft': False, 'body': body[0], 'assets': result}

            def gh(*args, **kwargs):
                action = args[1]; events.append((action, args))
                if action == 'download':
                    folder = Path(args[args.index('--dir')+1]); folder.mkdir(parents=True, exist_ok=True)
                    requested = [args[i+1] for i, arg in enumerate(args) if arg == '--pattern']
                    self.assertEqual(len(requested), 4)
                    self.assertTrue(all(name.startswith('icon-stage-newcommit-') for name in requested))
                    for asset in remote.values():
                        name, data = asset['name'], asset['data']
                        if name not in requested: continue
                        self.assertNotIn(data, original.values(), 'Old assets must never be downloaded')
                        if scenario == 'download-mismatch': data += b'bad'
                        (folder/name).write_bytes(data)
                elif action == 'upload':
                    self.assertNotIn('--clobber', args)
                    for index, filename in enumerate(args[3:]):
                        file = Path(filename)
                        self.assertTrue(file.name.startswith('icon-stage-newcommit-'))
                        remote[max(remote)+1] = {'name': file.name, 'data': file.read_bytes()}
                        if scenario == 'upload-failure' and not interrupted[0] and index == 1:
                            interrupted[0] = True
                            raise RuntimeError('synthetic interrupted upload')
                elif action == 'edit':
                    body[0] = Path(args[args.index('--notes-file')+1]).read_text(encoding='utf-8')
                    if scenario == 'notes-failure' and not interrupted[0]:
                        interrupted[0] = True
                        raise RuntimeError('synthetic lost notes response')
                else: raise AssertionError(args)
                return SimpleNamespace(returncode=0, stdout='')

            def verify(*args, **kwargs):
                events.append(('verify', args)); verifications[0] += 1
                if scenario == 'verification-failure' and verifications[0] == 2:
                    raise RuntimeError('synthetic extracted archive failure')

            push_cases = {'auto', 'marked-push', 'unmarked-push', 'forged-marker', 'wrong-branch',
                          'wrong-repo', 'marker-extra-line', 'marker-case'}
            environment = {'GITHUB_REPOSITORY': 'other/repo' if scenario == 'wrong-repo' else publish.REPO,
                           'GITHUB_SHA': 'newcommit', 'GITHUB_EVENT_NAME': 'push' if scenario in push_cases else 'workflow_dispatch',
                           'GITHUB_REF': 'refs/heads/other' if scenario == 'wrong-branch' else 'refs/heads/main',
                           'ICON_REPAIR_PUSH': 'false' if scenario in {'auto', 'unmarked-push'} else 'true',
                           'REPLACE_ICONS': 'false' if scenario == 'disabled' else 'true', 'RUNNER_TEMP': str(root)}
            error = None
            with patch.dict(os.environ, environment), patch.object(publish, 'github_api', api), patch.object(publish, 'gh', gh), \
                    patch.object(publish.subprocess, 'run', side_effect=verify):
                try:
                    publish.replace_published_icons('unapproved' if scenario == 'wrong-tag' else 'yingxu-v0.4.6',
                                                    names, artifacts, 'new notes', root/'verify.py', icon)
                except RuntimeError as failure: error = failure
            canonical = {a['name']: a['data'] for a in remote.values() if a['name'] in names}
            if scenario in {'success', 'marked-push', 'cleanup-failure'}:
                self.assertIsNone(error)
                self.assertEqual(canonical, content)
                self.assertIn('newcommit', body[0]); self.assertIn('oldcommit', body[0])
                self.assertTrue(all(i > 4 for i, a in remote.items() if a['name'] in names))
                if scenario == 'cleanup-failure':
                    self.assertEqual(remote[2]['data'], original[names[1]])
                    self.assertTrue(remote[2]['name'].startswith('icon-backup-'))
                else: self.assertEqual(len(remote), 4)
            else:
                self.assertIsNotNone(error)
                self.assertEqual(canonical, original); self.assertEqual(body[0], 'old notes')
                self.assertEqual(set(remote), {1, 2, 3, 4}, 'Original asset IDs must be retained after rollback')
            if any(event[0] == 'upload' for event in events):
                self.assertLess(next(i for i, event in enumerate(events) if event[0] == 'verify'),
                                next(i for i, event in enumerate(events) if event[0] == 'upload'))
            if mutations[0]:
                self.assertEqual(verifications[0], 2, 'Both local and downloaded staged ZIP must pass before promotion')
                first_patch = next(i for i, event in enumerate(events) if event[0] == 'api' and 'PATCH' in event[2])
                self.assertLess([i for i, event in enumerate(events) if event[0] == 'verify'][-1], first_patch)
            if scenario == 'marked-push':
                self.assertIn(('api', 'commits/newcommit', ()), events)


for case in ('auto', 'disabled', 'wrong-tag', 'bad-manifest', 'success', 'upload-failure',
             'download-mismatch', 'stage-digest', 'tag-changed', 'marked-push', 'unmarked-push',
             'forged-marker', 'wrong-branch', 'wrong-repo', 'marker-extra-line', 'marker-case',
             'verification-failure', 'notes-failure', 'cleanup-failure',
             *(f'promotion-{i}' for i in range(1, 9))):
    setattr(IconPublicationTests, 'test_'+case.replace('-', '_'), lambda self, scenario=case: self.exercise(scenario))


if __name__ == '__main__': unittest.main()
