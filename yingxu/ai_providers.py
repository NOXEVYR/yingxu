"""Bounded loopback HTTP trial protocol; no execution, discovery or downloads."""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import math
import re
import select
import socket
import threading
import time
from urllib.parse import quote, urlsplit, urlunsplit

from .store import UserError

PROTOCOL = 'yingxu-http-v1'
MAX_DECLARATION = 64 * 1024
MAX_RESPONSE = 64 * 1024
MAX_HTTP_BYTES = MAX_RESPONSE + 8 * 1024
MAX_PARAMETERS = 32 * 1024
ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
SECRET_KEY = re.compile(r'(?i)(?:^|[_-])(?:authorization|credential|password|secret|token|api[_-]?key|access[_-]?key)(?:$|[_-])')
SECRET_TEXT = re.compile(r'(?i)(?:\bbearer\s+\S+|\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*\S+|\bsk-[A-Za-z0-9_-]{12,})')
STATES = frozenset(('queued', 'running', 'succeeded', 'failed', 'cancel_requested', 'cancelled'))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def identifier(value, label='标识'):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise UserError(f'{label}无效。')
    return value


def bounded_text(value, maximum=160, label='文本', empty=False):
    if (not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip())
            or any(ord(x) < 32 and x not in '\n\t' for x in value)):
        raise UserError(f'{label}无效或过长。')
    return value


def fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise UserError('请求含有缺失或不支持的字段。')
    return value


def safe_value(value, secrets=(), depth=0):
    """Reject private request data; this is not a general secret scanner."""
    if depth > 8:
        raise UserError('结构超出深度限制。')
    if isinstance(value, dict):
        if len(value) > 64:
            raise UserError('字段数量超出限制。')
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128 or SECRET_KEY.search(key):
                raise UserError('秘密字段不能进入执行记录。')
            safe_value(item, secrets, depth + 1)
    elif isinstance(value, list):
        if len(value) > 128:
            raise UserError('列表数量超出限制。')
        for item in value:
            safe_value(item, secrets, depth + 1)
    elif isinstance(value, str):
        if len(value) > 16000 or SECRET_TEXT.search(value) or any(s and s in value for s in secrets):
            raise UserError('私密值或过长文本不能进入执行记录。')
        if re.search(r'(?i)https?://[^\s]+', value):
            for match in re.findall(r'(?i)https?://[^\s]+', value):
                parsed = urlsplit(match)
                if parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise UserError('带认证或签名的地址不能进入执行记录。')
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise UserError('只支持 JSON 数据。')
    if isinstance(value, float) and not math.isfinite(value):
        raise UserError('不支持非有限数值。')


def _finite_number(value):
    # Bound before converting an arbitrary JSON integer to a C double.
    return (type(value) in (int, float) and -10 ** 100 <= value <= 10 ** 100
            and (type(value) is int or math.isfinite(value)))


def loopback_url(value):
    if not isinstance(value, str) or len(value) > 500:
        raise UserError('请选择明确的本机 HTTP 地址。')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise UserError('本机地址无效。') from None
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or port is None or not 1024 <= port <= 65535
            or not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*/?', parsed.path)):
        raise UserError('仅支持显式端口的回环 HTTP 地址和普通路径。')
    return urlunsplit(('http', f'127.0.0.1:{port}', parsed.path.rstrip('/'), '', ''))


def _json(raw):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
    except (ValueError, UnicodeError, RecursionError):
        raise UserError('服务返回了无效的 JSON。', 502) from None


