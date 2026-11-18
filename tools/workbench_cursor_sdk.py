"""One-call source-workbench bridge for the audited optional Cursor SDK."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

from tools import local_synthetic_workflow as storage
from tools.workbench_connectors import ConnectorError
from tools.workbench_qoder import _spawn_in_job

VERSION = '1.4.0'
NODE_SHA256 = 'd702daa815eabbff763c7d3808e235cf031cf2ca70211b56847bc72f51e240e5'
LOCK_SHA256 = 'c41c9ade7d4263933c3402ad05fedff0d634a1c43606e7c2ee4ff95507c7c71d'
INSTALL_SHA256 = '2f788188d9ebff48f28653ea12a4e044bd7b0482bd7a0ebaa692de0a95047658'
DISALLOWED = ['mcp', 'webFetch', 'webSearch', 'shell', 'task']
MAX_BYTES = 65536


def _require(value, code):
    if not value:
        raise ConnectorError(code, 'Cursor SDK generation could not be admitted or verified.')


def _digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.digest()


def _plain(path):
    _require(not path.is_symlink() and not getattr(path.lstat(), 'st_file_attributes', 0) & 0x400,
             'sdk_linked_path')


class CursorSDKAdapter:
    source = 'cursor_sdk'

    def __init__(self, root, budget, sdk_root, node, model, allocation):
        _require(type(allocation) is int and 1 <= allocation <= 100000,
                 'sdk_token_reservation')
        _require(type(model) is str and re.fullmatch('[a-z0-9][a-z0-9._-]{0,159}', model, re.I)
                 and model.lower() not in {'auto','default'}, 'sdk_model_required')
        self.root, self.sdk_root, self.node = map(Path, (root, sdk_root, node))
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile = {'model': model, 'allocation': allocation, 'version': VERSION,
                        'runtime_sha256': INSTALL_SHA256, 'account_scope': 'isolated_sdk_profile',
                        'data_scope': 'public_bundle_only', 'max_invocations': 1}
        self.budget = budget
        self._checked = False
        self._lock = threading.RLock()
        self._tree = None
        self._operation = None
        self._cancelled = False
        self.last_receipt = None

    def _verify_installation(self):
        for path in (self.sdk_root, self.node, self.sdk_root/'package-lock.json', self.sdk_root/'sdk-profile'):
            for part in (path,) + tuple(path.parents):
                _plain(part)
        _require(_digest(self.node).hex() == NODE_SHA256
                 and _digest(self.sdk_root/'package-lock.json').hex() == LOCK_SHA256,
                 'sdk_runtime_changed')
        modules = self.sdk_root/'node_modules'
        _plain(modules)
        files = []
        total_bytes = 0
        for folder, directories, names in os.walk(modules):
            current = Path(folder)
            for name in directories + names:
                _plain(current/name)
            for name in names:
                path = current/name
                total_bytes += path.stat().st_size
                _require(len(files) < 2500 and total_bytes <= 64 * 1024 * 1024,
                         'sdk_runtime_changed')
                files.append((path.relative_to(modules).as_posix(), path))
        tree = hashlib.sha256()
        for relative, path in sorted(files):
            tree.update(relative.encode('utf-8') + b'\0' + _digest(path))
        _require(tree.hexdigest() == INSTALL_SHA256, 'sdk_runtime_changed')

    def _environment(self):
        env = {key:os.environ[key] for key in ('SYSTEMROOT','WINDIR','SYSTEMDRIVE','COMSPEC','PATHEXT')
               if key in os.environ}
        for key, name in [('USERPROFILE','sdk-profile'),('HOME','sdk-profile'),
                          ('APPDATA','login-roaming'),('LOCALAPPDATA','login-local'),
                          ('TEMP','runtime-temp'),('TMP','runtime-temp')]:
            directory = self.sdk_root/name
            _plain(directory)
            env[key] = str(directory)
        env.update(PATH=str(Path(env['SYSTEMROOT'])/'System32'), NO_COLOR='1', DO_NOT_TRACK='1')
        return env

    def _worker(self, mode, payload=None):
        cmd = [str(self.node), str(Path(__file__).with_name('workbench_cursor_sdk.mjs').resolve()),
               str(self.sdk_root), mode]
        with tempfile.TemporaryDirectory(prefix='xijian-cursor-sdk-') as cwd:
            if mode == 'offline':
                (Path(cwd)/'sentinel.txt').write_text('synthetic-host-canary', encoding='utf-8')
            with self._lock:
                _require(not self._cancelled, 'sdk_cancelled')
                process, tree = _spawn_in_job(cmd, cwd=cwd, env=self._environment(),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=0x08000000, shell=False)
                self._tree = tree
            try:
                try:
                    raw, _ = process.communicate(storage._bytes(payload) if payload is not None else b'',
                                                 timeout=150 if mode == 'generate' else 35)
                except subprocess.TimeoutExpired:
                    tree.terminate()
                    process.communicate()
                    _require(False, 'sdk_deadline')
                _require(len(raw) <= 2 * MAX_BYTES, 'sdk_output_limit')
                result = json.loads(raw)
                _require(type(result) is dict, 'sdk_response_invalid')
                detail = result.get('error')
                if process.returncode:
                    code = detail if isinstance(detail, str) and re.fullmatch('sdk_[a-z_]+', detail) else 'sdk_worker_failed'
                    _require(False, code)
                return result
            finally:
                with self._lock:
                    self._tree = None
                    tree.close()

    def probe(self):
        self._checked = False
        self._verify_installation()
        health = self._worker('offline')
        _require(health == {'status':'ready','version':VERSION,'tools':[],'excluded':DISALLOWED,
                            'setting_sources':[],'virtual_entries':0,'host_file_unavailable':True},
                 'sdk_policy_not_verified')
        catalog = self._worker('catalog')
        _require(catalog.get('status') == 'ready' and self.profile['model'] in catalog.get('models', []),
                 'sdk_model_unavailable')
        prepared = self._worker('prepare', {'model': self.profile['model'],
            'store_root': str(self.root/'preflight-sdk-state')})
        _require(prepared == {'status':'ready','model':self.profile['model']}, 'sdk_preflight')
        self._checked = True
        return health

    def readiness(self):
        missing = []
        if not self._checked:
            missing.append('sdk_preflight')
        if (self.root/'submission.json').exists():
            missing.append('invocation_limit')
        if self.budget.snapshot()['remaining'] < self.profile['allocation']:
            missing.append('token_budget')
        return {'source':self.source,'ready':not missing,'live':True,'missing':missing,
                'model':self.profile['model']}

    def operation(self):
        return self._operation

    def cancel(self):
        with self._lock:
            if self._operation is None:
                return None
            self._cancelled = True
            if self._tree:
                self._tree.terminate()
            return {'operation_id':self._operation,'status':'cancel_requested'}

    def call(self, public_bundle):
        raw = storage._bytes(public_bundle)
        _require(0 < len(raw) <= MAX_BYTES, 'sdk_public_bundle')
        self._verify_installation()
        operation = 'cursor-sdk-tool-free-once'
        with self._lock:
            _require(self.readiness()['ready'] and self._operation is None, 'sdk_not_ready')
            with (self.root/'submission.json').open('xb') as stream:
                stream.write(storage._bytes({'operation_id':operation,'bundle_sha256':storage._hash(raw),
                                             'model':self.profile['model']}))
                stream.flush()
                os.fsync(stream.fileno())
            self.budget.reserve(operation, self.profile['allocation'])
            self._operation = operation
            self._cancelled = False

        receipt = self.last_receipt = {'source':self.source,'operation_id':operation,
            'public_bundle_sha256':storage._hash(raw),'status':'unknown','version':VERSION,
            'offered_tools':[],'virtual_filesystem':'empty','setting_sources':[],'automatic_retry':False}
        observed = None
        candidate_file = self.root/'original.py'
        try:
            result = self._worker('generate', {'model':self.profile['model'], 'public_bundle':raw.decode('utf-8'),
                                               'store_root':str(self.root/'sdk-state'),
                                               'send_marker':str(self.root/'provider-send-started.json'),
                                               'candidate_file':str(candidate_file)})
            _require(not self._cancelled and result.get('model') == self.profile['model']
                     and type(result.get('tool_events')) is int and result['tool_events'] == 0
                     and type(result.get('reported_empty_tools')) is bool,
                     'sdk_generation_unverified')
            candidate = base64.b64decode(result['candidate_b64'], validate=True)
            _require(0 < len(candidate) <= MAX_BYTES and storage._hash(candidate) == result['candidate_sha256'],
                     'sdk_candidate_changed')
            _require(storage._read(candidate_file, MAX_BYTES) == candidate, 'sdk_candidate_changed')
            candidate.decode('utf-8')
            observed = result.get('observed_tokens')
            if type(observed) is not int or observed <= 0:
                observed = None
            receipt.update(status='complete', model=result['model'], tool_events=0,
                reported_empty_tools=result['reported_empty_tools'], candidate_sha256=result['candidate_sha256'],
                request_id=result.get('request_id'), observed_tokens=observed)
            return candidate
        finally:
            try:
                receipt['token_budget'] = self.budget.settle(operation, observed)
            except Exception:
                # A durable reservation still prevents replay; keep the exact candidate.
                receipt['budget_settlement'] = 'unconfirmed'
            finally:
                with self._lock:
                    self._operation = None
