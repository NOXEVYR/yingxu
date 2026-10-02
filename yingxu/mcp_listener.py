"""Opt-in loopback MCP listener lifecycle; no UI or credential persistence here."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import urlsplit

from .store import UserError, clean_path, has_link


_RESERVED = frozenset({'api', 'static', 'frontend', 'assets', 'canvas', 'index', 'favicon'})


def normalize_endpoint(value):
    """Only literal loopback HTTP and a narrow ASCII route namespace."""
    if not isinstance(value, str) or len(value) > 192:
        raise UserError('MCP 地址无效或过长。')
    if value == '':
        return ''
    if not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value) or any(c in value for c in ('%', '\\', '?', '#')):
        raise UserError('MCP 地址不能含编码、空白、查询或片段。')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise UserError('MCP 端口格式无效。') from exc
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')
            or parsed.username is not None or parsed.password is not None
            or not re.fullmatch(r'(?:127\.0\.0\.1|localhost):[0-9]{1,5}', parsed.netloc, re.I)
            or port is None or not 1024 <= port <= 65535):
        raise UserError('MCP 地址只支持 http://127.0.0.1:1024至65535/路径。')
    path = parsed.path
    if len(path) > 128 or not re.fullmatch(r'/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', path):
        raise UserError('MCP 路径须为不超过 128 字符的 ASCII 安全分段。')
    if path.split('/')[1].casefold() in _RESERVED:
        raise UserError('MCP 路径不能占用工作台 API 或静态资源路径。')
    return f'http://127.0.0.1:{port}{path}'


class MCPListener:
    """Controller lock -> ProjectMCP lock; body and socket IO stay outside dispatch.

    ``factory(port)`` must return an already bound, MCP-only server with
    ``serve_forever``, ``shutdown`` and ``server_close``. This controller owns
    only additional listeners, never the main workbench server.
    """

    def __init__(self, mcp, data_root):
        self.mcp = mcp
        self.data_root = clean_path(data_root)
        self.preferences_path = self.data_root / 'mcp-listener.json'
        self._lock = threading.RLock()
        self._operations = threading.Lock()
        self._closed = False
        self._main_port = None
        self._factory = None
        self._server = self._thread = None
        self._active = False
        self._preference = ''
        self._address_error = ''
        self._generation = 0
        # Only a public address preference is read. ProjectMCP credentials are
        # neither opened nor used and no socket/thread starts at construction.
        try:
            raw = self._read_preference()
            if raw is not None:
                saved = json.loads(raw.decode('utf-8'))
                if not isinstance(saved, dict) or set(saved) != {'schema', 'endpoint'} or type(saved['schema']) is not int or saved['schema'] != 1:
                    raise ValueError()
                self._preference = normalize_endpoint(saved['endpoint'])
        except (UserError, OSError, UnicodeError, ValueError):
            self._address_error = '已保存的 MCP 地址偏好无法安全读取，请明确保存有效地址。'

    def _read_preference(self):
        clean_path(self.data_root)
        target = self.preferences_path
        if target.is_symlink() or (target.exists() and has_link(target)):
            raise UserError('MCP 地址偏好不能是链接。', 409)
        if not target.exists():
            return None
        if not target.is_file() or target.stat().st_nlink != 1 or target.stat().st_size > 4096:
            raise UserError('MCP 地址偏好文件异常。', 409)
        with target.open('rb') as handle:
            raw = handle.read(4097)
        if len(raw) > 4096:
            raise UserError('MCP 地址偏好文件过大。', 409)
        return raw

    def _write_preference_bytes(self, raw):
        # The temporary file holds an address only, never a Bearer credential.
        self._read_preference()
        stage = self.data_root / ('.mcp-listener-' + secrets.token_hex(12) + '.tmp')
        try:
            with stage.open('xb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            self._read_preference()
            os.replace(stage, self.preferences_path)
        finally:
            try: stage.unlink(missing_ok=True)
            except OSError: pass

    def _save_preference(self, endpoint):
        raw = json.dumps({'schema': 1, 'endpoint': endpoint}, separators=(',', ':')).encode('utf-8')
        self._write_preference_bytes(raw)

    def _restore_preference(self, previous):
        if previous is None:
            self._read_preference()
            self.preferences_path.unlink(missing_ok=True)
        else:
            self._write_preference_bytes(previous)

    def attach(self, main_port, factory):
        if type(main_port) is not int or not 1024 <= main_port <= 65535 or not callable(factory):
            raise UserError('主服务 MCP 接入参数无效。')
        with self._operations, self._lock:
            if self._closed: raise UserError('映序正在退出。', 409)
            if self._main_port is not None:
                raise UserError('MCP 主服务已登记。', 409)
            self._main_port, self._factory = main_port, factory
            return self._status_locked()

    def _default(self):
        return f'http://127.0.0.1:{self._main_port}/mcp' if self._main_port is not None else ''

    def _endpoint(self):
        return self._preference or self._default()

    def _status_locked(self):
        result = self.mcp.status(self._endpoint())
        result.update(endpoint=self._endpoint(), default_endpoint=self._default(),
                      custom_endpoint=bool(self._preference), address_error=self._address_error)
        return result

    def status(self):
        server = thread = None
        with self._operations:
            with self._lock:
                result = self._status_locked()
                if not result['enabled'] and (self._active or self._server is not None):
                    self._active = False
                    self._generation += 1
                    server, thread = self._server, self._thread
                    self._server = self._thread = None
            self._stop(server, thread)
            return result

    def connection(self):
        with self._lock:
            if self._closed: raise UserError('映序正在退出。', 409)
            return self.mcp.connection(self._endpoint())

    @staticmethod
    def _stop(server, thread):
        if server is None: return
        try:
            # shutdown waits for serve_forever; never invoke it on a candidate
            # whose serving thread failed to start.
            if thread is not None and thread.is_alive(): server.shutdown()
        finally:
            server.server_close()
            if thread is not None and thread.is_alive(): thread.join(5)

    def _start(self, port):
        server = self._factory(port)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .5},
                                  name='yingxu-mcp-listener', daemon=True)
        try: thread.start()
        except BaseException:
            server.server_close()
            raise
        return server, thread

    def configure(self, data):
        if not isinstance(data, dict) or type(data.get('enabled')) is not bool:
            raise UserError('MCP 开关参数无效。')
        allowed = {'enabled', 'endpoint', 'project_id'} if data['enabled'] else {'enabled', 'endpoint'}
        if set(data) - allowed or (data['enabled'] and 'project_id' not in data):
            raise UserError('MCP 配置参数无效。')
        requested = normalize_endpoint(data['endpoint']) if 'endpoint' in data else None
        candidate = candidate_thread = retired = retired_thread = None
        failure = None
        result = None
        with self._operations:
            with self._lock:
                if self._closed: raise UserError('映序正在退出。', 409)
                if self._main_port is None: raise UserError('主服务尚未就绪。', 409)
                preference = self._preference if requested is None else requested
                if preference == self._default(): preference = ''
                endpoint = preference or self._default()
                parsed = urlsplit(endpoint)
                existing_port = self._server.server_port if self._server is not None else None
                if data['enabled']:
                    pid = data['project_id']
                    if not isinstance(pid, str) or not re.fullmatch('[0-9a-f]{32}', pid):
                        raise UserError('请选择有效的项目。')
                    self.mcp.store.get_project(pid)
                changed = preference != self._preference or bool(self._address_error)
                previous_bytes = None
                saved = False
                try:
                    if data['enabled'] and parsed.port != self._main_port and parsed.port != existing_port:
                        try: candidate, candidate_thread = self._start(parsed.port)
                        except OSError as exc: raise UserError('MCP 自定义端口无法绑定，旧地址与授权保持不变。', 409) from exc
                    if changed:
                        previous_bytes = self._read_preference()
                        self._save_preference(preference)
                        saved = True
                    # Preference-save failure occurs before this call, so it
                    # cannot rotate credentials or change the active project.
                    configured = self.mcp.configure({'enabled': True, 'project_id': data['project_id']} if data['enabled'] else {'enabled': False}, endpoint)
                    retired, retired_thread = self._server, self._thread
                    if not data['enabled'] or parsed.port == self._main_port:
                        self._server = self._thread = None
                    elif candidate is not None:
                        self._server, self._thread = candidate, candidate_thread
                        candidate = candidate_thread = None
                    else:
                        retired = retired_thread = None  # reuse same bound port
                    self._preference = preference
                    self._active = data['enabled']
                    self._address_error = ''
                    self._generation += 1
                    # No fallible project/DB reads after scope publication.
                    # ProjectMCP.configure supplies its prevalidated result.
                    result = dict(configured)
                    result.update(endpoint=endpoint, default_endpoint=self._default(),
                                  custom_endpoint=bool(preference), address_error='')
                except Exception as exc:
                    if saved:
                        try: self._restore_preference(previous_bytes)
                        except (OSError, UserError):
                            self._address_error = 'MCP 切换失败，旧监听仍保留；地址偏好回退失败，请重新保存地址。'
                            failure = UserError(self._address_error, 409)
                    if failure is None:
                        failure = exc if isinstance(exc, UserError) else UserError('MCP 地址保存或启动失败，旧监听与授权保持不变。', 409)
            # Nothing here owns the controller lock: a serving thread may be
            # waiting to match/dispatch while shutdown joins its server loop.
            self._stop(candidate, candidate_thread)
            self._stop(retired, retired_thread)
            if failure is not None: raise failure
            return result

    def _matches_locked(self, port, path):
        if self._closed: return False
        expected = urlsplit(self._endpoint())
        return port == expected.port and path == expected.path

    def matches(self, port, path):
        with self._lock:
            return self._matches_locked(port, path)

    def check_origin(self, port, headers):
        with self._lock:
            if self._closed or port != urlsplit(self._endpoint()).port:
                raise UserError('MCP 请求地址已变化。', 403)
        expected = f'127.0.0.1:{port}'
        if (headers.get('Host') != expected or
                (headers.get('Origin') is not None and headers.get('Origin') != 'http://' + expected)
                or headers.get('Sec-Fetch-Site') == 'cross-site'):
            raise UserError('MCP 请求来源不受信任。', 403)

    def request_generation(self, port, path, authorization):
        with self._lock:
            if not self._active or not self._matches_locked(port, path):
                raise UserError('MCP 地址或授权已变化，请重新连接。', 403)
            self.mcp.authorize(authorization)
            return self._generation

    def dispatch(self, port, path, callback, *, generation=None):
        with self._lock:
            if (not self._active or not self._matches_locked(port, path)
                    or (generation is not None and generation != self._generation)):
                raise UserError('MCP 地址或授权已变化，请重新连接。', 403)
            if not self.mcp.status(self._endpoint())['enabled']:
                self._active = False
                self._generation += 1
                raise UserError('MCP 授权项目已不可用。', 403)
            # callback must perform only shared ProjectMCP.handle. HTTP body
            # reading and response writing belong outside this critical section.
            return callback()

    def close(self):
        with self._operations:
            with self._lock:
                if self._closed: return
                self._closed, self._active = True, False
                self._generation += 1
                server, thread = self._server, self._thread
                self._server = self._thread = None
                self.mcp.close()
            self._stop(server, thread)
