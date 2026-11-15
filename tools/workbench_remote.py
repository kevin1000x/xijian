"""Public-bundle runtime node. Loopback server, suitable for an SSH tunnel.

The node owns its approved adapter configuration. Clients cannot supply commands,
paths or model overrides. Durable IDs consume an attempt before adapter entry.
"""
import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from tools import local_synthetic_workflow as storage
from tools.workbench_connectors import ConnectorError
from tools.workbench_lifecycle import atomic

MAX_BYTES = 65536
MAX_WIRE = 256 * 1024
TERMINAL = {'complete', 'cancelled', 'unresolved'}
PUBLIC_FAILURE_CODES = frozenset({
    'PUBLIC_OUTPUT_INVALID', 'PUBLIC_RESOURCE_LIMIT', 'PUBLIC_SOURCE_REJECTED',
    'PUBLIC_RUNTIME_ERROR',
})


def require(value, code='remote_node_rejected'):
    if not value:
        raise ConnectorError(code, '运行节点请求未完成；请查看任务状态。')


def task_id(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{32}', value.replace('-', ''))
            and str(uuid.UUID(value)) == value, 'remote_task_id')
    return value


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def public_bytes(bundle):
    base_fields = {'version', 'objective', 'roles', 'schema', 'examples', 'output', 'program_contract'}
    require(type(bundle) is dict and set(bundle) in (base_fields, base_fields | {'repair'})
            and type(bundle['version']) is int and bundle['version'] == 1, 'remote_public_bundle')
    if 'repair' in bundle:
        repair = bundle['repair']
        require(type(repair) is dict and set(repair) == {
            'base_bundle_sha256', 'previous_candidate_sha256', 'previous_candidate_source',
            'public_failure_code'}, 'remote_public_repair')
        base = {field: bundle[field] for field in base_fields}
        previous = repair['previous_candidate_source']
        reason = repair['public_failure_code']
        require(type(previous) is str and previous.strip() and type(reason) is str
            and reason in PUBLIC_FAILURE_CODES and repair['base_bundle_sha256'] == sha(storage._bytes(base))
            and repair['previous_candidate_sha256'] == sha(previous.encode('utf-8')), 'remote_public_repair')
    raw = storage._bytes(bundle)
    require(len(raw) <= MAX_BYTES, 'remote_public_bundle_size')
    return raw


