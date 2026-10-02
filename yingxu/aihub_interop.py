"""AI Hub's explicit read-only contract. No catalogue discovery or dispatch."""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlencode, urlsplit

from .ai_providers import (HTTPProvider, ProviderError, SECRET_KEY, SECRET_TEXT,
                           _json, bounded_text, canonical, fields, identifier)
from .store import UserError

PROTOCOL = 'aihub-interop/1'
MAX_RESPONSE = 128 * 1024
MAX_DECLARATION = 32 * 1024


def digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise UserError('联动摘要或修订无效。', 502)
    return value


def safe_tree(value, secret='', depth=0, nodes=None):
    """Bound opaque declarations; never infer a runnable input schema."""
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if depth > 12 or nodes[0] > 4096:
        raise UserError('联动响应结构过大。', 502)
    if isinstance(value, dict):
        if len(value) > 64:
            raise UserError('联动响应字段过多。', 502)
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 128 or SECRET_KEY.search(key):
                raise UserError('联动响应含私密字段。', 502)
            safe_tree(child, secret, depth + 1, nodes)
    elif isinstance(value, list):
        if len(value) > 128:
            raise UserError('联动响应列表过长。', 502)
        for child in value:
            safe_tree(child, secret, depth + 1, nodes)
    elif isinstance(value, str):
        if len(value) > MAX_DECLARATION or any(ord(x) < 32 and x not in '\n\t\r' for x in value):
            raise UserError('联动文本无效或过长。', 502)
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise UserError('联动文本编码无效。', 502) from None
        if SECRET_TEXT.search(value) or (secret and secret in value):
            raise UserError('联动响应含私密值。', 502)
        for address in re.findall(r'(?i)https?://[^\s"<>]+', value):
            try:
                parsed = urlsplit(address)
            except ValueError:
                raise UserError('联动响应中的地址格式无效。', 502) from None
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise UserError('联动响应含认证或签名地址。', 502)
    elif type(value) not in (int, float, bool, type(None)):
        raise UserError('联动响应必须是 JSON。', 502)
    elif type(value) in (int, float) and (not -10 ** 100 <= value <= 10 ** 100 or (type(value) is float and not math.isfinite(value))):
        raise UserError('联动响应数值超出范围。', 502)


def _path(value):
    bounded_text(value, 1024, '联动路径')
    if not (PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()):
        raise UserError('联动工作区路径无效。', 502)
    return value  # Opaque binding only: never open or resolve the returned path.


def _client_id(value):
    bounded_text(value, 120, '声明客户端')
    reserved = {'con', 'prn', 'aux', 'nul'} | {f'{prefix}{n}' for prefix in ('com', 'lpt') for n in range(1, 10)}
    if (value != value.strip() or value in ('.', '..') or value.endswith('.')
            or re.search(r'[\\/:*?"<>|\x00-\x1f]', value)
            or value.split('.')[0].casefold() in reserved):
        raise UserError('声明客户端标识不兼容。', 502)


def _binding(data, base_url):
    if data.get('protocol') != PROTOCOL or type(data.get('schema_version')) is not int or data['schema_version'] != 1:
        raise UserError('联动协议不兼容。', 502)
    identity, workspace = data.get('identity'), data.get('workspace')
    fields(identity, ('port', 'service_instance_id', 'control_protocol', 'server_version',
                      'install_root', 'app', 'status'),
           ('port', 'service_instance_id', 'control_protocol', 'server_version', 'install_root', 'app', 'status'))
    if (identity['status'] != 'available' or identity['app'] != 'ai-hub'
            or identity['control_protocol'] != 'ai-hub-local-control-v1'
            or type(identity['port']) is not int or identity['port'] != urlsplit(base_url).port):
        raise UserError('曜核服务身份不可用或与连接地址不符。', 409)
    identifier(identity['service_instance_id'], '曜核实例')
    bounded_text(identity['server_version'], 80)
    _path(identity['install_root'])
    fields(workspace, ('status', 'root', 'binding_revision', 'revision_scope',
                       'full_configuration_revision', 'cross_machine_uuid'),
           ('status', 'root', 'binding_revision', 'revision_scope',
            'full_configuration_revision', 'cross_machine_uuid'))
    if (workspace['status'] != 'available'
            or workspace['revision_scope'] != 'local_path_and_directory_identity'
            or workspace['full_configuration_revision'] is not False
            or workspace['cross_machine_uuid'] is not False):
        raise UserError('曜核工作区绑定不可用或协议已变化。', 409)
    if _path(workspace['root']) != data.get('workspace_root'):
        raise UserError('曜核工作区身份不一致。', 502)
    digest(workspace['binding_revision'])
    digest(data.get('connection_revision'))
    return {key: data[key] for key in ('identity', 'workspace', 'workspace_root', 'connection_revision')}


