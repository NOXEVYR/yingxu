"""Promote the exact Windows assets already downloaded and tested by the pinned job."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent
PLAN = json.loads((ROOT / 'verified-assets.json').read_text(encoding='utf-8'))
REPO = 'turnsolesama/yingxu'
MESSAGE = 'fix: promote verified 0.4.6 Windows icon assets'


def api(path, method='GET', fields=None, binary=False):
    args = ['gh', 'api', 'repos/' + REPO + '/' + path, '--method', method]
    if binary and path.startswith('releases/assets/'):
        args += ['-H', 'Accept: application/octet-stream']
    if binary and path.startswith('actions/jobs/') and path.endswith('/logs'):
        # Capture only into a pipe for evidence parsing; never render log ANSI.
        args += ['--allow-escape-sequences']
    kwargs = {}
    if fields is not None:
        args += ['--input', '-']
        kwargs['input'] = json.dumps(fields, ensure_ascii=False).encode('utf-8')
    raw = subprocess.check_output(args, **kwargs)
    if binary and path.endswith('/logs'):
        raw = re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]', b'', raw)
    return raw if binary else (json.loads(raw.decode('utf-8')) if raw.strip() else None)


def normalized(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def release():
    return api('releases/tags/' + PLAN['tag'])


def check_asset(current, expected, name):
    found = current.get(expected['id'], {})
    assert found.get('name') == name, 'Asset name changed: ' + name
    for key in ('digest', 'size'):
        assert found.get(key) == expected[key], 'Asset content changed: ' + name


def check(phase):
    current = {a['id']: a for a in release()['assets']}
    for entry in PLAN['assets']:
        check_asset(current, entry['old'], entry['old']['name'] if phase == 'before' else backup(entry))
        check_asset(current, entry['staged'], entry['staged']['name'] if phase == 'before' else entry['name'])
    assert api('git/ref/tags/' + PLAN['tag'])['object']['sha'] == PLAN['tag_sha'], 'Tag changed'


def backup(entry):
    return 'icon-backup-recovery-' + str(entry['old']['id']) + '-' + entry['name']


def rename(asset, name):
    api('releases/assets/' + str(asset['id']), 'PATCH', {'name': name})


def set_notes(release_id, body):
    # JSON transmits explicit LF; no Windows text-file newline conversion.
    api('releases/' + str(release_id), 'PATCH', {'body': normalized(body)})
    assert normalized(release().get('body') or '') == normalized(body), 'Release notes changed'


def main():
    assert os.environ.get('GITHUB_REPOSITORY') == REPO
    assert os.environ.get('GITHUB_REF') == 'refs/heads/main'
    assert os.environ.get('GITHUB_EVENT_NAME') == 'push'
    assert api('commits/' + os.environ['GITHUB_SHA'])['commit']['message'] == MESSAGE
    assert PLAN['repo'] == REPO and PLAN['tag'] == 'yingxu-v0.4.6'
    run = api('actions/runs/' + str(PLAN['run_id']))
    job = api('actions/jobs/' + str(PLAN['job_id']))
    assert run['head_sha'] == PLAN['source_commit']
    assert job['run_id'] == PLAN['run_id'] and job['status'] == 'completed'
    assert any(s['name'] == 'Build and verify complete offline ZIP' and s['conclusion'] == 'success' for s in job['steps'])
    current_release = release()
    assert not current_release['draft']
    assert current_release.get('body') == PLAN['observed_body'], 'Notes changed since review'
    check('before')
    documents = {}
    for entry in PLAN['assets']:
        if entry['name'].endswith('.zip'):
            continue  # The full ZIP already passed the pinned Windows download verification.
        assert entry['staged']['size'] < 16384
        raw = api('releases/assets/' + str(entry['staged']['id']), binary=True)
        assert len(raw) == entry['staged']['size']
        assert 'sha256:' + hashlib.sha256(raw).hexdigest() == entry['staged']['digest']
        documents[entry['name']] = raw.decode('utf-8-sig')
    manifest = json.loads(documents['YingXu-v0.4.6-manifest.json'])
    verified = json.loads(documents['YingXu-v0.4.6-verification.json'])
    archive = next(e for e in PLAN['assets'] if e['name'].endswith('.zip'))
    assert manifest['source_commit'] == verified['source_commit'] == PLAN['source_commit']
    assert manifest['icon_sha256'] == verified['icon_sha256'] == PLAN['icon_sha256']
    assert manifest['icon_revision'] == verified['icon_revision'] == 'viewfinder-v1'
    assert verified['ok'] is True and len(verified['checks']) == 31
    assert manifest['bytes'] == archive['staged']['size']
    assert 'sha256:' + manifest['sha256'] == archive['staged']['digest']
    assert documents['YingXu-v0.4.6-SHA256.txt'].split()[0] == manifest['sha256']
    # Read the pinned job's small text log to verify that the downloaded ZIP,
    # not only the original build, completed all 31 checks before promotion.
    log = api('actions/jobs/' + str(PLAN['job_id']) + '/logs', binary=True).decode('utf-8-sig')
    staged_log = log.split('Icon repair staging record:', 1)[1]
    assert '"ok": true' in staged_log and '"source_commit": "' + PLAN['source_commit'] + '"' in staged_log
    assert '"icon_sha256": "' + PLAN['icon_sha256'] + '"' in staged_log
    assert 'Updated release notes differ' in staged_log
    check('before')
    try:
        for entry in PLAN['assets']:
            rename(entry['old'], backup(entry))
        for entry in PLAN['assets']:
            rename(entry['staged'], entry['name'])
        check('after')
        set_notes(current_release['id'], PLAN['new_body'])
        check('after')
    except BaseException:
        for entry in PLAN['assets']:
            rename(entry['staged'], entry['staged']['name'])
        for entry in PLAN['assets']:
            rename(entry['old'], entry['old']['name'])
        set_notes(current_release['id'], PLAN['original_body'])
        check('before')
        raise
    # Promotion is committed. Cleanup failure leaves backups; never roll back
    # after deleting even one backup asset.
    for entry in PLAN['assets']:
        try:
            current = {a['id']: a for a in release()['assets']}
            check_asset(current, entry['old'], backup(entry))
            api('releases/assets/' + str(entry['old']['id']), 'DELETE')
        except Exception as error:
            print('::warning::Retained old asset ' + str(entry['old']['id']) + ': ' + str(error))
    current = {a['id']: a for a in release()['assets']}
    for entry in PLAN['assets']:
        check_asset(current, entry['staged'], entry['name'])
    print(json.dumps({'ok': True, 'source_commit': PLAN['source_commit'],
                      'zip_sha256': manifest['sha256'], 'zip_bytes': manifest['bytes'],
                      'zip_downloaded_again': False, 'checks_reused': 31}))


if __name__ == '__main__':
    main()
