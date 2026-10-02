"""Explicit private source-grant references; no grant issuing or automatic HTTP."""
from __future__ import annotations

import hashlib
from functools import wraps
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import threading
import uuid

from .ai_providers import HTTPProvider, _json, canonical, fields, safe_value
from .aihub_interop import _binding, safe_tree
from .store import UserError, now, uid

MAX_GRANT_BYTES = 16384
_GRANT_FIELDS = {'schema', 'protocol', 'grant_id', 'role', 'subject', 'token',
                 'workspace_root', 'workspace_binding_revision', 'execution_authority_id',
                 'ledger_epoch', 'connection'}
_CONNECTION_FIELDS = {'scheme', 'host', 'port', 'app', 'install_root',
                      'service_instance_id', 'control_protocol', 'connection_revision'}


def _fail(message='来源授权文件无效或已变化。', status=409):
    raise UserError(message, status)


def _guard(operation):
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except UserError:
            raise
        except Exception:
            _fail('来源操作未完成，请重新核对。')
    return guarded


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _service_instance(value):
    # Hub's local control uses uuid4().hex; grant/descriptor strings stay exact.
    return (_uuid(value) or (isinstance(value, str)
                            and re.fullmatch(r'[a-f0-9]{32}', value) is not None))


def _absolute(value):
    return (isinstance(value, str) and 0 < len(value) <= 1024
            and not any(ord(c) < 32 for c in value)
            and not value.startswith(('\\\\', '//'))
            and (PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()))


def _stamp(st):
    # On Windows Python 3.12, lstat and fstat can disagree about st_ctime
    # after rewriting a file. Compare explicit creation time there; preserve
    # POSIX change time, file identity, byte length and last-write time.
    changed = getattr(st, 'st_birthtime_ns', st.st_ctime_ns) if os.name == 'nt' else st.st_ctime_ns
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, changed)


def _directory_identity(st):
    # Other files in TEMP may legitimately change directory metadata during
    # this read. Only replacement/reparse of an ancestor changes its identity.
    return (st.st_dev, st.st_ino)


def _checked_stat(path, directory=False):
    st = path.lstat()
    if (stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 1024
            or not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode))
            or (not directory and st.st_nlink != 1)):
        _fail()
    return st