def _execution(value, describe=False):
    required = {'execution_mode' if describe else 'mode': 'harness_queue',
                'worker_required': True, 'direct_execution': False,
                'routing': 'target_tool_not_declared_client', 'dispatch_supports_declaration_cas': False}
    if not isinstance(value, dict) or any(type(value.get(k)) is not type(v) or value.get(k) != v for k, v in required.items()):
        raise UserError('曜核执行合同已变化，需要重新验收适配器。', 409)
    if describe and (value.get('native_cancel') is not False or value.get('cloud_upload') is not False):
        raise UserError('曜核操作合同已变化。', 409)


def validate_describe(raw, base_url, secret=''):
    data = _json(raw)
    fields(data, ('protocol', 'schema_version', 'identity', 'workspace', 'workspace_root',
                  'connection_revision', 'operations', 'limits', 'limitations'),
           ('protocol', 'schema_version', 'identity', 'workspace', 'workspace_root',
            'connection_revision', 'operations', 'limits', 'limitations'))
    safe_tree(data, secret)
    _binding(data, base_url)
    _execution(data['operations'], True)
    read_only = data['operations'].get('read_only')
    if (not isinstance(read_only, list) or any(not isinstance(x, str) for x in read_only)
            or not {'interop_describe', 'interop_capability_snapshot'} <= set(read_only)):
        raise UserError('曜核缺少只读快照出口。', 502)
    limits = data['limits']
    if not isinstance(limits, dict) or type(limits.get('declaration_bytes')) is not int or not 0 < limits['declaration_bytes'] <= MAX_DECLARATION:
        raise UserError('曜核声明预算不兼容。', 502)
    if type(limits.get('response_bytes')) is not int or not 0 < limits['response_bytes'] <= MAX_RESPONSE:
        raise UserError('曜核响应预算不兼容。', 502)
    return data


