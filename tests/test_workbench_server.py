import base64
import io
import json
import threading
from http.client import HTTPConnection

import pytest

from tools.workbench_server import InputError, Store, make_server, parse_table


def test_import_preserves_headers_and_leading_zero_categories():
    frame = parse_table({'format': 'tsv', 'text': '渠道\t金额\t退款\n007\t12\t2\n线上\t8\t0\n'})
    assert list(frame.columns) == ['渠道', '金额', '退款']
    assert frame['渠道'].tolist() == ['007', '线上']
    assert str(frame['金额'].dtype) == 'int64'


@pytest.mark.parametrize('text', ['a,a\n1,2', 'a,b\n1,', 'a,b\n1,2,3', 'a,b\n'])
def test_invalid_table_explains_rejection(text):
    with pytest.raises(InputError):
        parse_table({'format': 'csv', 'text': text})


def test_xlsx_formula_rejected_instead_of_silently_using_cached_value():
    import openpyxl
    book = openpyxl.Workbook()
    book.active.append(['渠道', '金额'])
    book.active.append(['直营', '=2+3'])
    stream = io.BytesIO()
    book.save(stream)
    with pytest.raises(InputError, match='公式'):
        parse_table({'format': 'xlsx', 'base64': base64.b64encode(stream.getvalue()).decode()})


def test_xlsx_values_and_first_visible_sheet():
    import openpyxl
    book = openpyxl.Workbook()
    book.active.sheet_state = 'hidden'
    sheet = book.create_sheet('数据')
    sheet.append(['渠道', '金额'])
    sheet.append(['007', 12.5])
    stream = io.BytesIO()
    book.save(stream)
    frame = parse_table({'format': 'xlsx', 'base64': base64.b64encode(stream.getvalue()).decode()})
    assert frame.iloc[0].tolist() == ['007', 12.5]


def test_csv_export_neutralizes_formula_text_and_keeps_exact_integers():
    from tools.workbench_server import export_csv, _wire
    result = {'status': 'EXECUTED_UNVERIFIED', 'columns': [{'key': 'g0', 'label': '=标题'}, {'key': 'm1', 'label': '金额'}], 'rows': [{'g0': '=1+1', 'm1': 9223372036854775808}]}
    raw = export_csv(result).decode('utf-8-sig')
    assert "'=标题" in raw and "'=1+1" in raw and '9223372036854775808' in raw
    assert _wire(result)['rows'][0]['m1'] == '9223372036854775808'


