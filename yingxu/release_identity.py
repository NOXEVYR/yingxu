"""Bounded build identities; only numeric revisions in one series are ordered."""
import hashlib
import io
import json
import re

from .range_zip import MAX_MANIFEST, RangeZip, UpdateError, validate_manifest

MAX_EXTERNAL_MANIFEST = 1024 * 1024


def parse_build_revision(value):
    if not isinstance(value, str) or len(value) > 42:
        return None
    match = re.fullmatch(r'([a-z][a-z0-9_-]{0,31})\.(0|[1-9][0-9]{0,8})', value)
    return (match[1], int(match[2])) if match else None


def compare_builds(current_build, latest_build):
    current, latest = parse_build_revision(current_build), parse_build_revision(latest_build)
    if current is None or latest is None or current[0] != latest[0]:
        return 'manual'
    return 'build' if latest[1] > current[1] else 'current'


def public_build(value):
    """Never expose arbitrary unbounded manifest values in status responses."""
    return value if parse_build_revision(value) is not None else ''


def fetch_metadata(candidate, network):
    external, asset = candidate['external_manifest'], candidate['asset']
    if not 0 < external['size'] <= MAX_EXTERNAL_MANIFEST:
        raise UpdateError('发布清单超过大小限制。')
    raw = network.get(external['url'], external['size'])
    if len(raw) != external['size'] or hashlib.sha256(raw).hexdigest() != external['sha256']:
        raise UpdateError('发布清单与 GitHub 资产摘要不一致。')
    try:
        metadata = json.loads(raw)
    except (ValueError, RecursionError):
        raise UpdateError('发布清单格式无效。') from None
    if (not isinstance(metadata, dict) or metadata.get('file') != asset['name'] or
            metadata.get('version') != candidate['version'] or type(metadata.get('bytes')) is not int or
            metadata['bytes'] != asset['size'] or metadata.get('sha256') != asset['sha256'] or
            metadata.get('root') != 'YingXu/' or not isinstance(metadata.get('release_manifest_sha256'), str) or
            not re.fullmatch('[a-f0-9]{64}', metadata['release_manifest_sha256'])):
        raise UpdateError('此发布未提供可验证的增量清单，请使用发布页的完整包。')
    return metadata


def fetch_manifest(candidate, metadata, network):
    asset = candidate['asset']
    archive = RangeZip(asset['url'], asset['size'], network)
    member = archive.members.get('YingXu/RELEASE_MANIFEST.json')
    if member is None or member.size > MAX_MANIFEST:
        raise UpdateError('发布包缺少有界文件清单。')
    output = io.BytesIO()
    archive.extract(member, output, metadata['release_manifest_sha256'])
    raw = output.getvalue()
    manifest, files = validate_manifest(raw, candidate['version'])
    if metadata.get('source_commit', '') != manifest.get('source_commit', ''):
        raise UpdateError('外部与内部发布清单来源不一致。')
    if metadata.get('build_revision', '') != manifest.get('build_revision', ''):
        raise UpdateError('外部与内部发布清单构建修订不一致。')
    expected = {'YingXu/' + name for name in files} | {'YingXu/RELEASE_MANIFEST.json'}
    if set(archive.members) != expected or any(archive.members['YingXu/' + name].size != row['bytes'] for name, row in files.items()):
        raise UpdateError('发布包目录与文件清单不一致。')
    return archive, raw, manifest, files
