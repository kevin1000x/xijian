"""Bounded external Qoder SDK adapter; only approved public bundles enter its cwd."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import uuid

from tools.workbench_connectors import ConnectorError
from tools import local_synthetic_workflow as storage

# Prospective conservative allocation, including SDK recovery uncertainty.
# This is a ledger admission allocation, not measured provider tokens or wire inventory.
SESSION_ALLOCATION = 3_200_000
REVIEWED_RUNTIME_SHA256 = 'fab07252a1b9b82f02cd91e528600dafe6f3ddff13efe0886e482d21c2579012'

class _ProcessTree:
    """Windows job is attached before stdin releases bridge code to start SDK."""
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes as w
        if os.name!='nt':
            raise OSError('Windows process tree enforcement required')
        class BASIC(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),
                ('flags',w.DWORD),('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),
                ('active_limit',w.DWORD),('affinity',ctypes.c_size_t),
                ('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(name,ctypes.c_ulonglong) for name in
                ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class EXTENDED(ctypes.Structure):
            _fields_=[('basic',BASIC),('io',IO),('process_memory',ctypes.c_size_t),
                ('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];k.CreateJobObjectW.restype=w.HANDLE
        k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        k.TerminateJobObject.argtypes=[w.HANDLE,w.UINT]
        k.CloseHandle.argtypes=[w.HANDLE]
        self.kernel=k
        self.handle=k.CreateJobObjectW(None,None)
        info=EXTENDED();info.basic.flags=0x2000
        if not self.handle or not k.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not k.AssignProcessToJobObject(self.handle,int(process._handle)):
            if self.handle:k.CloseHandle(self.handle)
            self.handle=None
            raise OSError('Process tree enforcement unavailable')
    def terminate(self):
        if self.handle:self.kernel.TerminateJobObject(self.handle,1)
    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle);self.handle=None

class QoderSDKAdapter:
    source = 'qoder'

    def __init__(self, root, budget, profile, token=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget = budget
        self.profile = copy.deepcopy(profile)
        self.profile.setdefault('auth_mode', 'local_login')
        self.profile.setdefault('model', 'gfmodel')
        self.profile['configuration_id']=uuid.uuid4().hex
        self._token = token
        self._ready = False
        self._lock = threading.Lock()
        self._process = None
        self._tree = None
        self._operation = None
        self._cancelled = False
        self.last_receipt = None
        self.paths = self._discover()

    @staticmethod
    def _discover():
        base = Path(os.environ.get('LOCALAPPDATA', ''))/'Programs'/'Qoder CN'/'.qoder-versions'
        for folder in sorted(base.glob('*'), reverse=True):
            resources = folder/'resources'
            paths = {'electron': folder/'Qoder CN.exe',
                     'sdk': resources/'app.asar/node_modules/@qoder-ai/qoder-cn-agent-sdk/dist/index.js',
                     'runtime': resources/'app.asar.unpacked/node_modules/@qoder-ai/qoder-cn-agent-sdk/dist/_worker/qoder-worker-runtime.obf.mjs'}
            # The SDK path is inside an Electron ASAR archive, not a native file.
            if paths['electron'].is_file() and paths['runtime'].is_file() and (resources/'app.asar').is_file():
                return {k: str(v) for k,v in paths.items()}
        return None

    def _runtime_digest(self):
        try:
            return hashlib.sha256(Path(self.paths['runtime']).read_bytes()).hexdigest()
        except (OSError, TypeError, KeyError):
            return None

    def admission_contract(self):
        # Fail closed on unreviewed vendor updates. No supplier files are modified.
        reviewed = self._runtime_digest() == REVIEWED_RUNTIME_SHA256
        return {'policy': 'qoder-sdk-conservative-v2', 'runtime_reviewed': reviewed,
                'runtime_sha256': REVIEWED_RUNTIME_SHA256 if reviewed else None,
                'allocation': SESSION_ALLOCATION, 'sdk_queries': 1,
                'max_turns': 4, 'context_window': 200000, 'max_output_tokens': 8192,
                'model_request_retries': 0, 'public_bytes': 65536,
                'wire_inventory_verified': False, 'actual_usage_known': False,
                'coverage': 'sdk_limits_conservative_not_wire_verified'}

    def operation(self):
        with self._lock:
            return self._operation

    def readiness(self):
        missing=[]
        if not self.paths: missing.append('installed_qoder_cn_sdk')
        if self.profile['auth_mode'] not in ('local_login','pat'): missing.append('auth_mode')
        if self.profile['auth_mode']=='pat' and (not isinstance(self._token,str) or not self._token): missing.append('credential')
        if not self._ready: missing.append('authenticated_model_catalog')
        if self.budget.snapshot()['remaining'] < SESSION_ALLOCATION: missing.append('token_budget')
        return {'ready':not missing,'live':True,'source':self.source,'missing':missing,
                'model':self.profile['model'],'allocation_per_generation':SESSION_ALLOCATION,
                'budget_coverage':'sdk_limits_conservative_not_wire_verified','actual_usage_known':False,
                'token_budget':self.budget.snapshot()}

    @staticmethod
    def _safe_observations(value):
        """Keep bounded structural evidence, never arbitrary worker text."""
        clean={'events':[]}
        if not isinstance(value,dict): return clean
        for event in value.get('events',[])[:128] if isinstance(value.get('events'),list) else []:
            if not isinstance(event,dict):continue
            if event.get('kind')=='model_request_admitted':
                ordinal=event.get('ordinal')
                if type(ordinal) is int and 1<=ordinal<=4:
                    clean['events'].append({'kind':'model_request_admitted','ordinal':ordinal,
                        'purpose':'main','context_window':200000})
            elif event.get('kind')=='tool_boundary':
                clean['events'].append({'kind':'tool_boundary',
                    'tool':event.get('tool') if event.get('tool') in ('Read','Write') else 'other',
                    'allowed':event.get('allowed') is True})
        subtype=value.get('result_subtype')
        if subtype in ('success','error_during_execution','error_max_turns','error_max_budget_usd','error_max_structured_output_retries'):
            clean['result_subtype']=subtype
        return clean

    def _invoke(self, mode, workspace, operation_id, timeout=120):
        request={'mode':mode, 'workspace':str(workspace), 'auth_mode':self.profile['auth_mode'],
                 'model':self.profile['model'], 'sdk':self.paths['sdk'], 'runtime':self.paths['runtime']}
        if self.profile['auth_mode']=='pat': request['token']=self._token
        # Deliberately exclude unrelated provider credentials and runtime preload vars.
        names=('PATH','SystemRoot','WINDIR','USERPROFILE','LOCALAPPDATA','APPDATA','TEMP','TMP','COMSPEC','PATHEXT')
        env={k:os.environ[k] for k in names if k in os.environ}
        env['ELECTRON_RUN_AS_NODE']='1'
        bridge=Path(__file__).with_name('workbench_qoder_bridge.cjs')
        with self._lock:
            if self._operation is not None:
                raise ConnectorError('operation_active','Qoder 已有请求运行。')
            self._cancelled=False
            self._operation=operation_id
            try:
                self._process=subprocess.Popen([self.paths['electron'],str(bridge)],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                    cwd=str(workspace),env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self._tree=_ProcessTree(self._process)
            except Exception:
                if self._process is not None:
                    self._process.kill();self._process.communicate()
                self._process=None
                self._operation=None
                raise ConnectorError('qoder_launch_failed','Qoder 启动失败，未自动重试。')
            process=self._process
            tree=self._tree
        try:
            try:
                raw,_=process.communicate(json.dumps(request,ensure_ascii=False).encode('utf-8'),timeout=timeout)
            except subprocess.TimeoutExpired:
                tree.terminate(); process.communicate()
                raise ConnectorError('qoder_timeout','Qoder 已超时并终止，未自动重试。')
            if self._cancelled:
                raise ConnectorError('qoder_cancelled','Qoder 已终止；本次预留按上限记账。')
            if process.returncode or len(raw)>262144:
                raise ConnectorError('qoder_unresolved','Qoder 未完整交付，未自动重试。')
            try: value=json.loads(raw)
            except (ValueError,UnicodeError):
                raise ConnectorError('qoder_invalid_receipt','Qoder 未返回有效交付记录。')
            observations=self._safe_observations(value)
            if self.last_receipt is not None and self.last_receipt.get('operation_id')==operation_id:
                self.last_receipt.update(observations)
            if not isinstance(value,dict) or value.get('status') != ('catalog' if mode=='catalog' else 'complete'):
                code='qoder_auth_required' if isinstance(value,dict) and value.get('code')=='AUTH_REQUIRED' else 'qoder_unresolved'
                raise ConnectorError(code,'Qoder 登录或本次交付未就绪；请查看连接状态。')
            return value
        finally:
            tree.terminate();tree.close()
            if process.poll() is None: process.kill(); process.wait()
            with self._lock:
                self._tree=None; self._process=None; self._operation=None

    def probe(self):
        self._ready=False
        if not self.paths: return {'status':'unavailable','missing':['installed_qoder_cn_sdk']}
        if self.profile['auth_mode'] not in ('pat','local_login'):
            return {'status':'unavailable','missing':['auth_mode']}
        workspace=Path(tempfile.mkdtemp(prefix='workbench-qoder-catalog-'))
        try:
            result=self._invoke('catalog',workspace,'catalog-'+uuid.uuid4().hex,timeout=30)
            choices=[m for m in result['models'] if m.get('isEnabled') is not False]
            self._ready=any(m.get('value')==self.profile['model'] and
                any(v.get('token_count')==200000 for v in m.get('context_config',{}).values()) for m in choices)
            return {'status':'ready' if self._ready else 'model_unavailable','model':self.profile['model'],
                    'models':[{'value':m['value'],'name':m.get('displayName',m['value'])} for m in choices],
                    'model_requests':0}
        finally:
            # Only remove an empty directory we created; preserve any unexpected bytes.
            try: workspace.rmdir()
            except OSError: pass

    def cancel(self):
        with self._lock:
            process=self._process; operation=self._operation
            if process is None or process.poll() is not None: return {'cancelled':False,'status':'idle'}
            self._cancelled=True
            self._tree.terminate(); process.wait(timeout=10)
            return {'cancelled':True,'status':'terminated','operation_id':operation}

    def call(self, public_bundle):
        if not self.readiness()['ready']:
            raise ConnectorError('qoder_not_ready','请先完成 Qoder 连接检查。')
        if not self.admission_contract()['runtime_reviewed']:
            raise ConnectorError('qoder_runtime_unreviewed','Qoder 运行时已改变，需核对新版本的预算控制后再生成。')
        raw=storage._bytes(public_bundle)
        if len(raw)>65536: raise ConnectorError('public_bundle_too_large','本次公开包超过 64 KB。')
        operation='qoder-'+uuid.uuid4().hex
        self.budget.reserve(operation,SESSION_ALLOCATION)
        receipt={'operation_id':operation,'configured_model':self.profile['model'],
                 'public_bundle_sha256':hashlib.sha256(raw).hexdigest(),'accounting':'conservative_allocation',
                 'reserved_tokens':SESSION_ALLOCATION,'admission_contract':self.admission_contract(),'status':'unknown'}
        self.last_receipt=receipt
        folder=self.root/operation
        try:
            folder.mkdir()
            workspace=Path(tempfile.mkdtemp(prefix='workbench-public-'))
            receipt['public_workspace']=str(workspace)
            (workspace/'public-bundle.json').write_bytes(raw)
            (folder/'public-bundle.json').write_bytes(raw)
            result=self._invoke('generate',workspace,operation)
            receipt.update(result)
            target=workspace/'analysis.py'
            if (not target.is_file() or target.is_symlink()
                or getattr(target.lstat(),'st_file_attributes',0)&0x400
                or target.stat().st_size>65536):
                raise ConnectorError('qoder_candidate_missing','Qoder 未交付限定大小的原始程序。')
            candidate=target.read_bytes()
            if not candidate.strip(): raise ConnectorError('qoder_candidate_empty','Qoder 交付了空程序。')
            (folder/'analysis.py').write_bytes(candidate)
            receipt.update(candidate_sha256=hashlib.sha256(candidate).hexdigest(),status='complete')
            return candidate
        except Exception as error:
            receipt['status']='unresolved'
            receipt['error_code']=getattr(error,'code','qoder_unresolved')
            raise
        finally:
            receipt['token_budget']=self.budget.settle(operation)
            if folder.is_dir(): (folder/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