def _read_grant(value, subject):
    """Bounded explicit file read with ancestor and opened-file identity checks."""
    try:
        if not _absolute(value) or not Path(value).is_absolute():
            _fail()
        # Reject ADS and device paths while allowing the single drive separator.
        tail = value[2:] if re.match(r'^[A-Za-z]:', value) else value
        if ':' in tail or any(p in ('.', '..') for p in re.split(r'[\\/]', tail)):
            _fail()
        path = Path(value)
        ancestors = [(p, _directory_identity(_checked_stat(p, True))) for p in path.parents]
        before = _checked_stat(path)
        if not 0 < before.st_size <= MAX_GRANT_BYTES:
            _fail()
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if _stamp(opened) != _stamp(before) or opened.st_nlink != 1:
                _fail()
            raw = stream.read(MAX_GRANT_BYTES + 1)
            if _stamp(os.fstat(stream.fileno())) != _stamp(opened):
                _fail()
        if (_stamp(_checked_stat(path)) != _stamp(before)
                or any(_directory_identity(_checked_stat(p, True)) != stamp for p, stamp in ancestors)
                or len(raw) != before.st_size or len(raw) > MAX_GRANT_BYTES):
            _fail()
        grant = _json(raw)
        if not isinstance(grant, dict) or set(grant) != _GRANT_FIELDS:
            _fail()
        connection = grant['connection']
        if not isinstance(connection, dict) or set(connection) != _CONNECTION_FIELDS:
            _fail()
        if (grant['schema'] != 'ai-hub-execution-grant/1' or grant['protocol'] != 'aihub-execution/1'
                or grant['role'] not in ('source', 'source_read') or grant['subject'] != subject
                or not all(_uuid(grant[k]) for k in ('grant_id', 'execution_authority_id', 'ledger_epoch'))
                or not _sha(grant['workspace_binding_revision']) or not _absolute(grant['workspace_root'])
                or connection['scheme'] != 'http' or connection['host'] != '127.0.0.1'
                or connection['app'] != 'ai-hub' or connection['control_protocol'] != 'ai-hub-local-control-v1'
                or type(connection['port']) is not int or not 1024 <= connection['port'] <= 65535
                or not _service_instance(connection['service_instance_id']) or not _absolute(connection['install_root'])
                or not _sha(connection['connection_revision'])
                or not isinstance(grant['token'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', grant['token'])):
            _fail()
        token = grant.pop('token')
        safe_tree(grant, token)
        if token in value:
            _fail()
        return grant, token, hashlib.sha256(raw).hexdigest()
    except Exception:
        _fail()


class SourceDescriptorProvider(HTTPProvider):
    """One observational endpoint, never a generic credentialed proxy."""
    def _request(self, method, suffix, body=None):
        if method != 'GET' or suffix != '/api/execution/describe' or body is not None:
            _fail('来源检查接口无效。')
        return super()._request(method, suffix)

    def describe(self):
        raw = self._request('GET', '/api/execution/describe')
        return _json(raw), raw


def _descriptor(value, row, token):
    try:
        safe_tree(value, token)
        if token and token in canonical(value):
            _fail()
        if (value.get('protocol') != 'aihub-execution/1' or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or value.get('routing') != 'declared_client'
                or value.get('accepted_transaction') != 'one_collaboration_database_commit'
                or value.get('provider_execution') != 'scoped_worker_adapter'
                or value.get('input_digest') != 'sha256_exact_input_json_utf8'
                or type(value.get('input_bytes')) is not int or value['input_bytes'] != 16000
                or value.get('native_cancel') is not False):
            _fail()
        bound = _binding({**value, 'protocol': 'aihub-interop/1'}, row['base_url'])
        meta, conn = row['metadata'], row['connection']
        if (bound['identity']['service_instance_id'] != conn['service_instance_id']
                or bound['identity']['install_root'] != conn['install_root']
                or bound['workspace_root'] != meta['workspace_root']
                or bound['workspace']['binding_revision'] != meta['workspace_binding_revision']
                or bound['connection_revision'] != conn['connection_revision']
                or value.get('execution_authority_id') != meta['execution_authority_id']
                or value.get('ledger_epoch') != meta['ledger_epoch']):
            _fail()
        return value
    except Exception:
        _fail('曜核执行身份或授权范围不匹配。')


class HubSourceStore:
    @_guard
    def __init__(self, store):
        self.store, self.lock = store, threading.RLock()
        with store.lock, store.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS ai_hub_source_identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1),source_authority TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO ai_hub_source_identity VALUES(1,?)', (str(uuid.uuid4()),))
            db.execute('''CREATE TABLE IF NOT EXISTS ai_hub_sources(
                id TEXT PRIMARY KEY,name TEXT NOT NULL,grant_path TEXT NOT NULL,
                grant_sha256 TEXT NOT NULL,metadata_json TEXT NOT NULL,
                enabled INTEGER NOT NULL,revision INTEGER NOT NULL,
                descriptor_raw TEXT NOT NULL DEFAULT '',checked_at TEXT NOT NULL DEFAULT '',
                created TEXT NOT NULL,updated TEXT NOT NULL)''')

    @_guard
    def identity(self):
        with self.store.connection() as db:
            value = db.execute('SELECT source_authority FROM ai_hub_source_identity WHERE singleton=1').fetchone()[0]
        if not _uuid(value):
            _fail('本机来源身份无效。')
        return {'source_authority': value}

    def _get(self, source_id):
        if not isinstance(source_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_id):
            _fail('来源标识无效。', 400)
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM ai_hub_sources WHERE id=?', (source_id,)).fetchone()
        if row is None:
            _fail('来源不存在。', 404)
        row = dict(row)
        meta = json.loads(row['metadata_json'])
        row.update(metadata=meta, connection=meta['connection'], role=meta['role'], subject=meta['subject'],
                   workspace_root=meta['workspace_root'], workspace_binding_revision=meta['workspace_binding_revision'],
                   execution_authority_id=meta['execution_authority_id'], authority=meta['execution_authority_id'],
                   ledger_epoch=meta['ledger_epoch'], base_url=f"http://127.0.0.1:{meta['connection']['port']}")
        return row

    @staticmethod
    def _public(row):
        return {**{k: row[k] for k in ('id', 'name', 'revision', 'role', 'subject', 'execution_authority_id',
                                       'ledger_epoch', 'grant_sha256', 'checked_at')},
                'enabled': bool(row['enabled']), 'checked': bool(row['descriptor_raw']),
                'execution_allowed': row['role'] == 'source' and bool(row['enabled']) and bool(row['descriptor_raw']),
                'read_only': row['role'] == 'source_read'}

    @_guard
    def get(self, source_id):
        with self.lock:
            return self._public(self._get(source_id))

    @_guard
    def list(self):
        with self.lock, self.store.connection() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM ai_hub_sources ORDER BY created,id LIMIT 16')]
            return {'identity': self.identity(), 'items': [self.get(i) for i in ids]}

    @_guard
    def put(self, body):
        fields(body, ('id', 'name', 'grant_path', 'expected_revision', 'enabled'), ('name', 'grant_path', 'expected_revision'))
        if (not isinstance(body['name'], str) or not body['name'].strip() or len(body['name']) > 80
                or any(ord(c) < 32 for c in body['name']) or type(body['expected_revision']) is not int
                or not 0 <= body['expected_revision'] < 2 ** 31 or type(body.get('enabled', True)) is not bool):
            _fail('来源名称、启用状态或修订无效。', 400)
        source_id = body['id'] if 'id' in body else uid()
        if not isinstance(source_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_id):
            _fail('来源标识无效。', 400)
        with self.lock, self.store.lock:
            meta, token, file_sha = _read_grant(body['grant_path'], self.identity()['source_authority'])
            try:
                safe_value(body['name'], (token,))
            except Exception:
                _fail('来源名称无效。', 400)
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT revision,created FROM ai_hub_sources WHERE id=?', (source_id,)).fetchone()
                if body['expected_revision'] != (old['revision'] if old else 0):
                    _fail('来源配置已变化，请刷新后重试。')
                if old is None and db.execute('SELECT count(*) FROM ai_hub_sources').fetchone()[0] >= 16:
                    _fail('最多保存16个来源。')
                db.execute('''INSERT INTO ai_hub_sources VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,grant_path=excluded.grant_path,
                    grant_sha256=excluded.grant_sha256,metadata_json=excluded.metadata_json,
                    enabled=excluded.enabled,revision=excluded.revision,descriptor_raw='',checked_at='',updated=excluded.updated''',
                    (source_id, body['name'], body['grant_path'], file_sha, canonical(meta), int(body.get('enabled', True)),
                     body['expected_revision'] + 1, '', '', old['created'] if old else now(), now()))
            return self.get(source_id)

    @_guard
    def resolve(self, source_id, revision=None):
        with self.lock:
            row = self._get(source_id)
            if (not row['enabled'] or (revision is not None and
                    (type(revision) is not int or revision != row['revision']))):
                _fail('来源已停用或原修订已变化。')
            meta, token, file_sha = _read_grant(row['grant_path'], self.identity()['source_authority'])
            if file_sha != row['grant_sha256'] or meta != row['metadata']:
                _fail()
            return row, token

    @_guard
    def check(self, source_id):
        row, token = self.resolve(source_id)
        try:
            value, raw = SourceDescriptorProvider(row, token).describe()
            if not isinstance(raw, bytes) or len(raw) > 65536 or _json(raw) != value:
                _fail()
            _descriptor(value, row, token)
        except Exception:
            _fail('曜核来源检查未完成，请核对授权与服务。')
        with self.lock, self.store.lock:
            current, _token = self.resolve(source_id, row['revision'])
            if current['grant_sha256'] != row['grant_sha256']:
                _fail()
            with self.store.connection() as db:
                db.execute('UPDATE ai_hub_sources SET descriptor_raw=?,checked_at=? WHERE id=? AND revision=?',
                           (raw.decode('utf-8'), now(), source_id, row['revision']))
            return self.get(source_id)

    @_guard
    def checked_descriptor(self, source_id, revision=None):
        row, token = self.resolve(source_id, revision)
        if not row['descriptor_raw']:
            _fail('请先显式检查曜核来源。')
        return _descriptor(_json(row['descriptor_raw'].encode('utf-8')), row, token)
