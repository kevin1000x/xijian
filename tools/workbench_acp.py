"""ACP v1 transport with explicit admission; protocol support is not isolation.

Only code-reviewed, digest-pinned native runtimes may be launched. A successful
initialize is never authentication, budget, or end-to-end task acceptance.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from tools.workbench_connectors import ConnectorError
from tools.workbench_qoder import _spawn_in_job
from tools import local_synthetic_workflow as storage
from tools import workbench_opencode

# Official portable executable validated against a local model server. Policy
# remains developer-owned; the UI cannot add executable trust or launch flags.
REVIEWED_RUNTIMES = {('opencode', workbench_opencode.VERSION, workbench_opencode.EXECUTABLE_SHA256):
    {'args':['acp','--hostname','127.0.0.1','--port','0'], 'candidate_mode':'text','native':'opencode'}}
MAX_FILE = 65536
MAX_LINE = 131072
MAX_OUTPUT = 2 * 1024 * 1024


def _fail(code):
    raise ConnectorError(code, 'ACP connection stopped: ' + code)


class ACPTransport:
    """One subprocess, newline-delimited JSON-RPC; no retries or shell bridge.

    Callers must review the executable BEFORE construction. Low-level transport
    is also used by synthetic subprocess tests; it cannot sandbox native tools.
    """
    def __init__(self, command, workspace, timeout=30, env=None):
        self.workspace = Path(workspace).resolve()
        self.timeout = max(.05, min(float(timeout), 300))
        self.deadline = time.monotonic() + self.timeout
        self.messages = queue.Queue(maxsize=64)
        self.cancelled = threading.Event()
        self.reader_stop = threading.Event()
        self.failure = None
        self.session_id = None
        self.serial = 0
        self.tree = None
        self._write_lock = threading.Lock()
        self._output_lock = threading.Lock()
        self._output = 0
        self._candidate_chunks = []
        self._candidate_size = 0
        options = dict(cwd=str(self.workspace), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False, creationflags=0x08000000 if os.name == 'nt' else 0,
            start_new_session=os.name != 'nt')
        if os.name == 'nt':
            self.process, self.tree = _spawn_in_job(command, **options)
        else:
            self.process = subprocess.Popen(command, **options)
        self.threads = [threading.Thread(target=self._read, args=(self.process.stdout, True), daemon=True),
                        threading.Thread(target=self._read, args=(self.process.stderr, False), daemon=True)]
        for thread in self.threads: thread.start()

    def __enter__(self): return self
    def __exit__(self, *unused): self.close()

    def _read(self, stream, protocol):
        try:
            while not self.reader_stop.is_set():
                raw = stream.readline(MAX_LINE + 1)
                if not raw: break
                with self._output_lock:
                    self._output += len(raw)
                    too_big = self._output > MAX_OUTPUT or len(raw) > MAX_LINE
                if too_big:
                    self.failure = 'acp_output_limit'
                    break
                if protocol:
                    try:
                        message = json.loads(raw.decode('utf-8'))
                    except (ValueError, UnicodeError):
                        self.failure = 'acp_protocol'
                        break
                    try: self.messages.put_nowait(message)
                    except queue.Full:
                        self.failure = 'acp_output_limit'
                        break
        except (OSError, ValueError):
            if not self.reader_stop.is_set(): self.failure = 'acp_stream'

    def _send(self, message):
        raw = json.dumps(message, ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n'
        if len(raw) > MAX_LINE: _fail('acp_input_limit')
        failed = []
        def write():
            with self._write_lock:
                try:
                    self.process.stdin.write(raw)
                    self.process.stdin.flush()
                except (OSError, ValueError): failed.append(True)
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        writer.join(max(0, self.deadline-time.monotonic()))
        if writer.is_alive():
            self.close()
            writer.join(timeout=1)
            _fail('acp_timeout')
        if failed: _fail('acp_stream')

    def request(self, method, params):
        if self.cancelled.is_set(): _fail('acp_cancelled')
        self.serial += 1
        request_id = self.serial
        early_updates = []
        self._send({'jsonrpc':'2.0','id':request_id,'method':method,'params':params})
        deadline = self.deadline
        while True:
            if self.cancelled.is_set(): _fail('acp_cancelled')
            if self.failure: _fail(self.failure)
            if time.monotonic() >= deadline: _fail('acp_timeout')
            try: message = self.messages.get(timeout=.05)
            except queue.Empty:
                if self.failure: _fail(self.failure)
                if self.process.poll() is not None and not self.threads[0].is_alive(): _fail('acp_incomplete')
                continue
            if not isinstance(message, dict) or message.get('jsonrpc') != '2.0': _fail('acp_protocol')
            if 'method' in message:
                if not isinstance(message['method'], str) or not isinstance(message.get('params', {}), dict): _fail('acp_protocol')
                if 'id' in message:
                    if type(message['id']) not in (int, str): _fail('acp_protocol')
                    try:
                        result = self.client_request(message['method'], message.get('params', {}))
                    except ConnectorError:
                        self._send({'jsonrpc':'2.0','id':message['id'],'error':{'code':-32601,'message':'Not permitted'}})
                        _fail('acp_client_request_denied')
                    self._send({'jsonrpc':'2.0','id':message['id'],'result':result})
                elif message['method'] == 'session/update' and method == 'session/new' and self.session_id is None:
                    # Native agents may advertise commands before returning the
                    # new ID. Buffer, then correlate against the actual response.
                    if len(early_updates) >= 64: _fail('acp_output_limit')
                    early_updates.append(message['params'])
                elif message['method'] != 'session/update' or message.get('params', {}).get('sessionId') != self.session_id:
                    _fail('acp_notification')
                else:
                    self._session_update(message['params'])
                continue
            if type(message.get('id')) is not int or message['id'] != request_id: _fail('acp_response_id')
            if ('result' in message) == ('error' in message): _fail('acp_protocol')
            if 'error' in message:
                error = message['error']
                _fail('acp_auth_required' if isinstance(error, dict) and error.get('code') == -32000 else 'acp_remote_error')
            if not isinstance(message['result'], dict): _fail('acp_protocol')
            for update in early_updates:
                session_id = message['result'].get('sessionId')
                if not isinstance(session_id, str) or not session_id or update.get('sessionId') != session_id:
                    _fail('acp_notification')
                self._session_update(update)
            return message['result']

    def _session_update(self, params):
        update = params.get('update')
        if not isinstance(update, dict): _fail('acp_protocol')
        if update.get('sessionUpdate') != 'agent_message_chunk': return
        content = update.get('content')
        if not isinstance(content, dict) or content.get('type') != 'text' or not isinstance(content.get('text'), str):
            _fail('acp_candidate_format')
        raw = content['text'].encode('utf-8')
        if self._candidate_size + len(raw) > MAX_FILE: _fail('acp_file')
        self._candidate_size += len(raw)
        self._candidate_chunks.append(raw)

    def candidate_text(self):
        """Join native ACP text deltas verbatim; never strip fences or rewrite code."""
        value = b''.join(self._candidate_chunks)
        if not value.strip(): _fail('acp_candidate_missing')
        return value

    def initialize(self):
        value = self.request('initialize', {'protocolVersion':1,
            'clientInfo':{'name':'xijian','version':'acp-v1'},
            'clientCapabilities':{'fs':{'readTextFile':True,'writeTextFile':True},'terminal':False}})
        if type(value.get('protocolVersion')) is not int or value['protocolVersion'] != 1: _fail('acp_version')
        if not isinstance(value.get('agentCapabilities'), dict) or not isinstance(value.get('authMethods', []), list): _fail('acp_protocol')
        return value

    def client_request(self, method, params):
        if self.session_id is None or params.get('sessionId') != self.session_id: _fail('acp_session')
        if method == 'session/request_permission': return {'outcome':{'outcome':'cancelled'}}
        if method not in ('fs/read_text_file','fs/write_text_file'): _fail('acp_tool_denied')
        path = Path(params.get('path', ''))
        if not path.is_absolute() or path.parent != self.workspace: _fail('acp_path')
        allowed = ('public-bundle.json','analysis.py') if method == 'fs/read_text_file' else ('analysis.py',)
        if path.name not in allowed: _fail('acp_path')
        for part in (path, self.workspace) + tuple(self.workspace.parents):
            if part.exists() and (part.is_symlink() or getattr(part.lstat(),'st_file_attributes',0) & 0x400): _fail('acp_path')
        if path.resolve().parent != self.workspace: _fail('acp_path')
        if method == 'fs/write_text_file':
            content = params.get('content')
            if not isinstance(content, str): _fail('acp_file')
            raw = content.encode('utf-8')
            if len(raw) > MAX_FILE: _fail('acp_file')
            path.write_bytes(raw)
            return {}
        if not path.is_file() or path.stat().st_size > MAX_FILE: _fail('acp_file')
        content = path.read_bytes().decode('utf-8')
        if 'line' in params or 'limit' in params:
            line, limit = params.get('line', 1), params.get('limit', MAX_FILE)
            if type(line) is not int or line < 1 or type(limit) is not int or limit < 1: _fail('acp_file')
            content = ''.join(content.splitlines(keepends=True)[line-1:line-1+limit])
        return {'content':content}

    def cancel(self):
        self.cancelled.set()
        # Never block cancellation on a peer which has stopped reading stdin.
        self.close()

    def close(self):
        self.reader_stop.set()
        if self.tree: self.tree.terminate()
        elif self.process.poll() is None:
            if os.name != 'nt':
                try: os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            else: self.process.kill()
        try: self.process.wait(timeout=5)
        except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(timeout=5)
        for thread in getattr(self, 'threads', []): thread.join(timeout=1)
        if self.tree: self.tree.close(); self.tree = None
        for stream in (self.process.stdin,self.process.stdout,self.process.stderr):
            try: stream.close()
            except OSError: pass


class ACPAdapter:
    source = 'acp'

    def __init__(self, root, budget, profile):
        self.root, self.budget = Path(root).resolve(), budget
        self.profile = copy.deepcopy(profile)
        self.last_receipt = None
        self._client = None
        self._operation = None
        self._lock = threading.Lock()
        self._protocol_checked = False
        self._authentication = 'unknown'
        self._cancelled = threading.Event()

    def _runtime_policy(self):
        try:
            path = Path(self.profile['executable'])
            if not path.is_absolute() or not path.is_file() or path.is_symlink(): return None
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != self.profile.get('executable_sha256'): return None
            return REVIEWED_RUNTIMES.get((self.profile.get('agent_kind'),self.profile.get('version'),digest))
        except (KeyError, OSError): return None

    def readiness(self):
        missing = []
        if not self._runtime_policy(): missing.append('runtime_boundary_unreviewed')
        if not self._protocol_checked: missing.append('protocol_not_checked')
        policy = self._runtime_policy()
        if policy and policy.get('native') == 'opencode':
            if not self.profile.get('model'): missing.append('model')
            if self._authentication not in ('authenticated','not_required'): missing.append('authentication')
        if self.profile.get('authorized') is not True or self.profile.get('data_scope') != 'public_bundle_only': missing.append('usage_authorization')
        allocation = self.profile.get('allocation')
        if type(allocation) is not int or not 1 <= allocation <= self.budget.snapshot()['remaining']: missing.append('token_budget')
        maximum = self.profile.get('max_invocations')
        if type(maximum) is not int or maximum < 1 or self._used() >= maximum: missing.append('invocation_limit')
        return {'ready':not missing,'source':self.source,'live':True,'missing':missing,
                'protocol_checked':self._protocol_checked,'authentication':self._authentication,'task_verified':False,
                'model':self.profile.get('model'),'allocation_per_generation':allocation}

    def _used(self):
        approval = self.profile.get('approval_id', self.profile.get('entry_id'))
        used = 0
        for path in self.root.glob('acp-*/admission.json'):
            try:
                if json.loads(path.read_text(encoding='utf-8')).get('approval_id') == approval:
                    used += 1
            except (OSError, ValueError):
                return self.profile.get('max_invocations', 1)
        return used

    def _open(self, workspace):
        policy = self._runtime_policy()
        if not policy: _fail('acp_runtime_unreviewed')
        # Runtime-specific policy supplies only reviewed flags/environment. No
        # free-form command or environment from the UI crosses this boundary.
        env = (workbench_opencode.environment(workspace, self.root/'opencode-home', self.profile.get('model'),
               self.profile.get('allocation',500)) if policy.get('native') == 'opencode' else policy['environment'](workspace))
        return ACPTransport([self.profile['executable']] + policy['args'], workspace,
            timeout=self.profile.get('timeout_seconds',30), env=env)

    def _native_login(self, workspace):
        env = workbench_opencode.environment(workspace, self.root/'opencode-home', self.profile.get('model'))
        command = [self.profile['executable'],'auth','list']
        options = dict(cwd=str(workspace),env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE,shell=False,creationflags=0x08000000 if os.name == 'nt' else 0,
                       start_new_session=os.name != 'nt')
        tree = None
        process, tree = _spawn_in_job(command, **options) if os.name == 'nt' else (subprocess.Popen(command,**options),None)
        try:
            output, error = process.communicate(timeout=15)
            if process.returncode or len(output)+len(error) > MAX_LINE: _fail('acp_login_status')
            status = workbench_opencode.authentication_from_list((output+error).decode('utf-8','replace'),self.profile.get('model'))
        finally:
            if tree: tree.terminate(); tree.close()
            elif process.poll() is None: process.kill()
            process.wait(timeout=5)
        login_env = workbench_opencode.environment(self.root/'opencode-home'/'login',self.root/'opencode-home',login=True)
        return status, {key:login_env[key] for key in workbench_opencode.LOGIN_KEYS}

    def probe(self):
        if not self._runtime_policy():
            return {'status':'boundary_unreviewed','compatible':False,'protocol_checked':False,'authentication':'unknown','model_requests':0}
        with tempfile.TemporaryDirectory(prefix='xijian-acp-probe-') as directory:
            with self._open(Path(directory)) as client:
                value = client.initialize()
                self._protocol_checked = True
                result = {'status':'protocol_checked','compatible':True,'generation_supported':True,
                        'protocol_checked':True,'authentication':'unknown',
                        'agentInfo':value.get('agentInfo',{}),'protocolVersion':1,'model_requests':0}
                if self._runtime_policy().get('native') == 'opencode':
                    status, login_env = self._native_login(Path(directory))
                    model = self.profile.get('model','')
                    if model.startswith('opencode/') and status == 'unauthenticated':
                        session = client.request('session/new',{'cwd':directory,'mcpServers':[]})
                        for option in session.get('configOptions',[]):
                            if option.get('category') != 'model': continue
                            choices = option.get('options',[])
                            values = [choice for group in choices for choice in group.get('options',[group])]
                            if any(choice.get('value') == model for choice in values): status = 'not_required'
                    self._authentication = status
                    result.update(authentication=status,login_environment=login_env,login_args=['auth','login'],
                                  login_scope='dedicated_opencode_home',model=model)
                return result

    def operation(self): return self._operation

    def cancel(self):
        with self._lock: client, operation = self._client, self._operation
        if operation is None: return {'cancelled':False,'status':'idle'}
        self._cancelled.set()
        if client is not None: client.cancel()
        return {'cancelled':True,'status':'terminated','operation_id':operation}

    def call(self, public_bundle):
        with self._lock:
            if self._operation is not None: _fail('acp_busy')
            if not self.readiness()['ready']: _fail('acp_not_ready')
            raw = storage._bytes(public_bundle)
            if len(raw) > MAX_FILE: _fail('public_bundle_too_large')
            operation = 'acp-' + uuid.uuid4().hex
            self.budget.reserve(operation,self.profile['allocation'])
            self._operation = operation
            self._cancelled.clear()
        receipt = {'operation_id':operation,'status':'unknown','source':self.source,
                   'public_bundle_sha256':hashlib.sha256(raw).hexdigest(),'accounting':'conservative_allocation'}
        self.last_receipt = receipt
        folder = self.root/operation
        try:
            folder.mkdir(parents=True)
            (folder/'admission.json').write_bytes(storage._bytes({
                'approval_id':self.profile.get('approval_id', self.profile.get('entry_id')),
                'operation_id':operation}))
            with tempfile.TemporaryDirectory(prefix='xijian-acp-public-') as directory:
                workspace = Path(directory)
                (workspace/'public-bundle.json').write_bytes(raw)
                with self._open(workspace) as client:
                    with self._lock: self._client = client
                    if self._cancelled.is_set(): _fail('acp_cancelled')
                    client.initialize()
                    session = client.request('session/new',{'cwd':str(workspace),'mcpServers':[]})
                    if not isinstance(session.get('sessionId'),str) or not session['sessionId']: _fail('acp_session')
                    client.session_id = session['sessionId']
                    if self.profile.get('model'):
                        client.request('session/set_model',{'sessionId':client.session_id,'modelId':self.profile['model']})
                    text_mode = self._runtime_policy().get('candidate_mode') == 'text'
                    prompt = ('Return only the complete unchanged UTF-8 Python program. No Markdown fences or commentary. '
                              'Do not use tools. The approved public bundle is:\n' + raw.decode('utf-8')) if text_mode else (
                              'Use only public-bundle.json. Deliver the unchanged UTF-8 program as analysis.py. Do not run commands or request additional tools.')
                    result = client.request('session/prompt',{'sessionId':client.session_id,'prompt':[
                        {'type':'text','text':prompt}]})
                    if result.get('stopReason') != 'end_turn': _fail('acp_incomplete')
                    candidate = client.candidate_text() if text_mode else client.client_request('fs/read_text_file',{'sessionId':client.session_id,'path':str(workspace/'analysis.py')})['content'].encode('utf-8')
                    if not candidate.strip(): _fail('acp_candidate_missing')
                    (folder/'analysis.py').write_bytes(candidate)
                    receipt.update(status='complete',candidate_sha256=hashlib.sha256(candidate).hexdigest())
                    return candidate
        except Exception as error:
            receipt.update(status='unresolved',error_code=getattr(error,'code','acp_unresolved'))
            raise
        finally:
            receipt['token_budget'] = self.budget.settle(operation)
            with self._lock: self._client = None; self._operation = None
            if folder.is_dir(): (folder/'receipt.json').write_bytes(storage._bytes(receipt))
