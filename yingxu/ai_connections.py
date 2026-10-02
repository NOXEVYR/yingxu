"""Private connection references. Saving does not probe or start a service."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading

from .ai_providers import (HTTPProvider, PROTOCOL, ProviderError, bounded_text,
                           canonical, fields, identifier, loopback_url, safe_value)
from .store import UserError, now, uid
from .aihub_interop import AIHubProvider, PROTOCOL as HUB_PROTOCOL, service_error


class AIConnectionStore:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        with store.lock, store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ai_call_connections(
                id TEXT PRIMARY KEY,name TEXT NOT NULL,provider TEXT NOT NULL,
                base_url TEXT NOT NULL,credential_env TEXT NOT NULL,
                enabled INTEGER NOT NULL,revision INTEGER NOT NULL,
                declaration_raw TEXT NOT NULL DEFAULT '',declaration_sha256 TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL DEFAULT '',created TEXT NOT NULL,updated TEXT NOT NULL)''')

    def _get(self, connection_id):
        identifier(connection_id, '连接标识')
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM ai_call_connections WHERE id=?', (connection_id,)).fetchone()
        if row is None:
            raise UserError('连接不存在。', 404)
        return dict(row)

    @staticmethod
    def _public(row):
        declared = json.loads(row['declaration_raw']) if row['declaration_raw'] else {}
        hub = row['provider'] == HUB_PROTOCOL
        return {
            'id': row['id'], 'name': row['name'], 'provider': row['provider'],
            'base_url': row['base_url'], 'revision': row['revision'], 'enabled': bool(row['enabled']),
            'checked': bool(declared), 'checked_at': row['checked_at'],
            'service_id': declared.get('identity', {}).get('service_instance_id', '') if hub else declared.get('service_id', ''),
            'declaration_revision': declared.get('connection_revision', '') if hub else declared.get('revision', ''),
            'declaration_sha256': row['declaration_sha256'], 'operations': [] if hub else declared.get('operations', []),
            'read_only': hub, 'connection_revision': declared.get('connection_revision', '') if hub else '',
            'execution_mode': 'harness_queue' if hub else 'direct',
            'dispatch_version_locked': False if hub else bool(declared),
            # Only the reference name is shown in the private workbench API.
            'credential_env': row['credential_env'], 'credential_configured': bool(row['credential_env']),
        }

    def get_public(self, connection_id):
        with self.lock:
            return self._public(self._get(connection_id))

    def list(self):
        with self.lock, self.store.connection() as db:
            rows = db.execute('SELECT * FROM ai_call_connections ORDER BY created,id LIMIT 128').fetchall()
        return {'items': [self._public(dict(row)) for row in rows]}

    def put(self, body):
        fields(body, ('id', 'name', 'provider', 'base_url', 'credential_env', 'enabled', 'expected_revision'),
               ('name', 'base_url', 'expected_revision'))
        name = bounded_text(body['name'], 160, '连接名称')
        safe_value(name)
        provider = body.get('provider', PROTOCOL)
        if provider not in (PROTOCOL, HUB_PROTOCOL):
            raise UserError('请选择已支持的直接工具或曜核只读协议。')
        base_url = loopback_url(body['base_url'])
        credential_env = body.get('credential_env', '')
        if not isinstance(credential_env, str) or (credential_env and not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,127}', credential_env)):
            raise UserError('只允许填写环境变量名称，不接受凭据值。')
        enabled = body.get('enabled', True)
        if type(enabled) is not bool or type(body['expected_revision']) is not int:
            raise UserError('连接启用状态或修订无效。')
        connection_id = identifier(body['id'], '连接标识') if body.get('id') else uid()
        with self.lock, self.store.lock, self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM ai_call_connections WHERE id=?', (connection_id,)).fetchone()
            revision = previous['revision'] if previous else 0
            if body['expected_revision'] != revision:
                raise UserError('连接配置已变化，请刷新后重试。', 409)
            if previous is None and db.execute('SELECT count(*) FROM ai_call_connections').fetchone()[0] >= 128:
                raise UserError('最多保存 128 个连接。', 409)
            created = previous['created'] if previous else now()
            db.execute('''INSERT INTO ai_call_connections VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,provider=excluded.provider,
                base_url=excluded.base_url,credential_env=excluded.credential_env,enabled=excluded.enabled,
                revision=excluded.revision,declaration_raw='',declaration_sha256='',checked_at='',updated=excluded.updated''',
                (connection_id, name, provider, base_url, credential_env, int(enabled), revision + 1,
                 '', '', '', created, now()))
        return self.get_public(connection_id)

    @staticmethod
    def _secret(row):
        if not row['credential_env']:
            return ''
        value = os.environ.get(row['credential_env'], '')
        if not value or len(value) > 4096 or any(ord(x) < 32 or ord(x) == 127 for x in value):
            raise UserError('连接的凭据环境变量未就绪或格式无效。', 409)
        return value

    def resolve(self, connection_id, revision=None):
        """Internal only: config/reference and transient secret, never project data."""
        with self.lock:
            row = self._get(connection_id)
            if not row['enabled']:
                raise UserError('连接已停用。', 409)
            if revision is not None and row['revision'] != revision:
                raise UserError('原连接修订已变化，请核对原服务。', 409)
            secret = self._secret(row)
            return row, secret

    def check(self, connection_id):
        row, secret = self.resolve(connection_id)
        hub = row['provider'] == HUB_PROTOCOL
        provider = AIHubProvider(row, secret) if hub else HTTPProvider(row, secret)
        try:
            _declared, raw = provider.describe()
        except ProviderError as error:
            if hub:
                raise service_error(error) from None
            raise UserError(str(error), 502) from None
        with self.lock, self.store.lock, self.store.connection() as db:
            current = db.execute('SELECT revision,enabled FROM ai_call_connections WHERE id=?', (connection_id,)).fetchone()
            if current is None or current['revision'] != row['revision'] or not current['enabled']:
                raise UserError('检查期间连接配置已变化，请重新检查。', 409)
            db.execute('UPDATE ai_call_connections SET declaration_raw=?,declaration_sha256=?,checked_at=? WHERE id=?',
                       (raw.decode('utf-8'), hashlib.sha256(raw).hexdigest(), now(), connection_id))
        return self.get_public(connection_id)