def validate_schema(schema, depth=0, count=None):
    if count is None:
        count = [0]
    count[0] += 1
    if depth > 4 or count[0] > 128:
        raise UserError('输入声明结构过大。')
    fields(schema, ('type', 'description', 'properties', 'required', 'additionalProperties',
                    'items', 'enum', 'minLength', 'maxLength', 'minimum', 'maximum', 'minItems', 'maxItems'), ('type',))
    kind = schema['type']
    if kind not in ('object', 'array', 'string', 'integer', 'number', 'boolean', 'null'):
        raise UserError('不支持的输入声明类型。')
    if 'description' in schema:
        bounded_text(schema['description'], 1000, empty=True)
    applicable = {
        'object': {'properties', 'required', 'additionalProperties'},
        'array': {'items', 'minItems', 'maxItems'},
        'string': {'minLength', 'maxLength'},
        'integer': {'minimum', 'maximum'}, 'number': {'minimum', 'maximum'},
        'boolean': set(), 'null': set(),
    }[kind] | {'type', 'description', 'enum'}
    if set(schema) - applicable:
        raise UserError('输入声明关键字与类型不匹配。')
    if kind == 'object':
        props = schema.get('properties', {})
        if not isinstance(props, dict) or len(props) > 32 or schema.get('additionalProperties', False) is not False:
            raise UserError('输入声明必须限定已知字段。')
        required = schema.get('required', [])
        if (not isinstance(required, list)
                or any(not isinstance(x, str) or x not in props for x in required)
                or len(required) != len(set(required))):
            raise UserError('必需输入字段无效。')
        for key, child in props.items():
            identifier(key, '输入字段')
            validate_schema(child, depth + 1, count)
    if kind == 'array':
        if 'items' not in schema:
            raise UserError('数组必须声明条目类型。')
        validate_schema(schema['items'], depth + 1, count)
    for low, high, cap in (('minLength', 'maxLength', 16000), ('minItems', 'maxItems', 128)):
        for name in (low, high):
            if name in schema and (type(schema[name]) is not int or not 0 <= schema[name] <= cap):
                raise UserError('输入声明长度约束无效。')
        if low in schema and high in schema and schema[low] > schema[high]:
            raise UserError('输入声明上下限无效。')
    for name in ('minimum', 'maximum'):
        if name in schema and not _finite_number(schema[name]):
            raise UserError('输入声明数值约束无效。')
    if 'minimum' in schema and 'maximum' in schema and schema['minimum'] > schema['maximum']:
        raise UserError('输入声明上下限无效。')
    if 'enum' in schema:
        if not isinstance(schema['enum'], list) or not 1 <= len(schema['enum']) <= 32:
            raise UserError('输入声明枚举无效。')
        for entry in schema['enum']:
            validate_input({k: v for k, v in schema.items() if k != 'enum'}, entry)


def validate_input(schema, value):
    kind = schema['type']
    valid = {'object': isinstance(value, dict), 'array': isinstance(value, list),
             'string': isinstance(value, str), 'integer': type(value) is int,
             'number': type(value) in (int, float), 'boolean': type(value) is bool,
             'null': value is None}[kind]
    if not valid:
        raise UserError('输入参数类型不符合操作声明。')
    if 'enum' in schema and not any(type(value) is type(x) and value == x for x in schema['enum']):
        raise UserError('输入参数不在允许范围。')
    if kind == 'object':
        props = schema.get('properties', {})
        if set(value) - set(props) or set(schema.get('required', [])) - set(value):
            raise UserError('输入参数含有缺失或未知字段。')
        for key, item in value.items():
            validate_input(props[key], item)
    elif kind == 'array':
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 128):
            raise UserError('输入列表数量超出范围。')
        for item in value:
            validate_input(schema['items'], item)
    elif kind == 'string':
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 16000):
            raise UserError('输入文本长度超出范围。')
    elif kind in ('integer', 'number'):
        if (not _finite_number(value) or value < schema.get('minimum', -math.inf)
                or value > schema.get('maximum', math.inf)):
            raise UserError('输入数值超出范围。')


def declaration(raw, secrets=()):
    if len(raw) > MAX_DECLARATION:
        raise UserError('操作声明超过 64 KiB。', 413)
    data = _json(raw)
    fields(data, ('protocol', 'service_id', 'revision', 'operations', 'execution_binding'), ('protocol', 'service_id', 'revision', 'operations', 'execution_binding'))
    safe_value(data, secrets)
    if data['protocol'] != PROTOCOL:
        raise UserError('服务不支持当前试用协议。', 409)
    if data['execution_binding'] != 'declaration_sha256':
        raise UserError('服务未声明原子执行版本校验，不能提交。', 409)
    identifier(data['service_id'], '服务身份')
    bounded_text(data['revision'], 128, '声明修订')
    ops = data['operations']
    if not isinstance(ops, list) or not 1 <= len(ops) <= 16:
        raise UserError('操作数量必须为 1 至 16。')
    seen = set()
    for op in ops:
        fields(op, ('id', 'name', 'input_schema', 'supports'), ('id', 'name', 'input_schema', 'supports'))
        identifier(op['id'], '操作标识')
        bounded_text(op['name'])
        if op['id'] in seen:
            raise UserError('操作标识重复。')
        seen.add(op['id'])
        fields(op['supports'], ('query', 'lookup', 'cancel', 'idempotency'), ('query', 'lookup', 'cancel', 'idempotency'))
        if any(type(x) is not bool for x in op['supports'].values()):
            raise UserError('操作支持标记必须为布尔值。')
        validate_schema(op['input_schema'])
        if op['input_schema']['type'] != 'object':
            raise UserError('顶层输入必须是对象。')
    return data