class RuntimeNode:
    encode = staticmethod(storage._bytes)

    def __init__(self, root, adapter):
        self.root, self.adapter = Path(root).resolve(), adapter
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root/'tasks').mkdir(exist_ok=True)
        # A lifetime writer lock prevents two daemons recovering each other's work.
        try:
            with (self.root/'writer.lock').open('xb') as stream: stream.write(b'0')
        except FileExistsError:
            pass
        self._writer = storage._Writer(self.root)
        self._writer.__enter__()
        self.lock = threading.RLock()
        self.active = None
        self.worker = None
        identity = self.root/'identity.json'
        if not identity.exists(): atomic(identity, dict(node_id=str(uuid.uuid4())))
        self.node_id = json.loads(identity.read_text('utf-8'))['node_id']
        self.runtime_sha256 = sha(storage._bytes(dict(source=adapter.source, profile=adapter.profile)))
        for path in (self.root/'tasks').glob('*/receipt.json'):
            record = json.loads(path.read_text('utf-8'))
            if record['status'] not in TERMINAL:
                record.update(status='unresolved', reason='node_restarted')
                atomic(path, record)

    def close(self):
        if self.active: self.cancel(self.active)
        if self.worker:
            self.worker.join(5)
            require(not self.worker.is_alive(), 'remote_worker_still_active')
        self._writer.__exit__(None, None, None)

    def capabilities(self):
        state = self.adapter.readiness()
        return dict(protocol='xijian-node-v1', node_id=self.node_id,
            runtime_sha256=self.runtime_sha256, source=self.adapter.source,
            model=self.adapter.profile.get('model'), ready=state.get('ready') is True,
            allocation=self.adapter.profile.get('allocation'), busy=self.active is not None,
            max_bundle_bytes=MAX_BYTES, public_repair=True)

    def _path(self, key):
        return self.root/'tasks'/task_id(key)

    def status(self, key):
        with self.lock:
            path = self._path(key)/'receipt.json'
            require(path.is_file(), 'remote_task_missing')
            return json.loads(path.read_text('utf-8'))

    def submit(self, data):
        require(type(data) is dict and set(data) == {
            'task_id', 'node_id', 'runtime_sha256', 'bundle', 'bundle_sha256'})
        folder = self._path(data['task_id'])
        raw = public_bytes(data['bundle'])
        require(data['node_id'] == self.node_id and data['runtime_sha256'] == self.runtime_sha256,
                'remote_identity_changed')
        require(sha(raw) == data['bundle_sha256'], 'remote_bundle_changed')
        with self.lock:
            if folder.exists():
                prior = self.status(data['task_id'])
                require(prior['bundle_sha256'] == sha(raw) and
                        prior['runtime_sha256'] == self.runtime_sha256, 'remote_task_conflict')
                return prior
            require(self.active is None, 'remote_node_busy')
            require(self.adapter.readiness().get('ready') is True, 'remote_adapter_unready')
            require(sha(storage._bytes(dict(source=self.adapter.source, profile=self.adapter.profile)))
                    == self.runtime_sha256, 'remote_identity_changed')
            folder.mkdir()
            storage._publish(folder/'public-bundle.json', raw)
            record = {k: data[k] for k in ('task_id', 'node_id', 'runtime_sha256', 'bundle_sha256')}
            record.update(status='running', source=self.adapter.source, model=self.adapter.profile.get('model'))
            atomic(folder/'receipt.json', record)
            self.active = data['task_id']
            self.worker = threading.Thread(target=self._run, args=(data['task_id'], copy.deepcopy(data['bundle'])), daemon=True)
            self.worker.start()
            return record

    def _run(self, key, bundle):
        try:
            with self.lock:
                require(self.status(key)['status'] == 'running', 'remote_cancelled')
            candidate = self.adapter.call(bundle)
            require(type(candidate) is bytes and 0 < len(candidate) <= MAX_BYTES)
            candidate.decode('utf-8')
            require(candidate.strip(), 'remote_empty_candidate')
            with self.lock:
                record = self.status(key)
                if record['status'] == 'cancelling':
                    record.update(status='cancelled')
                else:
                    storage._publish(self._path(key)/'candidate.py', candidate)
                    record.update(status='complete', candidate_sha256=sha(candidate))
                atomic(self._path(key)/'receipt.json', record)
        except Exception:
            with self.lock:
                record = self.status(key)
                record.update(status='cancelled' if record['status'] == 'cancelling' else 'unresolved')
                atomic(self._path(key)/'receipt.json', record)
        finally:
            with self.lock: self.active = None

    def cancel(self, key):
        with self.lock:
            record = self.status(key)
            if record['status'] in TERMINAL: return record
            require(self.active == key, 'remote_task_inactive')
            record.update(status='cancelling')
            atomic(self._path(key)/'receipt.json', record)
            self.adapter.cancel()
            return record

    def candidate(self, key):
        with self.lock:
            record = self.status(key)
            require(record['status'] == 'complete', 'remote_candidate_unavailable')
            raw = (self._path(key)/'candidate.py').read_bytes()
            require(sha(raw) == record['candidate_sha256'], 'remote_candidate_changed')
            return raw


def make_server(node, token, port=0):
    require(type(token) is str and 32 <= len(token) <= 256 and token.isascii()
            and not any(c.isspace() for c in token), 'remote_token_required')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def setup(self):
            super().setup()
            self.connection.settimeout(5)
        def _handle(self):
            try:
                require(not self.headers.get('Origin') and
                    hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+token), 'remote_authentication')
                parts = self.path.split('/')
                if self.command == 'GET' and self.path == '/v1/capabilities':
                    result = node.capabilities()
                elif self.command == 'POST' and self.path == '/v1/tasks':
                    length = int(self.headers.get('Content-Length', '0'))
                    require(0 < length <= MAX_WIRE and not self.headers.get('Transfer-Encoding'))
                    result = node.submit(json.loads(self.rfile.read(length)))
                elif len(parts) in (4, 5) and parts[1:3] == ['v1', 'tasks']:
                    key = task_id(parts[3])
                    if self.command == 'GET' and len(parts) == 4: result = node.status(key)
                    elif self.command == 'POST' and len(parts) == 5 and parts[4] == 'cancel': result = node.cancel(key)
                    elif self.command == 'GET' and len(parts) == 5 and parts[4] == 'candidate':
                        raw = node.candidate(key)
                        result = dict(task_id=key, candidate_sha256=sha(raw), candidate_b64=base64.b64encode(raw).decode('ascii'))
                    else: raise ValueError()
                else: raise ValueError()
                raw, code = storage._bytes(result), 200
            except Exception as error:
                raw, code = storage._bytes(dict(error=getattr(error, 'code', 'remote_request_invalid'))), 400
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            try: self.wfile.write(raw)
            except OSError: pass
        do_GET = do_POST = _handle
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


