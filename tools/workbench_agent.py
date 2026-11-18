"""Local prospective Agent orchestration. Disabled by default; no live defaults."""
from contextlib import nullcontext
import copy
import os
import json
from pathlib import Path
import re
import time
import uuid
import threading
from functools import wraps

from runtime.mvp_binding import BindingRejected, issue_binding, public_schema, bind_records
from tools import workbench_analysis as analysis
from tools import local_synthetic_workflow as storage
from tools.local_attempt_ledger import Busy


class AgentError(ValueError):
    def __init__(self, code, message='Agent 操作未完成；已保留原记录。'):
        self.code, self.message = code, message
        super().__init__(message)


def require(ok, code, message):
    if not ok:
        raise AgentError(code, message)


def digest(value):
    return storage._hash(storage._bytes(value))


def normalize_input_scope(frame, scope):
    valid = type(scope) is dict and 'columns' in scope and not (
        set(scope) - {'columns', 'value_columns', 'rows', 'allow_result_rows'})
    require(valid, 'input_scope_required', '请先选择内侧模型可见的字段。')
    columns, values, rows = scope['columns'], scope.get('value_columns', []), scope.get('rows', [])
    def selection(items, allowed):
        return (type(items) is list and all(type(n) is str and n in allowed for n in items)
                and len(items) == len(set(items)))
    require(selection(columns, frame.columns) and columns and selection(values, columns),
            'invalid_input_scope', '字段授权含重复或无效字段。')
    require(type(rows) is list and all(type(i) is int and 1 <= i <= len(frame) for i in rows)
            and len(rows) == len(set(rows)) and bool(rows) == bool(values)
            and type(scope.get('allow_result_rows', False)) is bool,
            'invalid_input_scope', '样例值须同时指定获准字段与有效行号。')
    return {'columns': [n for n in frame if n in columns], 'value_columns': [n for n in frame if n in values],
            'rows': sorted(rows), 'allow_result_rows': scope.get('allow_result_rows', False)}


def _id(value):
    require(type(value) is str and re.fullmatch('[0-9a-f-]{36}', value) and
            str(uuid.UUID(value)) == value, 'invalid_id', '任务编号无效。')
    return value


def serialized(fn):
    @wraps(fn)
    def call(self, *args, **kwargs):
        def invoke():
            with self.lock:
                try:
                    if fn.__name__ in mutations and args and getattr(self,'lifecycle',None):
                        self.lifecycle.guard(args[0])
                    return fn(self, *args, **kwargs)
                except (AgentError, analysis.WorkbenchError):
                    raise
                except (ValueError, TypeError, KeyError, OSError, storage.WorkflowError):
                    raise AgentError('invalid_agent_record', 'Task record invalid; preserved without continuing.') from None
        mutations={'publish','generate','run','reuse','revise','public_test','repair',
                   'interpret','enterprise_accept','enterprise_interpret'}
        if fn.__name__ in mutations and args and getattr(self,'lifecycle',None):
            return self.lifecycle.execute(args[0],fn.__name__,invoke)
        return invoke()
    return call


def recovery_read(fn):
    # Recovery opens the existing ledger writer lock even in read-only mode.
    # Serialize those short reads independently of mutation/transport control.
    @wraps(fn)
    def call(self,*args,**kwargs):
        with self.recovery_lock:
            return fn(self,*args,**kwargs)
    return call


def route_identity(connector):
    return {'source': getattr(connector, 'source', 'unconfigured'),
            'profile_sha256': digest(getattr(connector, 'profile', {}))}


