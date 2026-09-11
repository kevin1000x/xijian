"""Loopback-only synthetic analysis workbench. No model, network or shell tools."""
import argparse
import base64
import csv
import hashlib
import hmac
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from zipfile import ZipFile, BadZipFile

from runtime.analysis_runner import pd
from tools.workbench_desktop import VERSION as DESKTOP_VERSION

REPO = Path(__file__).resolve().parents[1]
MAX_INPUT = 2 * 1024 * 1024
MAX_BODY = 4 * 1024 * 1024
ASSETS = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}


class InputError(ValueError):
    def __init__(self, message, code='invalid_input'):
        super().__init__(message)
        self.code, self.message = code, message


def _fail(message, code='invalid_input'):
    raise InputError(message, code)


def _id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, AttributeError):
        _fail('记录编号无效。', 'invalid_id')


def _json(raw):
    def constant(_):
        _fail('JSON 中不能包含非有限数值。')
    return json.loads(raw, parse_constant=constant)


def _bytes(obj):
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _wire(value):
    # JSON/JavaScript cannot represent every Python integer. Preserve display digits.
    if type(value) is int and abs(value) > 9007199254740991:
        return str(value)
    if isinstance(value, dict):
        return {k: _wire(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_wire(v) for v in value]
    return value


def _atomic(path, obj):
    temporary = path.with_name('.tmp-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(_bytes(obj))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def parse_table(payload):
    if not isinstance(payload, dict):
        _fail('请选择文件或粘贴表格。')
    kind = payload.get('format', 'csv')
    if kind in ('csv', 'tsv'):
        text = payload.get('text')
        if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_INPUT:
            _fail('表格须为 UTF-8 文本，大小不超过 2 MiB。', 'input_size')
        try:
            rows = list(csv.reader(io.StringIO(text.lstrip('\ufeff')), delimiter='\t' if kind == 'tsv' else ',', strict=True))
        except csv.Error:
            _fail('无法读取表格：请检查分隔符、引号及每行的列数。')
    elif kind == 'xlsx':
        try:
            encoded = payload.get('base64')
            if not isinstance(encoded, str) or len(encoded) > MAX_BODY:
                _fail('Excel 文件大小不能超过 2 MiB。', 'input_size')
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > MAX_INPUT:
                _fail('Excel 文件大小不能超过 2 MiB。', 'input_size')
            with ZipFile(io.BytesIO(raw)) as archive:
                members = archive.infolist()
                if len(members) > 512 or sum(m.file_size for m in members) > 16 * 1024 * 1024:
                    _fail('Excel 文件解压后的内容过大。', 'input_size')
            import openpyxl
            book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
            try:
                sheets = [s for s in book.worksheets if s.sheet_state == 'visible']
                if not sheets:
                    _fail('Excel 文件没有可见工作表。')
                sheet = sheets[0]
                if (sheet.max_row or 0) > 4097 or (sheet.max_column or 0) > 32:
                    _fail('表格最多支持 4096 行数据、32 列。', 'input_size')
                rows = []
                for cells in sheet.iter_rows():
                    if any(c.data_type == 'f' for c in cells):
                        _fail('此版本不计算 Excel 公式。请在副本中将公式粘贴为值后导入。', 'excel_formula')
                    values = []
                    for cell in cells:
                        value = cell.value
                        if value is not None and type(value) not in (str, int, float):
                            _fail('此版本支持文本和数值，不支持日期或布尔单元格。', 'unsupported_type')
                        values.append('' if value is None else str(value))
                    rows.append(values)
                    if len(rows) > 4097:
                        _fail('表格最多支持 4096 行数据。', 'input_size')
            finally:
                book.close()
        except ImportError:
            _fail('当前环境无法读取 XLSX；可直接复制 Excel 区域并粘贴。', 'xlsx_unavailable')
        except (BadZipFile, OSError, ValueError, KeyError) as error:
            if isinstance(error, InputError):
                raise
            _fail('无法读取 XLSX 文件；可尝试复制单元格并粘贴。', 'invalid_xlsx')
    else:
        _fail('支持 CSV、TSV、XLSX 或从 Excel 复制的表格。', 'unsupported_format')
    rows = [row for row in rows if row]
    while rows and not any(str(v).strip() for v in rows[-1]):
        rows.pop()
    if len(rows) < 2 or len(rows) > 4097:
        _fail('请提供表头和 1～4096 行数据。', 'row_count')
    header = rows[0]
    if not 1 <= len(header) <= 32 or len(set(header)) != len(header) or any(not n.strip() or len(n) > 128 for n in header):
        _fail('列名应非空、不重复，最多 32 列，每个列名不超过 128 字。', 'headers')
    data = rows[1:]
    if any(len(row) != len(header) for row in data):
        _fail('各行列数不同，请检查粘贴区域或 CSV 分隔符。', 'row_width')
    columns = {}
    for index, name in enumerate(header):
        values = [str(row[index]) for row in data]
        if any(not v.strip() for v in values):
            _fail('列「%s」含空值；请先明确并填好这些值。此版本不自动删除缺失行。' % name, 'missing_values')
        if any(len(v) > 512 for v in values):
            _fail('单个单元格最多支持 512 字。', 'cell_size')
        integer = all(re.fullmatch(r'[+-]?\d+', v.strip()) for v in values)
        leading_zero = any(re.fullmatch(r'[+-]?0\d+', v.strip()) for v in values)
        if integer and not leading_zero:
            converted = [int(v) for v in values]
            if any(not -(2 ** 63) <= v < 2 ** 63 for v in converted):
                _fail('列「%s」含超出 int64 的单个整数。编号请使用文本格式。' % name, 'integer_range')
            columns[name] = pd.Series(converted, dtype='int64')
        else:
            try:
                converted = [float(v) for v in values]
                numeric = not leading_zero
            except ValueError:
                numeric = False
            if numeric:
                if not all(math.isfinite(v) for v in converted):
                    _fail('列「%s」含 NaN 或无穷数，请改为有限数值。' % name, 'nonfinite')
                columns[name] = pd.Series(converted, dtype='float64')
            else:
                columns[name] = pd.Series(values, dtype='object')
    return pd.DataFrame(columns)


def engine_run(frame, spec, out_dir):
    from tools.workbench_analysis import run_analysis
    return run_analysis(frame, spec, out_dir)


class Store:
    def __init__(self, root, *, agent_internal=None, agent_external=None, agent_execution_authorization=None, budget_root=None):
        self.root = Path(root).resolve()
        for name in ('plans', 'runs', 'metadata'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.datasets, self.confirmations = {}, {}
        self.mcp_sessions={}
        self.internal_configuration={'mode':'unconfigured'}
        self.external_configuration={'mode':'unconfigured'}
        from tools.workbench_budget import TokenBudget
        self.token_budget=TokenBudget(budget_root or self.root/'token-budget')

        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        from tools.workbench_agent import AgentWorkbench
        self.agent = AgentWorkbench(self.root / 'agent', internal=agent_internal,
            external=agent_external, execution_authorization=agent_execution_authorization)
        from tools.workbench_lifecycle import TaskLifecycle
        self.lifecycle=TaskLifecycle(self.agent)
        self.agent.lifecycle=self.lifecycle
        self.lifecycle.budget=lambda:self.token_budget
        self.host_seen={}
        self.registration_root=self.root/'agent'/'registrations'
        self.registration_root.mkdir(exist_ok=True)
        from tools.workbench_lifecycle import StateLock
        self.registration_lock=StateLock(self.registration_root)
        self.input_root=self.root/'admitted-inputs'
        self.input_root.mkdir(exist_ok=True)
        identity=self.root/'state-identity.json'
        try:
            with identity.open('xb') as stream:
                stream.write(_bytes({'state_id':str(uuid.uuid4())}));stream.flush();os.fsync(stream.fileno())
        except FileExistsError: pass
        self.state_id=_json(identity.read_bytes())['state_id']
        configuration=self.registration_root/'configuration.json'
        if configuration.exists():
            saved=_json(configuration.read_bytes())
            self.internal_configuration=saved['configuration']
            self.agent.enterprise_actor=saved['actor']


    def import_table(self, payload):
        frame = parse_table(payload)
        name = payload.get('name', '粘贴的表格')
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            _fail('请输入不超过 200 字的数据名称。')
        key = str(uuid.uuid4())
        with self.lock:
            while len(self.datasets) >= 6:
                self.datasets.pop(next(iter(self.datasets)))
            self.datasets[key] = (name, frame)
        columns = [{'name': n, 'type': 'string' if str(frame[n].dtype) == 'object' else 'integer' if str(frame[n].dtype) == 'int64' else 'number'} for n in frame]
        warnings = ['只读取第一个可见工作表；公式和日期不会自动转换。'] if payload.get('format') == 'xlsx' else []
        return dict(dataset_id=key, name=name, row_count=len(frame), columns=columns,
                    preview=frame.head(30).to_dict(orient='records'), warnings=warnings)

    def _dataset(self, key):
        _id(key)
        if key not in self.datasets:
            _fail('这份数据已不在当前会话中，请重新导入。保存的分析方案仍可复用。', 'dataset_missing')
        return self.datasets[key]

    def preview(self, payload):
        from tools.workbench_analysis import validate_spec, describe_spec
        with self.lock:
            _, frame = self._dataset(payload.get('dataset_id'))
            spec = validate_spec(frame, payload.get('spec'))
            token = secrets.token_urlsafe(24)
            while len(self.confirmations) >= 20:
                self.confirmations.pop(next(iter(self.confirmations)))
            self.confirmations[token] = (payload['dataset_id'], hashlib.sha256(_bytes(spec)).hexdigest())
            return dict(spec=spec, summary=describe_spec(frame, spec), confirmation=token)

    def run(self, payload):
        from tools.workbench_analysis import validate_spec
        with self.lock:
            name, frame = self._dataset(payload.get('dataset_id'))
            spec = validate_spec(frame, payload.get('spec'))
            signature = (payload['dataset_id'], hashlib.sha256(_bytes(spec)).hexdigest())
            token = payload.get('confirmation')
            if not isinstance(token, str) or self.confirmations.get(token) != signature:
                _fail('数据或分析口径发生变化，请重新预览并确认后执行。', 'confirmation_required')
            del self.confirmations[token]
            key = str(uuid.uuid4())
            meta = dict(id=key, name=name, spec=spec, created_at=time.strftime('%Y-%m-%d %H:%M:%S'), status='STARTED')
            _atomic(self.root / 'metadata' / (key + '.json'), meta)
            result = engine_run(frame.copy(deep=True), spec, self.root / 'runs' / key)
            meta['status'] = result.get('status')
            _atomic(self.root / 'metadata' / (key + '.json'), meta)
            return result

    def configure_agent(self,payload):
        from tools.workbench_connectors import InternalHTTPAdapter,PROTOCOLS
        from tools.workbench_agent import digest
        mode=payload.get('mode')
        if payload.get('authorized') is not True:
            _fail('请明确确认本次连接路线及合成数据范围。','configuration_authorization_required')
        if mode=='qoder_sdk':
            with self.agent.lock:
                if getattr(self.agent.external,'operation',lambda:None)():
                    _fail('External operation is active.','operation_active')
            from tools.workbench_qoder import QoderSDKAdapter
            profile={k:payload[k] for k in ('auth_mode','model') if k in payload}
            connector=QoderSDKAdapter(self.root/'qoder',self.token_budget,profile,
                                      token=payload.get('credential'))
            probe=connector.probe()
            readiness=connector.readiness()
            execution_ready=readiness.get('ready') is True
            if readiness.get('missing')==['token_budget']:
                contract=getattr(connector,'admission_contract',lambda:{})()
                execution_ready=(contract.get('runtime_reviewed') is True
                                 and (probe.get('status')=='ready' or probe.get('catalog_verified') is True))
            with self.agent.lock:
                if getattr(self.agent.external,'operation',lambda:None)():
                    _fail('External operation is active.','operation_active')
                self.agent.external=connector
                self.agent.execution_authorization=({'authorized':True,'data_scope':'synthetic_only',
                    'risk_acknowledgement':'existing_supervisor_not_hostile_isolation'}
                    if execution_ready else None)
            self.lifecycle.resume_safe()
            return {'mode':mode,'readiness':readiness,'probe':probe}
        with self.agent.lock:
            active=getattr(self.agent.external if mode=='external_http' else self.agent.internal,'operation',lambda:None)()
            if active:
                _fail('内侧请求仍在运行，请先结束该操作。','operation_active')
            if mode in {'http','external_http'}:
                protocol=payload.get('protocol')
                if protocol not in PROTOCOLS:
                    _fail('请选择 Responses、Chat 或 Messages 协议。','unsupported_protocol')
                credential=payload.get('credential')
                if type(credential) is not str or not 1<=len(credential)<=8192 or '\n' in credential or '\r' in credential:
                    _fail('请提供本次内侧路线的有效凭据。','credential_required')
                aliases=payload.get('model_aliases',[])
                if type(aliases) is not list or len(aliases)>20 or not all(type(a) is str and 1<=len(a)<=200 for a in aliases):
                    _fail('模型别名列表无效。','invalid_model_aliases')
                profile={'protocol':protocol,'url':payload.get('url'),'model':payload.get('model'),
                    'model_aliases':aliases,'route_id':payload.get('url'),'account_id':'operator-configured',
                    'approval_id':str(uuid.uuid4()),'authorized':True,'data_scope':('public_bundle_only' if mode=='external_http' else 'synthetic_internal_only'),
                    'max_invocations':100,'max_output_tokens':payload.get('max_output_tokens',4096),
                    'timeout_seconds':payload.get('timeout_seconds',60),'max_response_bytes':1024*1024,
                    'max_total_tokens':10_000_000}
                if 'chat_cap_field' in payload: profile['chat_cap_field']=payload['chat_cap_field']
                pinned=digest(profile)
                def boundary(current):
                    if digest(current)!=pinned: return {}
                    return {'profile_sha256':pinned,'launch_boundary':True,'budget_enforced':True,'data_boundary':True}
                adapter=InternalHTTPAdapter
                if mode=='external_http':
                    from tools.workbench_connectors import ExternalHTTPAdapter
                    adapter=ExternalHTTPAdapter
                connector=adapter(profile,token=credential,verifier=boundary,budget=self.token_budget)
                state=connector.readiness()
                if not state['ready']:
                    _fail('内侧配置不完整：'+', '.join(state['missing']),'invalid_connector_configuration')
                if mode=='external_http':
                    self.agent.external=connector
                    self.agent.execution_authorization={'authorized':True,'data_scope':'synthetic_only',
                        'risk_acknowledgement':'existing_supervisor_not_hostile_isolation'}
                    self.external_configuration={'mode':mode,'protocol':protocol,'url':profile['url'],
                        'model':profile['model'],'model_aliases':aliases}
                    return dict(self.external_configuration,readiness=self.agent.readiness(),token_budget=self.token_budget.snapshot())
                self.agent.internal=connector
                self.agent.enterprise_actor=None
                self.internal_configuration={'mode':mode,'protocol':protocol,'url':profile['url'],
                                             'model':profile['model'],'model_aliases':aliases}
            elif mode=='enterprise_mcp':
                actor=payload.get('actor_id')
                reservation=payload.get('host_token_reservation',100000)
                if (type(actor) is not str or not 1<=len(actor)<=200 or
                    payload.get('host_budget_enforced') is not True or
                    type(reservation) is not int or not 1<=reservation<=10_000_000):
                    _fail('企业 Agent 需要身份、明确的宿主预算控制确认和 Token 预留。','enterprise_scope_required')
                profile={'actor_id':actor,'approval_id':str(uuid.uuid4()),'data_scope':'synthetic_internal_only',
                         'host_budget_enforced':True,'host_token_reservation':reservation}
                self.agent.internal=None
                self.agent.enterprise_actor={'source':'enterprise_mcp','profile_sha256':digest(profile)}
                self.internal_configuration={'mode':mode,**profile}
            else:
                _fail('不支持的内侧连接方式。','unsupported_connector_mode')
            # Reconfiguration invalidates prior internal session capabilities.
            self.mcp_sessions.clear()
            if (self.registration_root/'configuration.json').exists():
                _atomic(self.registration_root/'configuration.json',
                    {'configuration':self.internal_configuration,'actor':self.agent.enterprise_actor})
            self.lifecycle.resume_safe()
            return dict(self.internal_configuration,readiness=self.agent.readiness(),
                        token_budget=self.token_budget.snapshot())

    def budget_view(self):
        value=self.token_budget.snapshot()
        credits={}
        conflicts=set()
        for path in (self.root/'agent'/'tasks').glob('*/record.json'):
            record=_json(path.read_bytes())
            for field in ('generation_receipt','internal_receipt','interpretation_receipt'):
                receipt=record.get(field) or {}
                op=receipt.get('operation_id'); amount=receipt.get('total_credits')
                if type(op) is str and type(amount) in (int,float) and math.isfinite(amount) and amount>=0:
                    if op in credits and credits[op]!=amount: conflicts.add(op)
                    credits[op]=amount
        observed=[amount for op,amount in credits.items() if op not in conflicts]
        value.update(credits=sum(observed) if observed else None,credit_observations=len(observed),
                     credit_conflicts=len(conflicts),credit_basis='sdk_reported_not_tokens')
        return value

    def host_status(self):
        registrations=[]
        for path in self.registration_root.glob('*.json'):
            if path.name=='configuration.json': continue
            value=_json(path.read_bytes())
            seen=self.host_seen.get(value['id'],0)
            registrations.append({'registration_id':value['id'],'dataset_ids':value['dataset_ids'],
                'connected':bool(seen and time.time()-seen<90 and not value['revoked'] and value['actor']==self.agent.enterprise_actor),
                'last_seen':seen or None,'revoked':value['revoked']})
        last=max(self.host_seen.values(),default=0)
        claims=[p.stem for p in self.lifecycle.root.glob('*.json')
                if self.lifecycle.view(p.stem).get('owner') and not self.lifecycle.view(p.stem)['owner_expired']]
        return {'configured':self.agent.enterprise_actor is not None,
                'connected':any(r['connected'] for r in registrations),'last_seen':last or None,
                'claimed_tasks':claims,'registrations':registrations}

    def _registration(self,key):
        path=self.registration_root/(_id(key)+'.json')
        if not path.exists(): _fail('Host registration not found.','mcp_session_rejected')
        return _json(path.read_bytes())

    def _admit_inputs(self,ids):
        if type(ids) is not list or len(ids)>6:
            _fail('Select up to six explicitly authorized inputs.','input_scope_required')
        for key in ids:
            name,frame=self._dataset(key)
            _atomic(self.input_root/(_id(key)+'.json'),{'name':name,'columns':list(frame),
                'records':frame.to_dict('records'),'dtypes':{n:str(frame[n].dtype) for n in frame}})
        return list(dict.fromkeys(ids))

    def _admitted_frame(self,key):
        value=_json((self.input_root/(_id(key)+'.json')).read_bytes())
        return value['name'],pd.DataFrame(value['records']).loc[:,value['columns']].astype(value['dtypes'])

    def register_host(self,payload):
        from tools.workbench_mcp import protect_token
        if not self.agent.enterprise_actor:
            _fail('Configure an approved internal host first.','enterprise_scope_required')
        with self.registration_lock:
            ids=self._admit_inputs(payload.get('dataset_ids',[]))
            key=str(uuid.uuid4()); secret=secrets.token_urlsafe(32)
            directory=self.root/'agent'/'mcp-credentials'; directory.mkdir(exist_ok=True)
            path=directory/(key+'.bin')
            with path.open('xb') as stream:
                stream.write(protect_token(secret.encode('ascii')));stream.flush();os.fsync(stream.fileno())
            record={'id':key,'actor':self.agent.enterprise_actor,'dataset_ids':ids,'revoked':False,
                'secret_sha256':hashlib.sha256(secret.encode('ascii')).hexdigest(),'selected_task':None,
                'reservation':self.internal_configuration['host_token_reservation']}
            _atomic(self.registration_root/(key+'.json'),record)
            _atomic(self.registration_root/'configuration.json',
                {'configuration':self.internal_configuration,'actor':self.agent.enterprise_actor})
        import sys
        return {'registration_id':key,'session_id':key,'credential_file':str(path),
            'client_config':{'mcpServers':{'local-workbench-internal':{
                'command':sys.executable,'args':['-I','-B',str(REPO/'tools'/'workbench_mcp.py'),
                    '--state-file',str(self.root/'server.json'),'--state-id',self.state_id,
                    '--session',key,'--credential-file',str(path)]}}},
            'dataset_ids':ids,'budget_notice':'Each delivered context is charged its conservative host allocation; host model traffic remains operator controlled.'}

    def admit_host(self,payload):
        with self.registration_lock:
            value=self._registration(payload.get('registration_id'))
            if value['revoked'] or value['actor']!=self.agent.enterprise_actor:
                _fail('Host registration is no longer authorized.','mcp_session_rejected')
            value['dataset_ids']=self._admit_inputs(payload.get('dataset_ids',[]))
            _atomic(self.registration_root/(value['id']+'.json'),value)
        return {'registration_id':value['id'],'dataset_ids':value['dataset_ids']}

    def revoke_host(self,payload):
        with self.registration_lock:
            value=self._registration(payload.get('registration_id'));value['revoked']=True
            _atomic(self.registration_root/(value['id']+'.json'),value)
            self.host_seen.pop(value['id'],None)
        return {'registration_id':value['id'],'revoked':True}

    def _task_input_path(self,key):
        directory=self.root/'agent'/'task-inputs';directory.mkdir(exist_ok=True)
        return directory/(_id(key)+'.json')

    def _authorized_task(self,key,registration):
        record=self.agent._load(_id(key))
        path=self._task_input_path(key)
        if not path.exists() and record.get('parent_id'):
            path=self._task_input_path(record['parent_id'])
        if (record['internal_route']!=registration['actor'] or not path.exists() or
            _json(path.read_bytes())['dataset_id'] not in registration['dataset_ids']):
            _fail('Task/input is not authorized for this connection.','mcp_task_scope_rejected')
        return record

    def _registered_action(self,name,payload,registration):
        from tools.workbench_mcp import TOOLS
        tool=next((t for t in TOOLS if t['name']==name),None)
        if (tool is None or type(payload) is not dict or set(payload)-set(tool['inputSchema']['properties'])
                or set(tool['inputSchema']['required'])-set(payload)):
            _fail('Unsupported tool or fields.','mcp_tool_rejected')
        sid=registration['id']; actor=registration['actor']
        self.host_seen[sid]=time.time()
        if name=='heartbeat':
            return {'connected':True,'last_seen':self.host_seen[sid],'registration_id':sid}
        if name=='list_authorized_inputs':
            return {'inputs':[{'dataset_id':key,'name':self._admitted_frame(key)[0]}
                              for key in registration['dataset_ids']]}
        if name=='list_tasks':
            rows=[]
            for item in self.agent.history():
                try: self._authorized_task(item['id'],registration)
                except Exception: continue
                rows.append(dict(item,lifecycle=self.lifecycle.view(item['id'])))
            return {'tasks':rows}
        if name=='create_task':
            dataset=payload.get('dataset_id')
            if dataset not in registration['dataset_ids']:
                _fail('Input was not admitted through the GUI.','mcp_input_scope_rejected')
            _,frame=self._admitted_frame(dataset)
            result=self.agent.draft(frame,payload.get('task'))
            _atomic(self._task_input_path(result['id']),{'dataset_id':dataset})
            self.lifecycle.claim(result['id'],sid)
            with self.registration_lock:
                latest=self._registration(sid);latest['selected_task']=result['id']
                _atomic(self.registration_root/(sid+'.json'),latest)
            return self.agent.get(result['id'])
        key=payload.get('proposal_id') or registration.get('selected_task')
        self._authorized_task(key,registration)
        if name=='claim_task':
            self.lifecycle.claim(key,sid)
            with self.registration_lock:
                latest=self._registration(sid);latest['selected_task']=key
                _atomic(self.registration_root/(sid+'.json'),latest)
            return self.agent.get(key)
        self.lifecycle.require_claim(key,sid)
        if name=='cancel_task': return self.lifecycle.cancel(key)
        if name=='get_task_status': return self.agent.get(key)
        self.lifecycle.guard(key)
        if name in {'get_task_context','get_result_context'}:
            context=(self.agent.enterprise_context(key,actor) if name=='get_task_context'
                     else self.agent.enterprise_result_context(key,actor))
            op='mcp-'+sid+':'+uuid.uuid4().hex
            self.token_budget.reserve(op,registration['reservation'])
            self.token_budget.settle(op,None)
            return context
        if name=='submit_proposal':
            result=self.agent.enterprise_accept(key,actor,payload['context_sha256'],payload['proposal'])
        elif name=='generate_program': result=self.agent.generate(key,payload['bundle_sha256'])
        elif name=='execute_program': result=self.agent.run(key,payload['candidate_sha256'])
        elif name=='test_public_program': result=self.agent.public_test(key,payload['candidate_sha256'])
        elif name=='prepare_repair': result=self.agent.repair(key,payload['candidate_sha256'])
        elif name=='reuse_program':
            dataset=payload.get('dataset_id')
            if dataset not in registration['dataset_ids']:
                _fail('Select an explicitly admitted new input.','mcp_input_scope_rejected')
            _,frame=self._admitted_frame(dataset)
            result=self.agent.reuse(key,frame)
            _atomic(self._task_input_path(result['id']),{'dataset_id':dataset})
        elif name=='submit_interpretation':
            result=self.agent.enterprise_interpret(key,actor,payload['run_id'],payload['result_sha256'],payload['text'])
        else: _fail('Unsupported MCP action.','mcp_tool_rejected')
        if result.get('proposal_id') and result['proposal_id']!=key:
            newkey=result['proposal_id']
            if not self._task_input_path(newkey).exists():
                _atomic(self._task_input_path(newkey),_json(self._task_input_path(key).read_bytes()))
            self.lifecycle.claim(newkey,sid)
            with self.registration_lock:
                latest=self._registration(sid);latest['selected_task']=newkey
                _atomic(self.registration_root/(sid+'.json'),latest)
        return result

    def create_mcp_session(self,payload):
        from tools.workbench_mcp import protect_token
        if not self.agent.enterprise_actor or self.internal_configuration.get('host_budget_enforced') is not True:
            _fail('请先配置企业内部 Agent 和宿主预算范围。','enterprise_scope_required')
        key=_id(payload.get('proposal_id'))
        dataset_id=_id(payload.get('dataset_id'))
        with self.lock:
            self._dataset(dataset_id)
            record=self.agent.get(key)
            if record['internal_route']!=self.agent.enterprise_actor:
                _fail('该任务不属于当前企业 Agent。','enterprise_scope_required')
            session_id=str(uuid.uuid4())
            secret=secrets.token_urlsafe(32)
            directory=self.root/'agent'/'mcp-credentials'
            directory.mkdir(parents=True,exist_ok=True)
            path=directory/(session_id+'.bin')
            with path.open('xb') as stream: stream.write(protect_token(secret.encode('ascii')))
            self.mcp_sessions[session_id]={'secret':secret,'proposal_id':key,'dataset_id':dataset_id,
                'actor':dict(self.agent.enterprise_actor),'expires':time.time()+3600,'allocations':set(),
                'reservation':self.internal_configuration['host_token_reservation'],'lock':threading.Lock()}
            import sys
            return {'session_id':session_id,'proposal_id':key,'dataset_id':dataset_id,
                'credential_file':str(path),'expires_in_seconds':3600,
                'host_token_reservation':self.internal_configuration['host_token_reservation'],
                'client_config':{'mcpServers':{'local-workbench-internal':{'command':sys.executable,
                    'args':['-I','-B',str(REPO/'tools'/'workbench_mcp.py'),'--server',self.server_url,
                            '--session',session_id,'--credential-file',str(path)]}}},
                'budget_notice':'宿主模型的 Token 控制由企业 Agent 负责；每次上下文分配全额记账，不接受自报用量退款。'}

    def mcp_action(self,name,payload,session_id,credential):
        from tools.workbench_mcp import TOOLS
        if type(session_id) is str:
            try: registered=self._registration(session_id)
            except (ValueError,OSError): registered=None
            if registered is not None:
                if (registered['revoked'] or registered['actor']!=self.agent.enterprise_actor or
                    type(credential) is not str or not credential.isascii() or
                    not hmac.compare_digest(hashlib.sha256(credential.encode('ascii')).hexdigest(),registered['secret_sha256'])):
                    _fail('Host registration rejected.','mcp_session_rejected')
                return self._registered_action(name,payload,registered)
        with self.lock:
            session=self.mcp_sessions.get(session_id)
        if (session is None or type(credential) is not str or not credential.isascii() or
            not hmac.compare_digest(credential,session['secret']) or time.time()>=session['expires'] or
            session['actor']!=self.agent.enterprise_actor):
            _fail('企业 MCP 会话无效、过期或范围已改变。','mcp_session_rejected')
        tool=next((t for t in TOOLS if t['name']==name),None)
        if (tool is None or type(payload) is not dict or set(payload)-set(tool['inputSchema']['properties'])
            or set(tool['inputSchema']['required'])-set(payload)):
            _fail('该会话不允许此操作或参数。','mcp_tool_rejected')
        if ('proposal_id' in payload and payload['proposal_id']!=session['proposal_id'] or
            'dataset_id' in payload and payload['dataset_id']!=session['dataset_id']):
            _fail('Legacy session is bound to one exact task/input.','mcp_task_scope_rejected')
        if name=='cancel_task': return self.lifecycle.cancel(session['proposal_id'])
        if name=='get_task_status': return self.agent.get(session['proposal_id'])
        with session['lock']:
            key=session['proposal_id']; actor=session['actor']
            self.lifecycle.guard(key)
            if name in {'get_task_context','get_result_context'}:
                context=(self.agent.enterprise_context(key,actor) if name=='get_task_context'
                         else self.agent.enterprise_result_context(key,actor))
                allocation=key+':'+name
                if allocation not in session['allocations']:
                    op='mcp-'+session_id+':'+name+':'+key
                    self.token_budget.reserve(op,session['reservation'])
                    self.token_budget.settle(op,None)
                    session['allocations'].add(allocation)
                return context
            if name=='submit_proposal':
                return self.agent.enterprise_accept(key,actor,payload['context_sha256'],payload['proposal'])
            if name=='get_task_status': return self.agent.get(key)
            if name=='generate_program': return self.agent.generate(key,payload['bundle_sha256'])
            if name=='execute_program': return self.agent.run(key,payload['candidate_sha256'])
            if name=='test_public_program': return self.agent.public_test(key,payload['candidate_sha256'])
            if name=='prepare_repair':
                self.agent._enterprise_record(key,actor)
                result=self.agent.repair(key,payload['candidate_sha256'])
                session['proposal_id']=result['proposal_id']
                return result
            if name=='reuse_program':
                with self.lock: _,frame=self._dataset(session['dataset_id'])
                result=self.agent.reuse(key,frame.copy(deep=True))
                session['proposal_id']=result['proposal_id']
                return result
            if name=='submit_interpretation':
                return self.agent.enterprise_interpret(key,actor,payload['run_id'],
                                                       payload['result_sha256'],payload['text'])
        _fail('未支持的企业 Agent 操作。','mcp_tool_rejected')

    def agent_action(self, action, payload):
        if action=='advance': return self.lifecycle.advance(payload.get('proposal_id'),payload.get('proposal_sha256'))
        if action=='cancel-task': return self.lifecycle.cancel(payload.get('proposal_id'))
        if action=='revise' and not payload.get('dataset_id'):
            return self.agent.revise(payload.get('proposal_id'),None,payload.get('task'))
        if action in {'draft','reuse','revise'}:
            with self.lock:
                _,frame=self._dataset(payload.get('dataset_id'))
                frame=frame.copy(deep=True)
            if action=='draft':
                result=self.agent.draft(frame,payload.get('task'))
                _atomic(self._task_input_path(result['id']),{'dataset_id':payload['dataset_id']})
                return result
            result=(self.agent.revise(payload.get('proposal_id'),frame,payload.get('task')) if action=='revise'
                    else self.agent.reuse(payload.get('proposal_id'),frame))
            _atomic(self._task_input_path(result['id']),{'dataset_id':payload['dataset_id']})
            return result
        key=payload.get('proposal_id')
        if action=='publish': return self.agent.publish(key,payload.get('proposal_sha256'))
        if action=='generate': return self.agent.generate(key,payload.get('bundle_sha256'))
        if action=='run': return self.agent.run(key,payload.get('candidate_sha256'))
        if action=='public-test': return self.agent.public_test(key,payload.get('candidate_sha256'))
        if action=='repair': return self.agent.repair(key,payload.get('candidate_sha256'))
        if action=='interpret': return self.agent.interpret(key,payload.get('run_id'))
        _fail('未知 Agent 操作。')

    def plans(self):
        result = []
        for path in sorted((self.root / 'plans').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            try:
                if path.stat().st_size <= MAX_INPUT:
                    result.append(_json(path.read_bytes()))
            except (ValueError, OSError):
                continue
        return result

    def save_plan(self, payload):
        from tools.workbench_analysis import validate_spec
        name, spec = payload.get('name'), payload.get('spec')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or not isinstance(spec, dict) or len(_bytes(spec)) > 32768:
            _fail('方案名称须为 1～80 字，并包含有效的分析说明。', 'invalid_plan')
        with self.lock:
            _, frame = self._dataset(payload.get('dataset_id'))
            # Persist the canonical contract, including all optional collections.
            # Reusing it with another table still requires preview-time validation.
            canonical = validate_spec(frame, spec)
            record = dict(id=str(uuid.uuid4()), name=name.strip(), spec=canonical)
            _atomic(self.root / 'plans' / (record['id'] + '.json'), record)
            return record

    def history(self):
        results = []
        for path in sorted((self.root / 'metadata').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
            try:
                item = _json(path.read_bytes())
                results.append({k: item[k] for k in ('id', 'name', 'created_at', 'status')})
            except (OSError, ValueError, KeyError):
                continue
        return results

    def recover(self, key):
        path = self.root / 'runs' / _id(key)
        from tools.workbench_analysis import recover_analysis, recover_candidate
        agent_path = self.root / 'agent' / 'runs' / key
        if not path.is_dir() and agent_path.is_dir():
            return recover_candidate(agent_path)
        if not path.is_dir():
            _fail('此记录没有完整的执行结果，请保留现场，不会自动重新执行。', 'run_missing')
        return recover_analysis(path)


def export_csv(result):
    if result.get('status') != 'EXECUTED_UNVERIFIED':
        _fail('此记录没有可导出的执行结果。', 'no_result')
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    columns = result['columns']
    def cell(value):
        # Spreadsheet formula neutralization applies only to strings, not numbers.
        if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
            return "'" + value
        return '' if value is None else value
    writer.writerow([cell(c['label']) for c in columns])
    for row in result['rows']:
        writer.writerow([cell(row[c['key']]) for c in columns])
    return b'\xef\xbb\xbf' + stream.getvalue().encode('utf-8')


def make_server(store, port=0):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'LocalWorkbench'

        def log_message(self, *_):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def send(self, status, body, content_type='application/json; charset=utf-8', extra=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def error(self, status, code, message):
            self.send(status, _bytes({'error': {'code': code, 'message': message}}))

        def allowed(self, post=False):
            host = self.headers.get('Host', '')
            valid = {'127.0.0.1:%s' % self.server.server_port, 'localhost:%s' % self.server.server_port}
            if host not in valid:
                self.error(403, 'host_rejected', '仅允许从本机工作台地址访问。')
                return False
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + host:
                self.error(403, 'origin_rejected', '不允许其他网页访问工作台。')
                return False
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.error(403, 'cross_site_rejected', '不允许跨站访问工作台。')
                return False
            token = self.headers.get('X-Workbench-Token', '')
            if post and (not token.isascii() or not hmac.compare_digest(token, store.token)):
                self.error(403, 'token_required', '请从工作台页面提交操作，或刷新页面重试。')
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            path = urlsplit(self.path).path
            try:
                if path in ASSETS:
                    file = REPO / 'workbench' / ASSETS[path]
                    if not file.is_file():
                        self.error(404, 'not_found', '页面资源尚未就绪。')
                        return
                    kind = 'text/html; charset=utf-8' if path == '/' else 'text/css; charset=utf-8' if path.endswith('.css') else 'text/javascript; charset=utf-8'
                    self.send(200, file.read_bytes(), kind)
                elif path == '/api/bootstrap':
                    self.send(200, _bytes(_wire(dict(app_id='local-analysis-workbench-v1', version=DESKTOP_VERSION, state_id=store.state_id, csrf_token=store.token, capabilities={'xlsx': importlib.util.find_spec('openpyxl') is not None, 'natural_language': False}, plans=store.plans(), runs=store.history()))))
                elif path == '/api/agent/readiness':
                    self.send(200, _bytes(dict(store.agent.readiness(),
                        host=store.host_status(),configuration=store.internal_configuration,external_configuration=store.external_configuration,token_budget=store.budget_view())))
                elif path == '/api/agent/tasks':
                    self.send(200, _bytes(_wire(store.agent.history())))
                elif path.startswith('/api/agent/tasks/'):
                    self.send(200, _bytes(_wire(store.agent.get(path[len('/api/agent/tasks/'):]))))
                elif path.startswith('/api/runs/'):
                    self.send(200, _bytes(_wire(store.recover(path[len('/api/runs/'):]))))
                elif path.startswith('/api/export/'):
                    key = _id(path[len('/api/export/'):])
                    self.send(200, export_csv(store.recover(key)), 'text/csv; charset=utf-8', {'Content-Disposition': 'attachment; filename="analysis-%s.csv"' % key})
                else:
                    self.error(404, 'not_found', '找不到此页面。')
            except Exception as error:
                self.handle_error(error)

        def do_POST(self):
            mcp_request=urlsplit(self.path).path.startswith('/api/mcp/')
            if not self.allowed(post=not mcp_request):
                return
            try:
                if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    _fail('请求必须使用 JSON。', 'content_type')
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= length <= MAX_BODY:
                    _fail('请求内容过大。', 'input_size')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    _fail('请求未传输完整。')
                payload = _json(raw)
                if not isinstance(payload, dict):
                    _fail('请求格式不正确。')
                if mcp_request:
                    if self.headers.get('Origin'):
                        _fail('企业 MCP 仅接受独立本机客户端。','mcp_origin_rejected')
                    name=urlsplit(self.path).path[len('/api/mcp/'):]
                    result=store.mcp_action(name,payload,self.headers.get('X-Workbench-MCP-Session'),
                                           self.headers.get('X-Workbench-MCP-Token'))
                    self.send(200,_bytes(_wire(result)))
                    return
                if urlsplit(self.path).path == '/api/agent/configure':
                    self.send(200,_bytes(store.configure_agent(payload)))
                    return
                registration_routes={'/api/agent/mcp-register':store.register_host,
                    '/api/agent/mcp-admit':store.admit_host,'/api/agent/mcp-revoke':store.revoke_host}
                if urlsplit(self.path).path in registration_routes:
                    self.send(200,_bytes(registration_routes[urlsplit(self.path).path](payload)))
                    return
                if urlsplit(self.path).path == '/api/agent/mcp-session':
                    self.send(200,_bytes(store.create_mcp_session(payload)))
                    return
                if urlsplit(self.path).path == '/api/agent/cancel':
                    self.send(200,_bytes(store.agent.cancel(payload.get('kind'),payload.get('operation_id'))))
                    return
                if urlsplit(self.path).path == '/api/shutdown':
                    store.lifecycle.stop()
                    self.send(200, _bytes({'status': 'stopped'}))
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                routes = {'/api/import': store.import_table, '/api/preview': store.preview, '/api/run': store.run, '/api/plans': store.save_plan}
                agent_routes = {'/api/agent/' + name: name for name in
                                ('draft', 'publish', 'generate', 'run', 'interpret', 'reuse', 'revise', 'public-test', 'repair', 'advance', 'cancel-task')}
                route = urlsplit(self.path).path
                if route in agent_routes:
                    self.send(200, _bytes(_wire(store.agent_action(agent_routes[route], payload))))
                    return
                action = routes.get(route)
                if action is None:
                    self.error(404, 'not_found', '找不到此操作。')
                    return
                self.send(200, _bytes(_wire(action(payload))))
            except Exception as error:
                self.handle_error(error)

        def handle_error(self, error):
            if isinstance(error, InputError) or (hasattr(error, 'code') and hasattr(error, 'message')):
                self.error(400, error.code, error.message)
            elif isinstance(error, (ValueError, TypeError, UnicodeError, RecursionError)):
                self.error(400, 'invalid_request', '请求或表格格式不正确，请检查后重试。')
            elif isinstance(error, (BrokenPipeError, ConnectionResetError)):
                return
            else:
                self.error(500, 'local_error', '本地操作未完成。已保留现有记录，请勿删除后反复执行。')

    server=ThreadingHTTPServer(('127.0.0.1', port), Handler)
    store.server_url='http://127.0.0.1:%s/' % server.server_port
    _atomic(store.root/'server.json',{'url':store.server_url,'pid':os.getpid(),
        'app_id':'local-analysis-workbench-v1','state_id':store.state_id,'version':DESKTOP_VERSION})
    store.lifecycle.resume_safe()
    return server


def main():
    parser = argparse.ArgumentParser(description='Local synthetic analysis workbench')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--state', type=Path, default=REPO / 'workspaces' / 'workbench')
    parser.add_argument('--open-browser', action='store_true')
    args = parser.parse_args()
    store = Store(args.state)
    server = make_server(store, args.port)
    address = 'http://127.0.0.1:%s/' % server.server_port
    # make_server publishes the pinned endpoint identity.
    if args.open_browser:
        webbrowser.open(address)
    if sys_stdout_available():
        print(address, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def sys_stdout_available():
    import sys
    return sys.stdout is not None


if __name__ == '__main__':
    main()