# Isolated stdlib worker gives DNS, connect and slow-drip reads one hard deadline.
# Token and body travel on stdin, never argv, inherited proxies or log files.
HTTP_WORKER = '''import json, sys
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *a, **k): raise ValueError('redirect')
try:
    config=json.loads(sys.stdin.buffer.read(300000))
    req=Request(config['url'],data=config['body'].encode('utf-8') if config['body'] is not None else None,headers=config['headers'])
    with build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=5) as response:
        raw=response.read(262145)
    if len(raw)>262144: raise ValueError('size')
    sys.stdout.buffer.write(raw)
except Exception: sys.exit(2)
'''


class RemoteAdapter:
    source = 'remote_node'

    def __init__(self, root, budget, profile, *, token):
        self.root, self.budget = Path(root), budget
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile, self.token = copy.deepcopy(profile), token
        url = urlsplit(self.profile.get('url', ''))
        require(url.scheme == 'https' or (url.scheme == 'http' and url.hostname in {'127.0.0.1', '::1'}), 'remote_url')
        require(url.hostname and not url.username and not url.password and not url.query and not url.fragment
                and url.path in ('', '/'), 'remote_url')
        require(type(token) is str and 32 <= len(token) <= 256 and token.isascii()
                and not any(c.isspace() for c in token), 'remote_token_required')
        timeout = self.profile.get('timeout_seconds', 120)
        require(type(timeout) is int and 1 <= timeout <= 300, 'remote_timeout')
        self.url = self.profile['url'].rstrip('/')
        self._lock = threading.Lock()
        self._operation = None
        self._cancel = threading.Event()
        self._capability = None
        self.last_receipt = None

    def _request(self, path, data=None, timeout=5):
        config = dict(url=self.url+path, body=storage._bytes(data).decode('utf-8') if data is not None else None,
            headers={'Authorization':'Bearer '+self.token, 'Content-Type':'application/json'})
        try:
            with subprocess.Popen([sys.executable,'-I','-B','-c',HTTP_WORKER], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)) as process:
                try: raw, _ = process.communicate(storage._bytes(config), timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill(); process.communicate()
                    raise
                require(process.returncode == 0)
            require(len(raw) <= MAX_WIRE)
            value = json.loads(raw)
            require(type(value) is dict)
            return value
        except Exception:
            raise ConnectorError('remote_request_unresolved', '运行节点连接中断；任务编号已保留，请查询原任务。') from None

    def probe(self):
        self._capability = None
        info = self._request('/v1/capabilities')
        require(info.get('protocol') == 'xijian-node-v1' and type(info.get('allocation')) is int
                and 1 <= info['allocation'] <= 10_000_000 and type(info.get('model')) is str,
                'remote_capabilities_invalid')
        task_id(info.get('node_id'))
        require(type(info.get('runtime_sha256')) is str and re.fullmatch('[0-9a-f]{64}', info['runtime_sha256']))
        for key in ('node_id', 'runtime_sha256', 'model', 'allocation'):
            require(key not in self.profile or self.profile[key] == info[key], 'remote_identity_changed')
        self.profile.update({k:info[k] for k in ('node_id','runtime_sha256','model','allocation')})
        self._capability = info
        return info

    def readiness(self):
        ready = bool(self._capability and self._capability['ready'] and self.profile.get('authorized') is True
            and self.budget.snapshot()['remaining'] >= self.profile['allocation'])
        return dict(ready=ready, live=True, source=self.source, model=self.profile.get('model'),
                    missing=[] if ready else ['remote_connection'], node=self._capability,
                    operation_id=self._operation)

    def operation(self): return self._operation

    def cancel(self):
        self._cancel.set()
        key = self._operation
        if key: return self._request('/v1/tasks/'+key+'/cancel', {})

    def query(self, key, timeout=5):
        value = self._request('/v1/tasks/'+task_id(key), timeout=timeout)
        self._check_receipt(value, key)
        return value

    def _check_receipt(self, value, key):
        require(value.get('task_id') == key and value.get('node_id') == self.profile['node_id']
                and value.get('runtime_sha256') == self.profile['runtime_sha256'], 'remote_identity_changed')
        local = json.loads((self.root/(key+'.json')).read_text('utf-8'))
        require(value.get('bundle_sha256') == local['bundle_sha256']
                and value.get('node_id') == local['node_id']
                and value.get('runtime_sha256') == local['runtime_sha256']
                and value.get('status') in TERMINAL | {'running','cancelling'}, 'remote_receipt_changed')

    def supports_public_repair(self):
        return bool(self._capability and self._capability.get('public_repair') is True)

    def submission_receipt(self, bundle, key):
        require('repair' not in bundle or self.supports_public_repair(), 'remote_repair_unsupported')
        return dict(task_id=task_id(key), operation_id=key, source=self.source,
            node_id=self.profile['node_id'], runtime_sha256=self.profile['runtime_sha256'],
            bundle_sha256=sha(public_bytes(bundle)), status='unresolved')

    def _fetch_candidate(self, state, timeout):
        require(state['status'] == 'complete', 'remote_candidate_unavailable')
        key = state['task_id']
        result = self._request('/v1/tasks/'+key+'/candidate', timeout=timeout)
        require(result.get('task_id') == key, 'remote_candidate_changed')
        candidate = base64.b64decode(result['candidate_b64'], validate=True)
        require(0 < len(candidate) <= MAX_BYTES and sha(candidate) == result.get('candidate_sha256')
                == state.get('candidate_sha256'), 'remote_candidate_changed')
        candidate.decode('utf-8')
        return candidate

    def recover_candidate(self, receipt):
        key = task_id(receipt['task_id'])
        state = self.query(key)
        require(all(state.get(field) == receipt.get(field) for field in
                    ('task_id', 'node_id', 'runtime_sha256', 'bundle_sha256')),
                'remote_receipt_changed')
        if receipt.get('candidate_sha256'):
            require(state.get('candidate_sha256') == receipt['candidate_sha256'],
                    'remote_candidate_changed')
        return self._fetch_candidate(state, 5), state

    def call(self, bundle, *, task_key=None):
        require(self._lock.acquire(False), 'remote_operation_active')
        reserved = False
        try:
            self.last_receipt = None
            require(self.readiness()['ready'], 'remote_not_ready')
            key = task_key if task_key is not None else str(uuid.uuid4())
            record = self.submission_receipt(bundle, key)
            # Immutable initial admission: even a direct caller cannot replay a client ID.
            receipt_path = self.root/(key+'.json')
            require(not receipt_path.exists(), 'remote_task_already_submitted')
            storage._publish(receipt_path, storage._bytes(record))
            self.last_receipt = record
            self.budget.reserve(key, self.profile['allocation']); reserved = True
            self._cancel.clear()
            self._operation = key
            deadline = time.monotonic()+self.profile.get('timeout_seconds',120)
            # Exactly one POST. A lost reply never causes a resubmission.
            state = self._request('/v1/tasks', dict(task_id=key, node_id=record['node_id'],
                runtime_sha256=record['runtime_sha256'], bundle=bundle, bundle_sha256=record['bundle_sha256']),
                timeout=min(5, max(.01, deadline-time.monotonic())))
            self._check_receipt(state, key)
            while state['status'] not in TERMINAL:
                require(not self._cancel.is_set(), 'remote_cancelled')
                require(time.monotonic() < deadline, 'remote_deadline')
                self._cancel.wait(.1)
                state = self.query(key, timeout=min(5, max(.01, deadline-time.monotonic())))
            require(state['status'] == 'complete' and not self._cancel.is_set(), 'remote_generation_unresolved')
            candidate = self._fetch_candidate(state, min(5, max(.01, deadline-time.monotonic())))
            require(not self._cancel.is_set(), 'remote_cancelled')
            record.update(status='complete', candidate_sha256=sha(candidate), runtime_source=state['source'])
            return candidate
        finally:
            self._operation = None
            try:
                if reserved:
                    record['token_budget'] = self.budget.settle(key)
                    atomic(self.root/(key+'.json'), record)
            finally:
                self._lock.release()


def main():
    import argparse
    from tools.workbench_external import ExternalAgents
    from tools.workbench_budget import TokenBudget
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, help='Dedicated node state directory')
    parser.add_argument('--agent-state', required=True, help='Previously approved external-agents settings directory')
    parser.add_argument('--budget-root', required=True, help='Existing shared token ledger; preserve prior usage')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    root = Path(args.state).resolve()
    budget = TokenBudget(args.budget_root)
    agents = ExternalAgents(args.agent_state, budget)
    _, adapter = agents.resolve()
    require(adapter is not None, 'remote_adapter_unready')
    node = RuntimeNode(root, adapter)
    server = make_server(node, os.environ.get('XIJIAN_NODE_TOKEN'), args.port)
    print(json.dumps(dict(url='http://127.0.0.1:%d'%server.server_port, **node.capabilities())), flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        server.server_close()
        node.close()


if __name__ == '__main__': main()