class ProviderError(Exception):
    def __init__(self, code, message, submitted=False, http_status=0):
        super().__init__(message)
        self.code = code
        self.submitted = submitted
        self.http_status = http_status


class _ResponseLimitError(OSError):
    pass


class _BoundedReader(io.RawIOBase):
    """Windows recv shutdown is not a reliable wakeup for buffered HTTP reads."""
    def __init__(self, sock, stopped, deadline, maximum=MAX_HTTP_BYTES):
        super().__init__()
        self.sock = sock
        self.stopped = stopped
        self.deadline = deadline
        self.consumed = 0
        self.maximum = maximum

    def readable(self):
        return True

    def readinto(self, buffer):
        if not len(buffer):
            return 0
        while True:
            remaining = self.deadline - time.monotonic()
            if self.stopped() or remaining <= 0:
                raise OSError('bounded local read stopped')
            ready, _, _ = select.select([self.sock], [], [], min(.05, remaining))
            if ready:
                if self.stopped():
                    raise OSError('bounded local read stopped')
                remaining_bytes = self.maximum - self.consumed
                received = self.sock.recv_into(memoryview(buffer)[:min(len(buffer), remaining_bytes + 1)])
                self.consumed += received
                if self.consumed > self.maximum:
                    raise _ResponseLimitError('bounded response byte budget exceeded')
                return received


class _ResponseSocket:
    def __init__(self, sock, stopped, deadline, maximum=MAX_HTTP_BYTES):
        self.sock, self.stopped, self.deadline = sock, stopped, deadline
        self.maximum = maximum

    def makefile(self, _mode):
        return io.BufferedReader(_BoundedReader(self.sock, self.stopped, self.deadline, self.maximum))