class AgentWorkbench:
    def __init__(self, root, internal=None, external=None, execution_authorization=None):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.recovery_lock = threading.RLock()
        self.internal, self.external = internal, external
        self.execution_authorization = copy.deepcopy(execution_authorization or {})
        self.enterprise_actor = None
        (self.root / 'tasks').mkdir(parents=True, exist_ok=True)
        (self.root / 'runs').mkdir(parents=True, exist_ok=True)
        (self.root / 'claims').mkdir(parents=True, exist_ok=True)
        (self.root / 'public-tests').mkdir(parents=True, exist_ok=True)

    def readiness(self):
        def state(connector, kind):
            if connector is None:
                return {'ready': False, 'live': False, 'source': 'unconfigured',
                        'missing': ['approved_' + kind + '_route', 'account_model_budget',
                                    'data_authorization', 'verified_launch_boundary']}
            return connector.readiness()
        internal, external = state(self.internal, 'internal'), state(self.external, 'external')
        if getattr(self.external,'source',None)=='qoder' and getattr(self,'lifecycle',None):
            admission=self.lifecycle.qoder_admission_status(self.external)
            external=dict(external,budget_coverage='sdk_limits_conservative_not_wire_verified',
                          live_admission=admission)
            if admission!='reviewed_conservative_contract':
                reason='token_budget' if admission=='budget_exhausted' else 'live_budget_bound_unverified'
                external.update(ready=False,missing=list(dict.fromkeys(list(external.get('missing',[]))+[reason])))

        if self.enterprise_actor is not None:
            internal={'ready':True,'live':False,'source':'enterprise_mcp','missing':[],
                      'mode':'inbound_actor','host_budget_enforcement':'operator_acknowledged'}

        return {'status': 'READY' if internal['ready'] and external['ready'] else 'BLOCKED',
                'input_recipient':self.enterprise_actor or route_identity(self.internal),
                'live': bool(internal.get('live') and external.get('live')),
                'internal': internal, 'external': external,
                'execution': {'ready': self._execution_allowed(),
                              'missing': [] if self._execution_allowed() else ['synthetic_execution_authorization'],
                              'message': '仅限明确核准的合成数据；现有监督器不构成恶意代码或生产数据隔离。'}}

    def _execution_allowed(self):
        return self.execution_authorization == {
            'authorized': True, 'data_scope': 'synthetic_only',
            'risk_acknowledgement': 'existing_supervisor_not_hostile_isolation'}

    def _connector(self, kind):
        value = getattr(self, kind)
        require(value is not None and value.readiness()['ready'],
                'connector_not_ready', '尚未配置并核准模型路线、数据范围、费用上限及启动隔离；未发起请求。')
        if kind=='external' and getattr(value,'source',None)=='qoder' and getattr(self,'lifecycle',None) and not self.lifecycle.qoder_admission_verified(value):
            raise AgentError('live_budget_bound_unverified',
                'Qoder request bound is unverified; new live submission is blocked. Existing results remain available.')
        return value

    def _path(self, key):
        return self.root / 'tasks' / _id(key)

    def _load(self, key, public_only=False, seen=None):
        seen=set() if seen is None else set(seen)
        require(key not in seen and len(seen)<64,'lineage_changed','任务版本关系无效或存在循环。')
        seen.add(key)
        record = storage._json(self._path(key) / 'record.json')
        require(record['id'] == key and record['proposal_id'] == key,
                'task_identity_changed', '任务身份已改变。')
        if not public_only:
            private = storage._json(self._path(key) / 'private.json')
            require(digest(private) == record['private_sha256'],
                    'private_snapshot_changed', '本地任务或输入快照已改变。')
        if record.get('parent_id'):
            reference=storage._json(self._path(key)/'parent-ref.json')
            require(digest(reference)==record.get('parent_ref_sha256') and
                all(record.get(k)==reference[k] for k in ('parent_id','revision_root','version_kind','repair_root_id')),
                'lineage_changed','任务版本引用已改变，已阻止继续。')
            parent=self._load(record['parent_id'],public_only=public_only,seen=seen)
            require(all(parent.get(k)==v for k,v in reference['pins'].items()),
                'lineage_changed','所引用的原任务内容已改变。')
            require(reference['revision_root']==parent.get('revision_root',parent['id']) and
                reference['repair_root_id']==parent.get('repair_root_id',parent['id']),
                'lineage_changed','任务版本家族身份已改变。')
        if 'public_bundle' in record:
            require(digest(record['public_bundle']) == record['bundle_sha256'] and
                    self._proposal_digest(record) == record['proposal_sha256'],
                    'proposal_changed', '任务公开内容或路线身份已改变。')
            record['public_preview'] = storage._bytes(record['public_bundle']).decode('utf-8')
        return record

    def _proposal_digest(self, record):
        identity={'bundle':record['public_bundle'],'private_sha256':record['private_sha256'],
            'route':record['external_route'],'internal_route':record['internal_route']}
        if 'task_binding_sha256' in record:
            identity['task_binding_sha256'] = record['task_binding_sha256']
        if record.get('parent_ref_sha256'):identity['parent_ref_sha256']=record['parent_ref_sha256']
        return digest(identity)

    def _pin_binding(self, record, private, binding):
        pin = digest(binding)
        if binding != private['binding']:
            storage._publish(self._path(record['id']) / ('binding-' + pin + '.json'), storage._bytes(binding))
        record['task_binding_sha256'] = pin

    def _execution_private(self, record):
        private = storage._json(self._path(record['id']) / 'private.json')
        require(digest(private) == record['private_sha256'], 'private_snapshot_changed', '本地输入快照已改变。')
        pin = record.get('task_binding_sha256')
        if pin is not None and digest(private['binding']) != pin:
            require(type(pin) is str and re.fullmatch('[0-9a-f]{64}', pin), 'binding_changed', '任务绑定摘要无效。')
            binding = storage._json(self._path(record['id']) / ('binding-' + pin + '.json'))
            require(digest(binding) == pin, 'binding_changed', '任务字段绑定已改变。')
            private['binding'] = binding
        return private

    @serialized
    def disclosure(self, key, proposal_sha256):
        record = self._load(key)
        require(record.get('proposal_sha256') and proposal_sha256 == record['proposal_sha256'],
                'proposal_changed', '请重新查看当前提案。')
        require(route_identity(self.external) == record['external_route'], 'route_changed',
                '外侧目的地已改变，请创建新提案。')
        binding = self._execution_private(record)['binding']
        bundle = record['public_bundle']
        names = {symbol: name for name, symbol in binding['mapping']['fields'].items()}
        roles = {role['column']: role['meaning'] for role in bundle['roles']}
        fields = []
        for field in bundle['schema']['fields']:
            symbol = field['name']
            fields.append({'local_name': names[symbol], 'symbol': symbol, 'dtype': field['dtype'],
                           'meaning': roles.get(symbol, '旧版整表字段'),
                           'restores': [col['key'] for col in bundle['output']['columns']
                                        if col['restore_from'] == symbol]})
        profile = getattr(self.external, 'profile', {})
        source = record['external_route']['source']
        return {'proposal_sha256': proposal_sha256, 'fields': fields, 'source': source,
                'destination': profile.get('url') if source in {'remote_node', 'external_http'} else None,
                'model': profile.get('model'), 'legacy': 'task_binding_sha256' not in record}

    def _lineage(self, record, parent, kind):
        reference={'parent_id':parent['id'],'revision_root':parent.get('revision_root',parent['id']),
            'repair_root_id':parent.get('repair_root_id',parent['id']),'version_kind':kind,
            'pins':{k:parent[k] for k in ('private_sha256','proposal_sha256','bundle_sha256','candidate_sha256') if k in parent}}
        storage._publish(self._path(record['id'])/'parent-ref.json',storage._bytes(reference))
        record.update({k:reference[k] for k in ('parent_id','revision_root','repair_root_id','version_kind')})
        record.update(root_id=reference['revision_root'],lineage_kind='public_repair' if kind=='repair' else kind,
            parent_ref_sha256=digest(reference))

    def _claim(self, name, value):
        path=self.root/'claims'/name
        try:
            with path.open('xb') as stream:
                stream.write(storage._bytes(value));stream.flush();os.fsync(stream.fileno())
        except FileExistsError:
            raise AgentError('operation_already_claimed','此操作已登记；为避免重复，不会再次提交。') from None

    def _save(self, record):
        target = self._path(record['id']) / 'record.json'
        temporary = target.with_name('.tmp-' + uuid.uuid4().hex)
        try:
            with temporary.open('xb') as stream:
                stream.write(storage._bytes(record))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary), str(target))
        finally:
            if temporary.exists():
                temporary.unlink()

    def _event(self, record, status, kind):
        record['status'] = status
        record['events'].append({'sequence': len(record['events']) + 1,
                                 'kind': kind, 'status': status,
                                 'at': time.strftime('%Y-%m-%dT%H:%M:%S')})
        self._save(record)
        if status=='DRAFT_UNKNOWN_BLOCKED' and getattr(self,'lifecycle',None):
            self.lifecycle.local.key=record['id']

    def get(self, key):
        record = self._load(key)
        scope=self.input_scope(record)
        record['input_schema_columns']=scope.get('columns',[])
        record['allow_result_rows']=scope.get('allow_result_rows') is True
        if record.get('run_id'):
            run_path = self.root / 'runs' / record['run_id']
            lifecycle=getattr(self,'lifecycle',None)
            pending=lifecycle.view(key).get('operation',{}) if lifecycle else {}
            if lifecycle and record.get('parent_id') and record['status']=='EXECUTION_UNKNOWN_BLOCKED':
                parent_pending=lifecycle.view(record['parent_id']).get('operation',{})
                if parent_pending.get('action')=='reuse' and parent_pending.get('status')=='started':
                    pending=parent_pending
            if run_path.is_dir() and not (pending.get('status')=='started' and pending.get('action') in {'run','reuse'}):
                record['result'] = self._associated_result(record)
                if record.get('interpretation'):
                    require(record['interpretation']['result_sha256'] == digest(record['result']),
                            'interpretation_identity_changed', '解释所引用的结果已改变。')
        public_test=self._public_test_summary(record)
        if public_test is not None:record['public_test']=public_test
        family=_id(record.get('repair_root_id',key))
        record['repair_slots_used']=sum((self.root/'claims'/('repair-family-'+family+'-'+str(slot)+'.json')).exists()
            for slot in (1,2))
        repair_claim=self.root/'claims'/('repair-parent-'+key+'.json')
        if repair_claim.exists():
            child_id=_id(storage._json(repair_claim)['child_id'])
            if (self._path(child_id)/'record.json').exists():record['repair_child_id']=child_id
            else:record['repair_reservation_blocked']=True
        if getattr(self,'lifecycle',None): record['lifecycle']=self.lifecycle.view(key)
        return copy.deepcopy(record)

    def input_scope(self,record):
        private=storage._json(self._path(record['id']) / 'private.json')
        require(digest(private)==record['private_sha256'],'private_snapshot_changed','任务输入快照已改变。')
        return private.get('input_scope',{})

    def mcp_view(self, result, key):
        if 'rows' in result or 'result' in result or 'interpretation' in result:
            owner=result.get('proposal_id') or key
            if self.input_scope(self._load(owner)).get('allow_result_rows') is not True:
                if 'rows' in result:
                    return {k:result[k] for k in ('run_id','status','correctness') if k in result}
                result=dict(result,result=None,interpretation=None)
        return {k:v for k,v in result.items() if k!='input_schema_columns'}

    def history(self):
        result = []
        for path in sorted((self.root / 'tasks').glob('*/record.json'),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
            record = storage._json(path)
            item={k:record[k] for k in ('id','proposal_id','status','events')}
            item.update({k:record[k] for k in ('parent_id','revision_root','version_kind','repair_root_id','repair_index') if k in record})
            result.append(item)
        return result

    def _prepare(self, frame, task, identity, parent=None, input_scope=None):
        analysis._frame(frame)
        scope = normalize_input_scope(frame, input_scope if input_scope is not None else {'columns': list(frame)})
        require(type(task) is str and 1<=len(task.strip())<=8000,
                'invalid_task','请填写 1～8000 字的分析任务。')
        binding=issue_binding(frame.loc[:, scope['columns']],analysis.NAMESPACE,analysis.SCOPE)
        key=str(uuid.uuid4())
        self._path(key).mkdir()
        private={'task':task,'records':frame.to_dict('records'),'columns':list(frame),'binding':binding,'input_scope':scope}
        storage._publish(self._path(key)/'private.json',storage._bytes(private))
        record={'id':key,'proposal_id':key,'events':[],'status':'DRAFT_STARTED',
                'source':identity['source'],'result':None,'interpretation':None,
                'private_sha256':digest(private),'external_route':route_identity(self.external),
                'internal_route':identity,
                'external_selection':{'source':getattr(self.external,'source','unconfigured'),
                    'entry_id':getattr(self.external,'profile',{}).get('entry_id'),
                    'model':getattr(self.external,'profile',{}).get('model'),
                    'version':getattr(self.external,'profile',{}).get('version'),
                    'reason':getattr(self.external,'selection_reason','configured_route')}}
        if parent is not None:self._lineage(record,parent,'revision')
        return record,private

    def _internal_context(self, private):
        import pandas as pd
        frame=pd.DataFrame(private['records']).loc[:,private['columns']]
        scope=normalize_input_scope(frame,private.get('input_scope'))
        binding=private['binding']
        return {'purpose':'draft','task':private['task'],
            'internal_schema':[{'name':n,'symbol':binding['mapping']['fields'][n],
                               'dtype':str(frame[n].dtype)} for n in scope['columns']],
            'records':[{n:private['records'][i-1][n] for n in scope['value_columns']} for i in scope['rows']],
            'required_response':{'objective':'public abstract task',
                'roles':[{'column':'COL_0001','meaning':'public semantic role'}],
                'output':{'columns':[{'key':'result','label':'public label',
                    'type':'integer|string|number','nullable':False,'restore_from':None}],'max_rows':1000}},
            'instructions':'Declare each required input exactly once in roles, including any restore_from field. Use only required opaque columns. Do not copy private names, values, paths or raw task into public descriptions. Do not write code.'}

    def _accept_proposal(self, record, private, proposed):
        import pandas as pd
        frame=pd.DataFrame(private['records']).loc[:,private['columns']]
        binding,task=private['binding'],private['task']
        require(type(proposed) is dict and set(proposed) == {'objective', 'roles', 'output'},
                'invalid_proposal', '内侧模型未返回完整结构化任务约定。')
        require(type(proposed['objective']) is str and 1 <= len(proposed['objective']) <= 2000
                and type(proposed['roles']) is list and 1 <= len(proposed['roles']) <= 32,
                'invalid_proposal', '请明确声明本任务需要的字段和用途。')
        symbols = {f['name'] for f in public_schema(binding)['fields']}
        seen = set()
        for role in proposed['roles']:
            require(type(role) is dict and set(role) == {'column', 'meaning'} and
                    type(role['column']) is str and role['column'] in symbols and role['column'] not in seen and
                    type(role['meaning']) is str and 1 <= len(role['meaning']) <= 200,
                    'invalid_proposal', '公共角色必须唯一引用现有不透明列。')
            seen.add(role['column'])
        output = analysis.validate_output_contract(proposed['output'], binding)
        original_fields = binding['mapping']['fields']
        columns = [name for name in frame if name in original_fields and original_fields[name] in seen]
        if len(columns) != len(original_fields):
            binding = issue_binding(frame.loc[:, columns], analysis.NAMESPACE, analysis.SCOPE)
        rename = {original_fields[name]: binding['mapping']['fields'][name] for name in columns}

        def task_symbol(symbol):
            require(symbol in rename, 'undeclared_task_field', '公开任务或还原输出引用了未声明字段，请补充用途并重新提案。')
            return rename[symbol]

        def public_text(value):
            return re.sub(r'COL_[0-9]{4}(?![0-9])', lambda match: task_symbol(match.group()), value)

        roles = [{'column': task_symbol(role['column']), 'meaning': public_text(role['meaning'])}
                 for role in proposed['roles']]
        for col in output['columns']:
            if col['restore_from'] is not None:
                col['restore_from'] = task_symbol(col['restore_from'])
            col['key'] = public_text(col['key'])
            col['label'] = public_text(col['label'])
        output = analysis.validate_output_contract(output, binding)
        schema = public_schema(binding)
        # Examples depend only on public dtypes, never row counts, ranges, or enum domains.
        examples = [{f['name']: ('SYNTHETIC_' + str(i) if f['dtype'] == 'object'
                     else bool(i%2) if f['dtype']=='bool' else i + 0.25 if f['dtype'] == 'float64' else i)
                     for f in schema['fields']} for i in (1, 2, 3)]
        bundle = {'version': 1, 'objective': public_text(proposed['objective']), 'roles': roles,
                  'schema': schema, 'examples': examples, 'output': output,
                  'program_contract': 'Write analysis.py in the isolated public workspace, containing original UTF-8 Python source defining analyze(df). It must return a JSON object with only rows, an array of objects with exactly output column keys. Do not access files, network, environment or subprocesses. Text input is opaque scoped tokens; preserve tokens in restore_from output columns. Examples are independent synthetic values, not the real token domain. No markdown fences.'}
        if record['external_route']['source'] in {'external_http','codex','claude','acp','remote_node','cursor_sdk'}:
            bundle['program_contract']=bundle['program_contract'].replace(
                'Write analysis.py in the isolated public workspace, containing original UTF-8 Python source defining analyze(df).',
                'Return only original UTF-8 Python source text defining analyze(df). No file tools are available.')
        text = json.dumps([proposed, {k: bundle[k] for k in ('objective','roles','output')}], ensure_ascii=False)
        forbidden = list(frame.columns) + [v for c in frame for v in frame[c] if type(v) is str]
        forbidden.append(task)
        require(not any(len(v) >= 4 and v in text for v in forbidden),
                'private_text_in_public', '拟公开内容包含原始字段、文本值或任务原文；已拦截，需重新抽象。')
        self._pin_binding(record, private, binding)
        record.update(public_bundle=bundle, bundle_sha256=digest(bundle),
                      public_preview=storage._bytes(bundle).decode('utf-8'))
        record['proposal_sha256']=self._proposal_digest(record)
        self._event(record, 'AWAITING_PUBLICATION_APPROVAL', 'proposal_validated')

    def _call_connector(self, connector, payload, **kwargs):
        # Bind cancellation to this actual transport owner, not another queued task.
        lifecycle=getattr(self,'lifecycle',None)
        key=getattr(lifecycle.local,'key',None) if lifecycle else None
        kind='external' if connector is self.external else 'internal'
        if lifecycle and key:
            with lifecycle.lock:
                lifecycle.guard(key)
                require(kind not in lifecycle.transports,'operation_active','Another task owns this transport.')
                lifecycle.transports[kind]=key
        self.lock.release()
        try:
            if lifecycle and key: lifecycle.guard(key)
            return connector.call(payload, **kwargs)
        finally:
            if lifecycle and key:
                with lifecycle.lock:
                    if lifecycle.transports.get(kind)==key: lifecycle.transports.pop(kind,None)
            self.lock.acquire()

    @serialized
    def draft(self, frame, task, _parent=None, input_scope=None, expected_route=None):
        identity=self.enterprise_actor or route_identity(self.internal)
        if self.enterprise_actor is not None or (self.internal is not None and
                (identity['source']!='local_test_double' or self.internal.readiness().get('live') is not False)):
            require(type(input_scope) is dict and expected_route is not None,'input_scope_required',
                    '请先明确批准内侧接收方和可见字段。')
        require(expected_route is None or expected_route == identity,'internal_route_changed','内侧接收方已改变，请重新核对。')
        if self.enterprise_actor is not None:
            record,_=self._prepare(frame,task,self.enterprise_actor,_parent,input_scope)
            self._event(record,'AWAITING_INTERNAL_PROPOSAL','enterprise_context_prepared')
            return self.get(record['id'])
        connector=self._connector('internal')
        record,private=self._prepare(frame,task,route_identity(connector),_parent,input_scope)
        self._event(record,'DRAFT_UNKNOWN_BLOCKED','internal_request_started')
        try:
            proposed=self._call_connector(connector,self._internal_context(private))
            record['internal_receipt']=copy.deepcopy(getattr(connector,'last_receipt',None))
            self._accept_proposal(record,private,proposed)
        except Exception:
            record['internal_receipt']=copy.deepcopy(getattr(connector,'last_receipt',None))
            self._event(record,'DRAFT_FAILED','internal_request_failed_or_invalid')
            raise
        return self.get(record['id'])

    @serialized
    def revise(self, key, frame, task, input_scope=None, expected_route=None):
        parent=self._load(key)
        require('UNKNOWN' not in parent['status'],'revision_unavailable',
            '原任务提交状态未知；请先核对，不可通过修订重放。')
        tested=self._public_test_summary(parent)
        require(tested is None or tested['status']!='UNKNOWN_BLOCKED',
            'revision_unavailable','公开测试状态未知；请先核对，不可通过修订重放。')
        if frame is None:
            import pandas as pd
            private=storage._json(self._path(key)/'private.json')
            frame=pd.DataFrame(private['records']).loc[:,private['columns']]
            if input_scope is None:
                require(self.enterprise_actor is None and getattr(self.internal,'source',None)=='local_test_double'
                        and self.internal.readiness().get('live') is False,'input_scope_required','请重新核对本次修订的字段授权。')
                require('input_scope' in private,'input_scope_required','历史任务缺少字段授权，请重新导入并选择字段。')
                input_scope={'columns':private['input_scope']['columns']}
        return self.draft.__wrapped__(self,frame,task,_parent=parent,input_scope=input_scope,expected_route=expected_route)

    def _public_material(self, record):
        require('UNKNOWN' not in record['status'] and record.get('candidate_sha256') and
            record.get('approved_proposal_sha256')==record.get('proposal_sha256'),
            'public_test_unavailable','需要已收到的原始程序与对应的精确公开批准。')
        raw=storage._read(self._path(record['id'])/'public-bundle.json')
        require(raw==storage._bytes(record['public_bundle']) and digest(record['public_bundle'])==record['bundle_sha256'],
            'bundle_changed','已批准的公开材料发生变化。')
        candidate=storage._read(self._path(record['id'])/'candidate.py')
        require(storage._hash(candidate)==record['candidate_sha256'],'candidate_changed','原始程序已改变。')
        return record['public_bundle'],candidate

    @recovery_read
    def _public_test_summary(self, record):
        claim_path=self.root/'claims'/('public-test-'+record['id']+'.json')
        if not claim_path.exists():return None
        claim=storage._json(claim_path)
        bundle=record['public_bundle']
        binding=analysis._public_binding(bundle['schema'],bundle['examples'])
        expected={'proposal_id':record['id'],'owner_id':record['id'],'candidate_sha256':record['candidate_sha256'],
            'bundle_sha256':record['bundle_sha256'],'request_sha256':digest(analysis._public_request(binding)),
            'binding_sha256':digest(binding),'contract_sha256':digest(bundle['output']),
            'source':record['candidate_source']}
        require(all(claim.get(k)==v for k,v in expected.items()),
            'public_test_association_changed','公开测试与本任务的程序或样例不匹配。')
        run_id=_id(claim['run_id'])
        summary={'status':'UNKNOWN_BLOCKED','run_id':run_id,'candidate_sha256':record['candidate_sha256'],
            'bundle_sha256':record['bundle_sha256'],'reason_code':None,'result_sha256':None,
            'correctness':'NOT_EVALUATED','scope':'published_examples_only'}
        lifecycle=getattr(self,'lifecycle',None)
        pending=lifecycle.view(record['id']).get('operation',{}) if lifecycle else {}
        own_operation=bool(lifecycle and getattr(lifecycle.local,'key',None)==record['id'])
        if pending.get('status')=='started' and pending.get('action')=='public_test' and not own_operation:
            return dict(summary,in_flight=True)
        root=self.root/'public-tests'/run_id
        if not (root/'manifest.json').is_file():return summary
        manifest=storage._json(root/'manifest.json')
        require(manifest['task']==analysis.PUBLIC_AGENT_TASK and manifest['run_id']==run_id and
            manifest['runtime']==claim['runtime'] and
            all(manifest[k]==v for k,v in expected.items() if k!='proposal_id'),
            'public_test_association_changed','公开测试运行身份已改变。')
        # Ledger recovery still takes its writer lock; recovery_read serializes
        # concurrent GUI and execution-gate readers without holding self.lock.
        try:
            result=analysis._recover_agent_locked(root)
        except Busy:
            raise AgentError('public_test_busy','另一次操作正在核对公开测试；未重复执行，请稍后刷新。') from None
        require(result['run_id']==run_id and result['candidate_sha256']==record['candidate_sha256'],
            'public_test_association_changed','公开测试结果身份已改变。')
        summary['result_sha256']=digest(result)
        if result['status']=='EXECUTED_UNVERIFIED':summary['status']='PASSED'
        elif result['status']=='EXECUTION_FAILED':
            summary['status']='FAILED'
            reason=result.get('reason')
            if reason in {'enforcement_unavailable','cleanup_failed','worker_startup_blocked'}:
                summary.update(status='ISOLATION_BLOCKED',reason_code='PUBLIC_ISOLATION_UNAVAILABLE')
                return summary
            summary['reason_code']=('PUBLIC_OUTPUT_INVALID' if reason=='invalid_table_output' else
                'PUBLIC_RESOURCE_LIMIT' if reason in {'timeout','output_limit','resource_limit','result_limit'} else
                'PUBLIC_SOURCE_REJECTED' if reason in {'invalid_source','syntax_error'} else 'PUBLIC_RUNTIME_ERROR')
        return summary

    @serialized
    def public_test(self, key, candidate_sha256):
        record=self._load(key,public_only=True)
        require(record.get('candidate_sha256')==candidate_sha256,'candidate_changed','请确认当前原始程序摘要。')
        bundle,candidate=self._public_material(record)
        require(record['candidate_source']=='local_test_double' or self._execution_allowed(),
            'synthetic_execution_authorization_required','尚缺本次合成数据执行授权。')
        binding=analysis._public_binding(bundle['schema'],bundle['examples'])
        run_id=str(uuid.uuid4())
        claim={'proposal_id':key,'owner_id':key,'run_id':run_id,'candidate_sha256':candidate_sha256,
            'bundle_sha256':record['bundle_sha256'],'request_sha256':digest(analysis._public_request(binding)),
            'binding_sha256':digest(binding),'contract_sha256':digest(bundle['output']),
            'source':record['candidate_source'],
            'runtime':analysis.agent_runtime(analysis.Limits(input_bytes=1024*1024,result_bytes=1024*1024))}
        self._claim('public-test-'+key+'.json',claim)
        analysis.run_public_candidate(candidate,bundle,self.root/'public-tests'/run_id,source=record['candidate_source'],proposal_id=key)
        record['public_test']=self._public_test_summary(record)
        if getattr(self,'lifecycle',None): record['lifecycle']=self.lifecycle.view(key)
        return copy.deepcopy(record)

    @serialized
    def repair(self, key, candidate_sha256):
        parent=self._load(key)
        require(parent.get('candidate_sha256')==candidate_sha256,'candidate_changed','请确认当前原始程序摘要。')
        bundle,candidate=self._public_material(parent)
        failure=self._public_test_summary(parent)
        require(failure is not None and failure['status']=='FAILED','repair_unavailable',
            '仅确定的公开契约测试失败允许修复；未知结果不可重放。')
        connector=self._connector('external')
        require(route_identity(connector)==parent['external_route'],'route_changed',
            '修复需沿用原外侧连接；更换连接请创建任务修订。')
        if connector.source == 'remote_node':
            require(connector.supports_public_repair(), 'remote_repair_unsupported',
                    '当前运行节点版本不支持公开修复。')
        private=storage._json(self._path(key)/'private.json')
        scoped_binding = (self._execution_private(parent)['binding']
                          if 'task_binding_sha256' in parent else None)
        base=copy.deepcopy(bundle)
        base.pop('repair',None)
        envelope=copy.deepcopy(base)
        envelope['repair']={'base_bundle_sha256':digest(base),'previous_candidate_sha256':candidate_sha256,
            'previous_candidate_source':candidate.decode('utf-8'),'public_failure_code':failure['reason_code']}
        require(len(storage._bytes(envelope))<=65536,'repair_bundle_too_large','修复公开材料超过大小上限，未截断或发送。')
        child_id=str(uuid.uuid4())
        family=parent.get('repair_root_id',parent['id'])
        # The root family owns only two durable slots. A separate parent claim
        # prevents sibling requests; crashes consume rather than release slots.
        self._claim('repair-parent-'+key+'.json',{'parent_id':key,'child_id':child_id,'family':family})
        index=None
        for slot in (1,2):
            try:
                self._claim('repair-family-'+family+'-'+str(slot)+'.json',
                    {'parent_id':key,'child_id':child_id,'family':family,'slot':slot})
                index=slot;break
            except AgentError as error:
                if error.code!='operation_already_claimed':raise
        require(index is not None,'repair_limit_reached','此任务家族已使用两次修复名额。')
        self._path(child_id).mkdir()
        storage._publish(self._path(child_id)/'private.json',storage._bytes(private))
        record={'id':child_id,'proposal_id':child_id,'events':[],'status':'AWAITING_PUBLICATION_APPROVAL',
            'source':parent['source'],'private_sha256':parent['private_sha256'],'result':None,'interpretation':None,
            'external_route':route_identity(connector),'internal_route':copy.deepcopy(parent['internal_route']),
            'public_bundle':envelope,'bundle_sha256':digest(envelope),
            'public_preview':storage._bytes(envelope).decode('utf-8'),
            'repair_index':index,'repair_depth':parent.get('repair_depth',0)+1,
            'parent_public_test_sha256':digest(failure)}
        self._lineage(record,parent,'repair')
        if scoped_binding is not None:
            self._pin_binding(record, private, scoped_binding)
        if 'external_selection' in parent:
            record['external_selection'] = copy.deepcopy(parent['external_selection'])
        record['proposal_sha256']=self._proposal_digest(record)
        self._event(record,'AWAITING_PUBLICATION_APPROVAL','public_repair_prepared')
        return self.get(child_id)

    def _enterprise_record(self, key, actor):
        record=self._load(key)
        require(actor==self.enterprise_actor==record['internal_route'],
                'internal_route_changed','企业内侧 Agent 身份或会话范围已改变。')
        return record,storage._json(self._path(key)/'private.json')

    @serialized
    def enterprise_context(self, key, actor):
        record,private=self._enterprise_record(key,actor)
        require(record['status']=='AWAITING_INTERNAL_PROPOSAL','proposal_not_pending','该任务不再等待内侧提案。')
        context=self._internal_context(private)
        return {'proposal_id':key,'context':context,'context_sha256':digest(context)}

    @serialized
    def enterprise_accept(self, key, actor, context_sha256, proposal):
        record,private=self._enterprise_record(key,actor)
        require(record['status']=='AWAITING_INTERNAL_PROPOSAL' and
                context_sha256==digest(self._internal_context(private)),
                'proposal_context_changed','提案必须引用本任务的精确内侧上下文。')
        self._accept_proposal(record,private,proposal)
        return self.get(key)

    @serialized
    def enterprise_result_context(self, key, actor):
        record,private=self._enterprise_record(key,actor)
        require(private.get('input_scope',{}).get('allow_result_rows') is True,
                'result_scope_required','结果行仅在本机查看。')
        require(record['status'] in {'EXECUTED_UNVERIFIED','AWAITING_INTERNAL_INTERPRETATION'}
                and record['interpretation'] is None,
                'interpretation_not_available','当前任务不等待结果解释。')
        result=self._associated_result(record)
        require(result['status']=='EXECUTED_UNVERIFIED','interpretation_not_available','没有可解释的实际结果。')
        return {'proposal_id':key,'task':private['task'],'run_id':record['run_id'],
                'result':result,'result_sha256':digest(result)}

    @serialized
    def enterprise_interpret(self,key,actor,run_id,result_sha256,text):
        record,_=self._enterprise_record(key,actor)
        context=self.enterprise_result_context(key,actor)
        require(run_id==context['run_id'] and result_sha256==context['result_sha256'] and
                type(text) is str and 1<=len(text)<=12000,
                'interpretation_identity_changed','解释必须引用当前任务的实际结果摘要。')
        record['interpretation']={'text':text,'run_id':run_id,'result_sha256':result_sha256,
                                  'source':'enterprise_mcp'}
        self._event(record,'INTERPRETED_UNVERIFIED','enterprise_interpretation_received')
        return self.get(key)

    def cancel(self, kind, operation_id=None):
        require(kind in {'internal','external'},'invalid_operation','请选择内侧或外侧操作。')
        connector=getattr(self,kind)
        require(connector is not None and callable(getattr(connector,'cancel',None)),
                'cancellation_unavailable','当前连接没有可终止的活动传输。')
        if kind=='internal':
            return connector.cancel(operation_id)
        return connector.cancel()

    @serialized
    def publish(self, key, proposal_sha256):
        record = self._load(key)
        require(record['status'] == 'AWAITING_PUBLICATION_APPROVAL' and
                proposal_sha256 == record['proposal_sha256'],
                'approval_required', '必须确认当前任务的精确公开内容摘要。')
        require(digest(record['public_bundle']) == record['bundle_sha256'],
                'bundle_changed', '拟公开内容已改变。')
        require(route_identity(self.external) == record['external_route'], 'route_changed',
                '外侧目的地已改变，请创建新提案。')
        self._execution_private(record)
        storage._publish(self._path(key) / 'public-bundle.json', storage._bytes(record['public_bundle']))
        record['approved_proposal_sha256'] = proposal_sha256
        self._event(record, 'PUBLISHED', 'exact_publication_approved')
        return self.get(key)

    @serialized
    def generate(self, key, bundle_sha256):
        connector = self._connector('external')
        record = self._load(key)
        require(record['status'] == 'PUBLISHED' and bundle_sha256 == record['bundle_sha256'],
                'publication_required', '仅可提交已精确批准且未提交过的公开包。')
        require(route_identity(connector) == record['external_route'],
                'route_changed', '外侧路线与已批准提案不同；未发起请求。')
        self._execution_private(record)
        bundle = storage._json(self._path(key) / 'public-bundle.json')
        require(digest(bundle) == bundle_sha256 and bundle == record['public_bundle'] and
                record['approved_proposal_sha256'] == record['proposal_sha256'],
                'bundle_changed', '公开包或批准身份已改变。')
        remote = connector.source == 'remote_node'
        options = {}
        if remote:
            # Record the local task association before the transport can send.
            record['generation_receipt'] = connector.submission_receipt(bundle, key)
            record['remote_submission_id'] = key
            options['task_key'] = key
        self._event(record, 'GENERATION_UNKNOWN_BLOCKED', 'external_request_started')
        try:
            candidate = self._call_connector(connector, bundle, **options)
            self._save_candidate(record, candidate, connector.source)
            record['generation_receipt'] = copy.deepcopy(getattr(connector, 'last_receipt',
                {'source': connector.source, 'observed_model': None, 'observed_requests': None}))
            self._event(record, 'CANDIDATE_READY', 'original_candidate_received')
        except Exception:
            # Submission may already have happened. No retry or fallback.
            receipt = getattr(connector, 'last_receipt', None)
            if not remote or (receipt and receipt.get('task_id') == key):
                record['generation_receipt'] = copy.deepcopy(receipt)
            self._event(record, 'GENERATION_UNKNOWN_BLOCKED', 'external_response_unresolved')
            raise AgentError('generation_unknown', '外侧提交结果未知或无效；已锁定本次提交，不会自动重试。') from None
        return self.get(key)

    def _save_candidate(self, record, candidate, source):
        require(type(candidate) is bytes and 0 < len(candidate) <= 1024 * 1024,
                'invalid_candidate', '外侧模型未返回有效的原始程序字节。')
        text = candidate.decode('utf-8')
        storage._publish(self._path(record['id']) / 'candidate.py', candidate)
        record.update(candidate_sha256=storage._hash(candidate), candidate_source=source,
                      candidate_preview=text)

    @serialized
    def remote_recover(self, key):
        from tools.workbench_remote import RemoteAdapter

        record = self._load(key, public_only=True)
        receipt = record.get('generation_receipt') or {}
        recovery = record.get('remote_recovery')
        require(receipt.get('source') == 'remote_node' and receipt.get('task_id')
                and (record['status'] == 'GENERATION_UNKNOWN_BLOCKED' or
                     (record['status'] == 'CANDIDATE_READY' and recovery)),
                'remote_recovery_unavailable', '当前任务没有可取回的原节点程序。')
        if 'remote_submission_id' in record:
            require(record['remote_submission_id'] == key == receipt['task_id'],
                    'remote_receipt_changed', '节点回执必须属于本任务的原始提交。')
        require(receipt.get('bundle_sha256') == record['bundle_sha256']
                and record['approved_proposal_sha256'] == record['proposal_sha256']
                and storage._read(self._path(key) / 'public-bundle.json')
                    == storage._bytes(record['public_bundle']),
                'bundle_changed', '已批准的公开材料发生变化。')
        lifecycle = getattr(self, 'lifecycle', None)
        if lifecycle:
            lifecycle.guard_remote_recovery(key)
        if recovery:
            candidate = storage._read(self._path(key) / 'candidate.py')
            require(record['candidate_sha256'] == recovery['candidate_sha256'] == storage._hash(candidate),
                    'candidate_changed', '原始程序已改变。')
        else:
            connector = self.external
            require(isinstance(connector, RemoteAdapter) and route_identity(connector) == record['external_route'],
                    'route_changed', '请按原连接设置重新连接同一运行节点。')
            candidate, recovery = connector.recover_candidate(receipt)

        # Cancellation may arrive while the two GET requests are in progress.
        with lifecycle.lock if lifecycle else nullcontext():
            if lifecycle:
                lifecycle.guard_remote_recovery(key)
            if record['status'] == 'GENERATION_UNKNOWN_BLOCKED':
                self._save_candidate(record, candidate, 'remote_node')
                record['remote_recovery'] = recovery
                self._event(record, 'CANDIDATE_READY', 'remote_candidate_recovered')
            if lifecycle and (record['events'][-1]['kind'] == 'remote_candidate_recovered'
                    and lifecycle.view(key)['state'] == 'UNKNOWN_BLOCKED'):
                lifecycle.finish_remote_recovery(key)
        return self.get(key)

    @serialized
    def run(self, key, candidate_sha256):
        record = self._load(key)
        require(record['status'] == 'CANDIDATE_READY' and candidate_sha256 == record['candidate_sha256'],
                'candidate_confirmation_required', '程序已改变或已执行；需确认当前原始程序摘要。')
        public_test=self._public_test_summary(record)
        require(public_test is None or public_test['status']=='PASSED',
            'public_test_not_passed','当前公开测试尚未通过，已阻止此程序进入内部执行。')
        # Current existing worker is explicitly not a hostile-code sandbox.
        require(record['candidate_source'] == 'local_test_double' or self._execution_allowed(),
                'synthetic_execution_authorization_required', '真实外部程序尚缺明确的合成数据执行风险授权，已阻止执行。')
        candidate = storage._read(self._path(key) / 'candidate.py')
        require(storage._hash(candidate) == candidate_sha256, 'candidate_changed', '原始程序已改变。')
        private = self._execution_private(record)
        scoped = 'task_binding_sha256' in record
        frame = self._execution_frame(private, scoped)
        run_id = str(uuid.uuid4())
        record['run_id'] = record['submitted_run_id'] = run_id
        record['execution_request_sha256'] = self._request_identity(private, scoped)
        self._event(record, 'EXECUTION_UNKNOWN_BLOCKED', 'execution_started')
        result = analysis.run_candidate(frame, candidate, record['public_bundle']['output'],
            private['binding'], self.root / 'runs' / run_id, source=record['candidate_source'],
            bundle_sha256=record['bundle_sha256'])
        record['result'] = result
        self._event(record, result['status'], 'execution_observed')
        return result

    @serialized
    def reuse(self, key, frame):
        """New run of the exact previous candidate against a compatible local snapshot."""
        original = self._load(key)
        require(original['status'] in {'EXECUTED_UNVERIFIED', 'INTERPRETED_UNVERIFIED', 'EXECUTION_FAILED'},
                'reuse_unavailable', '仅可复用已有明确执行终态的程序；未知状态不会自动重试。')
        self._associated_result(original)
        public_test=self._public_test_summary(original)
        require(public_test is None or public_test['status']=='PASSED',
                'public_test_not_passed','原程序的公开测试失败或状态未知，不能通过复用绕过；未创建新任务或执行。')
        analysis._frame(frame)
        prior_private = self._execution_private(original)
        private = {'task': prior_private['task'], 'records': frame.to_dict('records'),
                   'columns': list(frame), 'binding': prior_private['binding']}
        # Only classify rejection of this new input. Historical validation above
        # and malformed/stale binding failures retain their integrity error path.
        try:
            self._request_identity(private, 'task_binding_sha256' in original)
        except BindingRejected as error:
            if str(error) in {'unknown_enum_value', 'missing_declared_column',
                    'dtype_mismatch', 'dtype_reissue_required', 'unsupported_input',
                    'transport_not_lossless'}:
                raise AgentError('reuse_input_incompatible',
                    '新输入与原绑定不兼容，请创建新任务；未执行，未调用模型。') from None
            raise
        private['input_scope']=normalize_input_scope(frame,{'columns':list(private['binding']['mapping']['fields'])})
        require(original['candidate_source'] == 'local_test_double' or self._execution_allowed(),
                'synthetic_execution_authorization_required', '缺少本次合成数据执行风险授权。')
        candidate = storage._read(self._path(key) / 'candidate.py')
        public_raw = storage._read(self._path(key) / 'public-bundle.json')
        require(storage._hash(candidate) == original['candidate_sha256'] and
                public_raw == storage._bytes(original['public_bundle']),
                'candidate_changed', '原始程序或批准的公开包已改变。')
        new_key = str(uuid.uuid4())
        self._path(new_key).mkdir()
        storage._publish(self._path(new_key) / 'private.json', storage._bytes(private))
        storage._publish(self._path(new_key) / 'candidate.py', candidate)
        storage._publish(self._path(new_key) / 'public-bundle.json', public_raw)
        record = {name: copy.deepcopy(original[name]) for name in
            ('source', 'external_route', 'internal_route', 'public_bundle', 'bundle_sha256',
             'public_preview', 'candidate_sha256', 'candidate_source', 'candidate_preview', 'generation_receipt')}
        record.update(id=new_key, proposal_id=new_key, events=[], status='CANDIDATE_READY',
                      result=None, interpretation=None, private_sha256=digest(private),
                      reused_from=key, reused_approval_sha256=original['proposal_sha256'])
        if 'external_selection' in original:
            record['external_selection']=copy.deepcopy(original['external_selection'])
        if 'task_binding_sha256' in original:
            self._pin_binding(record, private, private['binding'])
        self._lineage(record,original,'reuse')
        record['proposal_sha256'] = self._proposal_digest(record)
        self._event(record, 'CANDIDATE_READY', 'original_candidate_reused')
        self.run(new_key, record['candidate_sha256'])
        return self.get(new_key)

    def _execution_frame(self, private, scoped):
        import pandas as pd
        columns = private['columns']
        if scoped:
            declared = private['binding']['mapping']['fields']
            if not set(declared).issubset(columns):
                raise BindingRejected('missing_declared_column')
            columns = sorted(declared, key=declared.get)
        return pd.DataFrame(private['records']).loc[:, columns]

    def _request_identity(self, private, scoped=False):
        frame = self._execution_frame(private, scoped)
        binding = private['binding']
        records, receipt = bind_records(frame, binding, public_schema(binding),
            analysis.NAMESPACE, analysis.SCOPE, expected_binding_digest=binding['digest'])
        return digest({'records': records, 'binding_receipt': receipt})

    @recovery_read
    def _associated_result(self, record):
        root = self.root / 'runs' / _id(record['run_id'])
        private = self._execution_private(record)
        expected_request = self._request_identity(private, 'task_binding_sha256' in record)
        manifest = storage._json(root / 'manifest.json')
        require(record['run_id'] == record['submitted_run_id'] == manifest['run_id'] and
                expected_request == record['execution_request_sha256'] == manifest['request_sha256'] and
                digest(private['binding']) == manifest['binding_sha256'] and
                digest(record['public_bundle']['output']) == manifest['contract_sha256'] and
                record['candidate_sha256'] == manifest['candidate_sha256'] and
                record['bundle_sha256'] == manifest['bundle_sha256'] and
                record['candidate_source'] == manifest['source'],
                'execution_association_changed', '执行记录与当前任务、原始输入或候选不匹配。')
        result = analysis.recover_candidate(root)
        require(result['run_id'] == record['run_id'] and
                result['candidate_sha256'] == record['candidate_sha256'] and
                result['bundle_sha256'] == record['bundle_sha256'] and
                result['binding_digest'] == private['binding']['digest'],
                'execution_association_changed', '执行结果不属于当前任务。')
        return result

    @serialized
    def recover(self, key):
        record = self._load(key)
        require('run_id' in record, 'run_missing', '任务尚未启动执行。')
        return self._associated_result(record)

    @serialized
    def interpret(self, key, run_id):
        if self.enterprise_actor is not None:
            context=self.enterprise_result_context(key,self.enterprise_actor)
            require(context['run_id']==run_id,'interpretation_identity_changed','结果运行编号不匹配。')
            record=self._load(key)
            self._event(record,'AWAITING_INTERNAL_INTERPRETATION','enterprise_result_prepared')
            return self.get(key)
        connector = self._connector('internal')
        record = self._load(key)
        require(record.get('run_id') == run_id and record.get('interpretation') is None and
                record['status'] == 'EXECUTED_UNVERIFIED',
                'interpretation_not_available', '需要本任务尚未解释的实际成功执行结果。')
        require(route_identity(connector) == record['internal_route'],
                'internal_route_changed', '内侧解释路线与原任务批准的路线不同；未发送任务或结果。')
        result = self.recover(key)
        require(result['status'] == 'EXECUTED_UNVERIFIED',
                'interpretation_not_available', '当前结果不可解释。')
        private = storage._json(self._path(key) / 'private.json')
        require(private.get('input_scope',{}).get('allow_result_rows') is True,
                'result_scope_required','结果行仅在本机查看。')
        self._event(record, 'INTERPRETATION_UNKNOWN_BLOCKED', 'internal_interpretation_started')
        try:
            response = self._call_connector(connector, {'purpose': 'interpret', 'task': private['task'],
                'result': result, 'result_sha256': digest(result),
                'instructions': 'Return only {text: string}. Explain actual result and limitations; correctness is not independently evaluated.'})
            require(type(response) is dict and set(response) == {'text'} and
                    type(response['text']) is str and 1 <= len(response['text']) <= 12000,
                    'invalid_interpretation', '内侧解释格式不完整。')
            record['interpretation_receipt']=copy.deepcopy(getattr(connector,'last_receipt',None))
            record['interpretation'] = {'text': response['text'], 'run_id': run_id,
                'result_sha256': digest(result), 'source': connector.source}
            self._event(record, 'INTERPRETED_UNVERIFIED', 'internal_interpretation_received')
        except Exception:
            self._event(record, 'INTERPRETATION_UNKNOWN_BLOCKED', 'internal_interpretation_unresolved')
            raise
        return self.get(key)