@pytest.fixture
def service(tmp_path):
    server = make_server(Store(tmp_path), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(5)


def request(server, method, path, body=None, headers=None):
    client = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    client.request(method, path, body=body, headers=headers or {})
    response = client.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    client.close()
    return result


def test_localhost_host_origin_and_csrf(service):
    assert request(service, 'GET', '/', headers={'Host': 'evil.example'})[0] == 403
    status, headers, raw = request(service, 'GET', '/api/bootstrap')
    assert status == 200
    token = json.loads(raw)['csrf_token']
    assert 'Access-Control-Allow-Origin' not in headers
    assert request(service, 'POST', '/api/import', '{}', {'Content-Type': 'application/json'})[0] == 403
    auth = {'Content-Type': 'application/json', 'X-Workbench-Token': token, 'Origin': 'https://evil.example'}
    assert request(service, 'POST', '/api/import', '{}', auth)[0] == 403
    auth.pop('Origin')
    status, _, raw = request(service, 'POST', '/api/import', json.dumps({'format': 'csv', 'name': 'test', 'text': '渠道,金额\n本地,5'}).encode(), auth)
    assert status == 200
    assert json.loads(raw)['columns'][0]['name'] == '渠道'


def test_no_arbitrary_file_or_unbounded_request(service):
    assert request(service, 'GET', '/../PROJECT_SPEC.md')[0] == 404
    assert request(service, 'GET', '/api/runs/../../PROJECT_SPEC.md')[0] == 400
    assert request(service, 'GET', '/')[1]['Content-Security-Policy'].find("connect-src 'self'") >= 0
    _, _, raw = request(service, 'GET', '/api/bootstrap')
    token = json.loads(raw)['csrf_token']
    headers = {'X-Workbench-Token': token, 'Content-Type': 'application/json', 'Content-Length': '5000000'}
    assert request(service, 'POST', '/api/import', b'', headers)[0] == 400


def test_saved_plan_survives_restart_without_saving_dataset(tmp_path):
    store = Store(tmp_path)
    data = store.import_table({'format': 'csv', 'text': '渠道,金额\n本地,5'})
    spec = {'version': 1, 'group_by': ['渠道'], 'metrics': [{'id': 'm1', 'op': 'count', 'column': None, 'label': '笔数'}]}
    saved = store.save_plan({'name': '复用方案', 'spec': spec, 'dataset_id': data['dataset_id']})
    assert saved['spec']['filters'] == saved['spec']['derived'] == saved['spec']['sort'] == []
    assert saved['spec']['limit'] is None
    reopened = Store(tmp_path)
    assert reopened.plans() == [saved]
    assert reopened.datasets == {}


def test_launcher_rejects_nonlocal_saved_url(tmp_path):
    from start_workbench import existing_url
    (tmp_path / 'server.json').write_text(json.dumps({'url': 'https://example.com/'}))
    assert existing_url(tmp_path) is None


def test_preview_confirmation_is_bound_to_data_and_spec_and_consumed(tmp_path, monkeypatch):
    from tools import workbench_server as module
    store = Store(tmp_path)
    data = store.import_table({'name': 'demo', 'format': 'csv', 'text': '渠道,金额\n本地,5'})
    spec = {'version': 1, 'group_by': ['渠道'], 'metrics': [{'id': 'm1', 'op': 'sum', 'column': '金额', 'label': '合计'}]}
    preview = store.preview({'dataset_id': data['dataset_id'], 'spec': spec})
    changed = dict(spec, limit=1)
    with pytest.raises(InputError, match='确认'):
        store.run({'dataset_id': data['dataset_id'], 'spec': changed, 'confirmation': preview['confirmation']})
    monkeypatch.setattr(module, 'engine_run', lambda *args, **kw: {'status': 'EXECUTED_UNVERIFIED', 'correctness': 'NOT_EVALUATED', 'run_id': args[2].name, 'columns': [], 'rows': []})
    store.run({'dataset_id': data['dataset_id'], 'spec': spec, 'confirmation': preview['confirmation']})
    with pytest.raises(InputError, match='确认'):
        store.run({'dataset_id': data['dataset_id'], 'spec': spec, 'confirmation': preview['confirmation']})


def test_agent_default_readiness_blocked_and_draft_requires_configuration(service):
    status, _, raw = request(service, 'GET', '/api/agent/readiness')
    readiness = json.loads(raw)
    assert status == 200 and readiness['status'] == 'BLOCKED'
    assert readiness['internal']['ready'] is False and readiness['external']['ready'] is False
    _, _, bootstrap = request(service, 'GET', '/api/bootstrap')
    headers = {'Content-Type': 'application/json', 'X-Workbench-Token': json.loads(bootstrap)['csrf_token']}
    _, _, imported = request(service, 'POST', '/api/import',
        json.dumps({'format': 'csv', 'text': 'amount\n7\n9'}), headers)
    dataset_id = json.loads(imported)['dataset_id']
    status, _, raw = request(service, 'POST', '/api/agent/draft',
        json.dumps({'dataset_id': dataset_id, 'task': 'Sum squares'}), headers)
    assert status == 400 and json.loads(raw)['error']['code'] == 'connector_not_ready'
    assert json.loads(request(service, 'GET', '/api/agent/tasks')[2]) == []


def test_http_and_enterprise_configuration_never_echo_credentials(tmp_path):
    store=Store(tmp_path)
    result=store.configure_agent({'mode':'http','protocol':'anthropic_messages',
        'url':'https://internal.example.invalid/v1/messages','model':'company-model',
        'credential':'SYNTHETIC_SECRET_ONLY','authorized':True})
    assert 'SYNTHETIC_SECRET_ONLY' not in json.dumps(result)
    assert store.agent.internal.profile['protocol']=='anthropic_messages'
    assert not any(b'SYNTHETIC_SECRET_ONLY' in p.read_bytes() for p in tmp_path.rglob('*') if p.is_file())
    state=store.configure_agent({'mode':'enterprise_mcp','actor_id':'company-agent',
        'authorized':True,'host_budget_enforced':True,'host_token_reservation':2000})
    assert state['mode']=='enterprise_mcp'
    assert store.agent.internal is None and store.agent.enterprise_actor['source']=='enterprise_mcp'


def test_scoped_mcp_cannot_publish_or_read_other_task(tmp_path):
    from tools.workbench_mcp import protect_token
    store=Store(tmp_path)
    store.server_url='http://127.0.0.1:12345'
    store.configure_agent({'mode':'enterprise_mcp','actor_id':'company-agent','authorized':True,
        'host_budget_enforced':True,'host_token_reservation':2000})
    dataset=store.import_table({'format':'csv','text':'PRIVATE_AMOUNT_X\n7\n9'})
    p=store.agent_action('draft',{'dataset_id':dataset['dataset_id'],'task':'PRIVATE_TASK_X'})
    session=store.create_mcp_session({'proposal_id':p['id'],'dataset_id':dataset['dataset_id']})
    assert 'credential' not in session and 'token' not in session
    token=protect_token(__import__('pathlib').Path(session['credential_file']).read_bytes(),decrypt=True).decode()
    assert token not in json.dumps(session)
    args=(session['session_id'],token)
    context=store.mcp_action('get_task_context',{},*args)
    assert context['context']['records']==[{'PRIVATE_AMOUNT_X':7},{'PRIVATE_AMOUNT_X':9}]
    assert store.token_budget.snapshot()['charged']==2000
    with pytest.raises(InputError): store.mcp_action('publish',{},*args)
    with pytest.raises(InputError): store.mcp_action('get_task_context',{'proposal_id':'other'},*args)
    with pytest.raises(InputError): store.mcp_action('get_task_context',{},session['session_id'],'wrong')


def test_qoder_configuration_grants_only_ready_synthetic_execution(tmp_path, monkeypatch):
    import sys
    import types
    class Adapter:
        source='qoder'
        ready=True
        def __init__(self, root, budget, profile, token=None): self.profile=profile
        def probe(self): return {'catalog_verified':self.ready}
        def readiness(self): return {'ready':self.ready,'source':'qoder','missing':[]}
    monkeypatch.setitem(sys.modules,'tools.workbench_qoder',types.SimpleNamespace(QoderSDKAdapter=Adapter))
    store=Store(tmp_path)
    store.configure_agent({'mode':'qoder_sdk','authorized':True,'auth_mode':'local_login'})
    assert store.agent._execution_allowed()
    assert store.agent.execution_authorization['data_scope']=='synthetic_only'
    Adapter.ready=False
    store.configure_agent({'mode':'qoder_sdk','authorized':True,'auth_mode':'local_login'})
    assert not store.agent._execution_allowed()


def test_qoder_cannot_replace_active_connector_before_probe(tmp_path,monkeypatch):
    import sys,types
    store=Store(tmp_path)
    class Active:
        def operation(self):return 'running-op'
    original=Active();store.agent.external=original
    class Adapter:
        def __init__(self,*args,**kwargs):pytest.fail('must reject before new probe')
    monkeypatch.setitem(sys.modules,'tools.workbench_qoder',types.SimpleNamespace(QoderSDKAdapter=Adapter))
    with pytest.raises(InputError):
        store.configure_agent({'mode':'qoder_sdk','authorized':True})
    assert store.agent.external is original


def test_external_http_configuration_separate_scope_and_optional_revision(tmp_path):
    store=Store(tmp_path)
    class InternalFixture:
        def readiness(self):return {'ready':False,'live':False,'source':'local_test_double','missing':[]}
    internal=InternalFixture();store.agent.internal=internal
    result=store.configure_agent({'mode':'external_http','protocol':'openai_chat',
        'url':'https://external.invalid/v1/chat/completions','model':'synthetic-code-model',
        'credential':'SYNTHETIC_EXTERNAL_ONLY','authorized':True})
    assert store.agent.internal is internal
    assert store.agent.external.source=='external_http'
    assert store.agent.external.profile['data_scope']=='public_bundle_only'
    assert store.agent._execution_allowed()
    assert 'SYNTHETIC_EXTERNAL_ONLY' not in json.dumps(result)
    store.configure_agent({'mode':'enterprise_mcp','actor_id':'synthetic-client',
        'authorized':True,'host_budget_enforced':True,'host_token_reservation':1000})
    data=store.import_table({'format':'csv','text':'PRIVATE_AMOUNT_X\n2\n3'})
    parent=store.agent_action('draft',{'dataset_id':data['dataset_id'],'task':'PRIVATE_TASK_X'})
    revised=store.agent_action('revise',{'proposal_id':parent['id'],'task':'NEW_PRIVATE_TASK_X'})
    assert revised['parent_id']==parent['id']
    assert revised['status']=='AWAITING_INTERNAL_PROPOSAL'
    context=store.agent.enterprise_context(revised['id'],store.agent.enterprise_actor)
    assert context['context']['records']==[{'PRIVATE_AMOUNT_X':2},{'PRIVATE_AMOUNT_X':3}]


def test_m2_mcp_public_test_and_repair_never_approve_publication(tmp_path):
    from tools.workbench_mcp import protect_token
    from pathlib import Path
    class ExternalFixture:
        source='local_test_double'
        calls=0
        def readiness(self):return {'ready':True,'live':False,'missing':[],'source':self.source}
        def call(self,bundle):
            self.calls+=1
            return b'def analyze(df):\n    return {"rows":[{"wrong":1}]}\n'
    external=ExternalFixture()
    store=Store(tmp_path,agent_external=external)
    store.server_url='http://127.0.0.1:12345'
    store.configure_agent({'mode':'enterprise_mcp','actor_id':'synthetic-m2-client',
        'authorized':True,'host_budget_enforced':True,'host_token_reservation':1000})
    data=store.import_table({'format':'csv','text':'PRIVATE_AMOUNT_X\n7\n9'})
    parent=store.agent_action('draft',{'dataset_id':data['dataset_id'],'task':'PRIVATE_TASK_X'})
    session=store.create_mcp_session({'proposal_id':parent['id'],'dataset_id':data['dataset_id']})
    secret=protect_token(Path(session['credential_file']).read_bytes(),decrypt=True).decode()
    auth=(session['session_id'],secret)
    context=store.mcp_action('get_task_context',{},*auth)
    proposal={'objective':'Compute total','roles':[{'column':'COL_0001','meaning':'amount'}],
        'output':{'columns':[{'key':'total','label':'Total','type':'integer',
            'nullable':False,'restore_from':None}],'max_rows':10}}
    parent=store.mcp_action('submit_proposal',{'context_sha256':context['context_sha256'],'proposal':proposal},*auth)
    store.agent_action('publish',{'proposal_id':parent['id'],'proposal_sha256':parent['proposal_sha256']})
    parent=store.mcp_action('generate_program',{'bundle_sha256':parent['bundle_sha256']},*auth)
    tested=store.mcp_action('test_public_program',{'candidate_sha256':parent['candidate_sha256']},*auth)
    assert tested['public_test']['status']=='FAILED'
    child=store.mcp_action('prepare_repair',{'candidate_sha256':parent['candidate_sha256']},*auth)
    assert child['parent_id']==parent['id']
    assert store.mcp_action('get_task_status',{},*auth)['id']==child['id']
    from tools.workbench_agent import AgentError
    with pytest.raises(AgentError):
        store.mcp_action('generate_program',{'bundle_sha256':child['bundle_sha256']},*auth)
    with pytest.raises(InputError):store.mcp_action('publish',{'proposal_sha256':child['proposal_sha256']},*auth)
    assert external.calls==1