def validate_snapshot(raw, base_url, describe, capability_id, expected_sha='', secret=''):
    data = _json(raw)
    fields(data, ('protocol', 'schema_version', 'identity', 'workspace', 'workspace_root',
                  'connection_revision', 'snapshot_id', 'selected', 'declaration', 'harness',
                  'evidence', 'execution', 'limitations'),
           ('protocol', 'schema_version', 'identity', 'workspace', 'workspace_root',
            'connection_revision', 'snapshot_id', 'selected', 'declaration', 'harness',
            'evidence', 'execution', 'limitations'))
    safe_tree(data, secret)
    if _binding(data, base_url) != _binding(describe, base_url):
        raise UserError('曜核实例或工作区已变化，保留旧选型，请重新检查连接。', 409)
    digest(data['snapshot_id'])
    _execution(data['execution'])
    selected = data['selected']
    fields(selected, ('id', 'key', 'client_id', 'target_tool', 'updated_at'),
           ('id', 'key', 'client_id', 'target_tool', 'updated_at'))
    if selected['id'] != capability_id:
        raise UserError('曜核返回了其他能力。', 502)
    for key in ('id', 'key', 'target_tool'):
        identifier(selected[key], '能力来源标识')
    _client_id(selected['client_id'])
    bounded_text(selected['updated_at'], 80)
    declaration = data['declaration']
    fields(declaration, ('origin', 'encoding', 'text', 'bytes', 'sha256', 'parsed'),
           ('origin', 'encoding', 'text', 'bytes', 'sha256', 'parsed'))
    if declaration['origin'] != 'stored_normalized_declaration' or declaration['encoding'] != 'utf-8':
        raise UserError('曜核声明来源或编码不兼容。', 502)
    if not isinstance(declaration['text'], str) or not declaration['text'].strip():
        raise UserError('能力声明原文无效。', 502)
    text = declaration['text'].encode('utf-8')
    if (len(text) > min(MAX_DECLARATION, describe['limits']['declaration_bytes'])
            or type(declaration['bytes']) is not int or declaration['bytes'] != len(text)
            or digest(declaration['sha256']) != hashlib.sha256(text).hexdigest()):
        raise UserError('能力声明的字节数或摘要不一致。', 502)
    parsed = _json(text)
    if (not isinstance(parsed, dict) or canonical(parsed) != canonical(declaration['parsed'])
            or parsed.get('key') != selected['key']):
        raise UserError('能力声明原文与解析内容不一致。', 502)
    if 'name' in parsed:
        bounded_text(parsed['name'], 160, '能力名称')
    if expected_sha and declaration['sha256'] != expected_sha:
        raise UserError('此能力已重新发布，旧选型保持原样。', 409)
    harness = data['harness']
    fields(harness, ('status', 'id', 'revision', 'enabled', 'connection_mode', 'origin'),
           ('status', 'id', 'revision', 'enabled', 'connection_mode', 'origin'))
    if harness['id'] != selected['target_tool']:
        raise UserError('工作端声明不一致。', 502)
    if harness['status'] == 'registered':
        if (type(harness['enabled']) is not bool or type(harness['revision']) is not int or harness['revision'] < 1
                or harness['connection_mode'] not in ('mcp_stdio', 'manual') or harness['origin'] != 'explicit'):
            raise UserError('工作端登记声明无效。', 502)
    elif harness['status'] in ('registration_unavailable', 'not_explicitly_registered'):
        if (harness['enabled'] is not None or harness['revision'] is not None
                or harness['connection_mode'] is not None or harness['origin'] not in (None, 'legacy_usage')):
            raise UserError('未登记工作端不能声称已启用。', 502)
    else:
        raise UserError('工作端登记状态不兼容。', 502)
    evidence = data['evidence']
    if (not isinstance(evidence, dict) or not isinstance(evidence.get('declaration'), dict)
            or not isinstance(evidence.get('actual_invocation'), dict)
            or evidence['declaration'].get('original_publisher_bytes_preserved') is not False
            or evidence.get('actual_invocation', {}).get('status') != 'unverified'
            or evidence.get('actual_invocation', {}).get('observed') is not False):
        raise UserError('曜核证据合同已变化，需要重新验收。', 409)
    if len(raw) > describe['limits']['response_bytes']:
        raise UserError('曜核快照超过声明的响应预算。', 502)
    return data


class AIHubProvider(HTTPProvider):
    max_response = MAX_RESPONSE

    def describe(self):
        raw = self._request('GET', '/api/interop/describe')
        return validate_describe(raw, self.base_url, self.secret), raw

    def snapshot(self, describe, capability_id, expected_sha=''):
        identifier(capability_id, '能力 ID')
        if expected_sha:
            digest(expected_sha)
        query = urlencode({'capability_id': capability_id,
                           'connection_revision': describe['connection_revision'],
                           '_workspace_root': describe['workspace_root'],
                           **({'expected_declaration_sha256': expected_sha} if expected_sha else {})})
        raw = self._request('GET', '/api/interop/capability-snapshot?' + query)
        return validate_snapshot(raw, self.base_url, describe, capability_id, expected_sha, self.secret), raw

    def _request(self, method, suffix, body=None):
        if method != 'GET' or body is not None or not (suffix == '/api/interop/describe' or suffix.startswith('/api/interop/capability-snapshot?')):
            raise ProviderError('read_only', '曜核适配器仅允许两个固定的只读出口。')
        return super()._request(method, suffix)

    def call(self, action, attempt):
        raise ProviderError('read_only', '曜核连接当前仅用于选型快照，不能提交、查询执行或取消任务。')


def service_error(error):
    # Never include a remote error body, URL, credential or exception repr.
    status = error.http_status if error.http_status in (404, 409, 413, 422, 503) else 502
    message = {404: '曜核能力不存在或已撤回。', 409: '曜核实例、工作区或声明已变化，请重新检查。',
               413: '曜核声明或响应超过预算。', 422: '曜核声明无效。',
               503: '曜核只读接口暂不可用。'}.get(status, '曜核连接中断或响应无效，请稍后核对。')
    return UserError(message, status)
