"""Synthetic loopback service; no media, model or external I/O."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import socket
import threading
import time


class MockCallHTTP:
    def __init__(self):
        self.lock = threading.RLock()
        self.service_id = 'synthetic-call-service'
        self.capabilities = {
            'protocol': 'yingxu-http-v1', 'service_id': self.service_id, 'revision': 'fixture-1',
            'execution_binding': 'declaration_sha256',
            'operations': [{'id': 'describe', 'name': '合成描述',
                            'input_schema': {'type': 'object', 'properties': {
                                'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
                                'required': ['prompt'], 'additionalProperties': False},
                            'supports': {'query': True, 'lookup': True, 'cancel': True, 'idempotency': True}}],
        }
        self.counts = dict.fromkeys(('capabilities', 'submit', 'query', 'lookup', 'cancel'), 0)
        self.jobs = {}
        self.received = []
        self.submit_state = self.query_state = 'running'
        self.cancel_state = 'cancel_requested'
        self.post_status = 202
        self.get_status = 200
        self.results = []
        self.drop_submit = self.drop_cancel = self.block_submit = False
        self.cap_header_bytes = self.post_header_bytes = 0
        self.drip_capabilities = False
        self.change_before_submit = False
        self.bad_execution_hash = False
        self.submit_entered = threading.Event()
        self.release_submit = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args):
                pass

            def send(self, data, status):
                raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
                try:
                    if self.path == '/capabilities' and fixture.drip_capabilities:
                        packet = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(raw)).encode() + b'\r\n\r\n' + raw
                        for byte in packet:
                            if fixture.release_submit.is_set():
                                return
                            self.wfile.write(bytes((byte,)))
                            self.wfile.flush()
                            time.sleep(.025)
                        return
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.send_header('Connection', 'close')
                    header_bytes = fixture.post_header_bytes if self.command == 'POST' else fixture.cap_header_bytes
                    for index in range(header_bytes // 4000):
                        self.send_header('X-Synthetic-' + str(index), 'z' * 4000)
                    self.end_headers()
                    self.wfile.write(raw)
                    self.wfile.flush()
                except OSError:
                    pass

            def drop(self):
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            def do_GET(self):
                with fixture.lock:
                    if self.path == '/capabilities':
                        fixture.counts['capabilities'] += 1
                        data = deepcopy(fixture.capabilities)
                        data['service_id'] = fixture.service_id
                    elif self.path.startswith('/requests/'):
                        fixture.counts['lookup'] += 1
                        data = fixture.jobs.get(self.path.rsplit('/', 1)[1])
                        if data:
                            data = dict(data, state=fixture.query_state, service_id=fixture.service_id, results=deepcopy(fixture.results))
                    elif self.path.startswith('/jobs/'):
                        fixture.counts['query'] += 1
                        data = next((x for x in fixture.jobs.values() if x['job_id'] == self.path.rsplit('/', 1)[1]), None)
                        if data:
                            data = dict(data, state=fixture.query_state, service_id=fixture.service_id, results=deepcopy(fixture.results))
                    else:
                        data = None
                self.send(data or {}, fixture.get_status if data else 404)

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                except (ValueError, OSError):
                    self.send({}, 400)
                    return
                with fixture.lock:
                    if self.path == '/jobs':
                        if fixture.change_before_submit:
                            fixture.capabilities['revision'] = 'changed-between-get-and-post'
                        declaration = dict(fixture.capabilities, service_id=fixture.service_id)
                        digest = hashlib.sha256(json.dumps(declaration, ensure_ascii=False).encode('utf-8')).hexdigest()
                        if body.get('expected_declaration_sha256') != digest:
                            self.send({}, 409)
                            return
                        fixture.counts['submit'] += 1
                        fixture.received.append(body)
                        data = {'service_id': fixture.service_id, 'request_id': body['request_id'],
                                'job_id': 'job-' + body['request_id'], 'state': fixture.submit_state,
                                'declaration_sha256': 'f' * 64 if fixture.bad_execution_hash else digest,
                                'results': deepcopy(fixture.results)}
                        fixture.jobs[body['request_id']] = data
                        drop, block = fixture.drop_submit, fixture.block_submit
                        fixture.submit_entered.set()
                    elif self.path.endswith('/cancel'):
                        fixture.counts['cancel'] += 1
                        previous = fixture.jobs.get(body['request_id'])
                        data = dict(previous, state=fixture.cancel_state) if previous else None
                        drop, block = fixture.drop_cancel, False
                    else:
                        data = None
                        drop = block = False
                if block:
                    fixture.release_submit.wait(5)
                if drop:
                    self.drop()
                else:
                    self.send(data or {}, fixture.post_status if data else 404)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def close(self):
        self.release_submit.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)
