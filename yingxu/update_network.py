"""Update-only staggered TCP connections; urllib retains proxy and TLS policy."""
from collections import OrderedDict, deque
import errno
import functools
import http.client
import os
import selectors
import socket
import time
import urllib.request

CONNECTION_DELAY = .250
CANCEL_INTERVAL = .050
MAX_ADDRESSES = 64
_PENDING = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR,
            10035, 10036, 10037}  # Winsock's nonblocking connect results.


def _ordered_addresses(records):
    # Keep the system's first family and each family's address order, while
    # giving the other family its first attempt at the next staggered slot.
    families, seen = OrderedDict(), set()
    for record in records:
        family, kind, protocol, _, address = record
        identity = (family, kind, protocol, address)
        if family not in (socket.AF_INET, socket.AF_INET6) or identity in seen:
            continue
        seen.add(identity)
        families.setdefault(family, deque()).append(record)
        if len(seen) >= MAX_ADDRESSES:
            break
    ordered = []
    while any(families.values()):
        for group in families.values():
            if group:
                ordered.append(group.popleft())
    return ordered


def create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None,
                      *, active=None, deadline=None):
    """Race system-ordered TCP candidates without changing global socket APIs.

    DNS remains the standard synchronous getaddrinfo call. Cancellation and
    budgets are checked before/after resolution and every bounded select wait.
    """
    check = active or (lambda: None)
    check()
    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
        timeout = socket.getdefaulttimeout()
    end = float('inf') if timeout is None else time.monotonic() + timeout
    if deadline is not None:
        end = min(end, deadline)

    def remaining():
        check()
        value = end - time.monotonic()
        if value <= 0:
            raise socket.timeout('更新连接超时。')
        return value

    remaining()
    records = _ordered_addresses(socket.getaddrinfo(*address, 0, socket.SOCK_STREAM))
    remaining()
    if not records:
        raise OSError('没有可用的更新连接地址。')
    opened, pending = [], set()
    winner, last_error = None, None
    next_attempt, index = time.monotonic(), 0
    selector = selectors.DefaultSelector()
    try:
        while True:
            budget = remaining()
            now = time.monotonic()
            if index < len(records) and now >= next_attempt:
                family, kind, protocol, _, destination = records[index]
                index += 1
                candidate = None
                try:
                    # A fixed source port cannot be shared by concurrent
                    # attempts. Release its predecessor before binding again.
                    if source_address and source_address[1]:
                        for previous in list(pending):
                            selector.unregister(previous)
                            pending.remove(previous)
                            previous.close()
                    candidate = socket.socket(family, kind, protocol)
                    opened.append(candidate)
                    candidate.setblocking(False)
                    if source_address:
                        candidate.bind(source_address)
                    code = candidate.connect_ex(destination)
                    if code in (0, errno.EISCONN, 10056):
                        candidate.settimeout(None if end == float('inf') else remaining())
                        winner = candidate
                        return winner
                    if code not in _PENDING:
                        raise OSError(code, os.strerror(code))
                    selector.register(candidate, selectors.EVENT_WRITE)
                    pending.add(candidate)
                    next_attempt = time.monotonic() + CONNECTION_DELAY
                except OSError as error:
                    last_error = error
                    if candidate is not None:
                        candidate.close()
                    next_attempt = time.monotonic()
                continue
            if not pending:
                if index == len(records):
                    raise last_error or OSError('更新连接未能建立。')
                continue
            wait = min(budget, CANCEL_INTERVAL)
            if index < len(records):
                wait = min(wait, max(0, next_attempt-now))
            for key, _ in selector.select(wait):
                candidate = key.fileobj
                code = candidate.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if code == 0:
                    candidate.settimeout(None if end == float('inf') else remaining())
                    winner = candidate
                    return winner
                selector.unregister(candidate)
                pending.remove(candidate)
                candidate.close()
                last_error = OSError(code, os.strerror(code))
                next_attempt = time.monotonic()
    finally:
        selector.close()
        for candidate in opened:
            if candidate is not winner:
                candidate.close()


class UpdateHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, active, deadline):
        super().__init__()
        self.connector = functools.partial(create_connection, active=active, deadline=deadline)

    def _connection(self, host, **kwargs):
        connection = http.client.HTTPConnection(host, **kwargs)
        connection._create_connection = self.connector
        return connection

    def http_open(self, request):
        return self.do_open(self._connection, request)


class UpdateHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, active, deadline, **kwargs):
        super().__init__(**kwargs)
        self.connector = functools.partial(create_connection, active=active, deadline=deadline)

    def _connection(self, host, **kwargs):
        connection = http.client.HTTPSConnection(host, **kwargs)
        connection._create_connection = self.connector
        return connection

    def https_open(self, request):
        return self.do_open(self._connection, request, context=self._context)


def update_opener(active, deadline, redirect):
    # build_opener still installs the standard system/environment ProxyHandler.
    return urllib.request.build_opener(redirect, UpdateHTTPHandler(active, deadline),
                                       UpdateHTTPSHandler(active, deadline))