class HTTPProvider:
    max_response = MAX_RESPONSE
    def __init__(self, config, secret='', timeout=2.0):
        self.base_url = loopback_url(config['base_url'])
        self.secret = secret
        self.timeout = timeout
        self._lock = threading.Lock()
        self._socket = None
        self._aborted = False

    def abort(self):
        with self._lock:
            self._aborted = True
            sock = self._socket
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _request(self, method, suffix, body=None):
        parsed = urlsplit(self.base_url)
        connection = http.client.HTTPConnection('127.0.0.1', parsed.port, timeout=self.timeout)
        submitted = False
        timer = None
        response = None
        deadline = time.monotonic() + self.timeout
        try:
            with self._lock:
                if self._aborted:
                    raise ProviderError('stopped', '本地操作已停止。')
            connection.connect()
            with self._lock:
                self._socket = connection.sock
                if self._aborted:
                    raise ProviderError('stopped', '本地操作已停止。')
            timer = threading.Timer(max(0.0, deadline - time.monotonic()), self.abort)
            timer.daemon = True
            timer.start()
            headers = {'Accept': 'application/json', 'Connection': 'close'}
            if self.secret:
                if any(ord(x) < 32 or ord(x) == 127 for x in self.secret):
                    raise ProviderError('invalid_credential', '连接凭据格式无效。')
                headers['Authorization'] = 'Bearer ' + self.secret
            encoded = canonical(body).encode('utf-8') if body is not None else None
            if encoded is not None:
                if len(encoded) > MAX_RESPONSE:
                    raise ProviderError('request_limit', '请求数据超过 64 KiB。')
                headers['Content-Type'] = 'application/json'
            submitted = method == 'POST'
            connection.request(method, (parsed.path or '') + suffix, encoded, headers)
            # Keep stdlib HTTP parsing, but make every header/body read observe
            # the total deadline and close signal even on Windows.
            response = http.client.HTTPResponse(_ResponseSocket(connection.sock, lambda: self._aborted,
                                               deadline, self.max_response + 8 * 1024))
            response.begin()
            # GET is a read; POST can create or acknowledge an asynchronous job.
            # All accepted statuses still require the same strict JSON/identity checks.
            if response.status not in ((200, 201, 202) if method == 'POST' else (200,)):
                raise ProviderError('http_error', '服务未返回成功响应。', submitted, response.status)
            if response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                raise ProviderError('invalid_response', '服务响应类型不受支持。', submitted)
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdigit() or len(length) > 10 or int(length) > self.max_response):
                raise ProviderError('response_limit', '服务响应超过边界。', submitted)
            chunks = []
            total = 0
            while True:
                chunk = response.read1(min(8192, self.max_response + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > self.max_response:
                    raise ProviderError('response_limit', '服务响应超过边界。', submitted)
            if self._aborted:
                raise ProviderError('timeout', '服务响应超时或本地操作已停止。', submitted)
            if length is not None and total != int(length):
                raise ProviderError('incomplete_response', '服务响应不完整。', submitted)
            return b''.join(chunks)
        except ProviderError:
            raise
        except _ResponseLimitError:
            raise ProviderError('response_limit', '服务响应头、正文或分块结构超过总字节预算。', submitted) from None
        except (OSError, http.client.HTTPException, ValueError):
            raise ProviderError('connection_error', '服务连接中断或响应无效。', submitted) from None
        finally:
            if timer:
                timer.cancel()
            with self._lock:
                self._socket = None
            if response:
                response.close()
            connection.close()

    def describe(self):
        raw = self._request('GET', '/capabilities')
        try:
            return declaration(raw, (self.secret,)), raw
        except UserError:
            raise ProviderError('invalid_declaration', '服务操作声明无效或包含私密数据。') from None

    def call(self, action, attempt):
        if action == 'submit':
            raw = self._request('POST', '/jobs', {
                'request_id': attempt['request_id'], 'operation_id': attempt['operation_id'],
                'expected_declaration_sha256': attempt['declaration_sha256'],
                'parameters': attempt['parameters'],
                'context': {k: attempt[k] for k in ('task_id', 'run_id', 'input_digest')},
            })
        elif action == 'lookup':
            raw = self._request('GET', '/requests/' + quote(attempt['request_id'], safe=''))
        elif action == 'query':
            raw = self._request('GET', '/jobs/' + quote(attempt['native_job_id'], safe=''))
        elif action == 'cancel':
            raw = self._request('POST', '/jobs/' + quote(attempt['native_job_id'], safe='') + '/cancel',
                                {'request_id': attempt['request_id']})
        else:
            raise ProviderError('unsupported', '不支持的操作。')
        try:
            data = _json(raw)
            fields(data, ('service_id', 'request_id', 'job_id', 'state', 'results', 'declaration_sha256'),
                   ('service_id', 'request_id', 'job_id', 'state', 'declaration_sha256'))
            if data['service_id'] != attempt['service_id'] or data['request_id'] != attempt['request_id']:
                raise UserError('响应身份不一致。')
            if data['declaration_sha256'] != attempt['declaration_sha256']:
                raise UserError('响应的执行声明版本不一致。')
            identifier(data['job_id'], '工具任务标识')
            safe_value(data['job_id'], (self.secret,))
            if attempt['native_job_id'] and data['job_id'] != attempt['native_job_id']:
                raise UserError('工具任务身份变化。')
            if data['state'] not in STATES:
                raise UserError('未知工具状态。')
            results = data.get('results', [])
            if not isinstance(results, list) or len(results) > 32:
                raise UserError('结果描述数量超出限制。')
            clean = []
            for entry in results:
                if not isinstance(entry, dict):
                    raise UserError('结果描述无效。')
                # P1 never retains URLs, provider paths, headers or raw responses.
                item = {k: entry[k] for k in ('name', 'mime_type', 'size_bytes', 'sha256', 'resource_id') if k in entry}
                for key in ('name', 'mime_type', 'resource_id'):
                    if key in item:
                        bounded_text(item[key], 200)
                if 'size_bytes' in item and (type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= 8 * 1024 ** 3):
                    raise UserError('结果大小声明无效。')
                if 'sha256' in item and not re.fullmatch('[0-9a-f]{64}', item['sha256']):
                    raise UserError('结果摘要声明无效。')
                safe_value(item, (self.secret,))
                item['verification'] = 'declared_unverified'
                item['downloaded'] = False
                clean.append(item)
            return {'native_job_id': data['job_id'], 'state': data['state'], 'results': clean}
        except (UserError, TypeError, ValueError):
            raise ProviderError('invalid_response', '服务结果无效、身份不一致或包含私密数据。', action in ('submit', 'cancel')) from None
