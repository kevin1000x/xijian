import json
import pytest
from tools import workbench_connectors as c
from tools.workbench_budget import TokenBudget

PUBLIC = dict(version=1, objective='Calculate public total', roles=[], schema={}, examples=[], output={}, program_contract='Return original Python source only')
SOURCE = '# original CRLF\r\ndef analyze(df):\r\n    return {"rows": []}\r\n'

def profile(protocol):
    return dict(route_id='loopback', account_id='synthetic', model='test-model', approval_id='offline-test', authorized=True, data_scope='public_bundle_only', max_invocations=1, max_output_tokens=256, timeout_seconds=5, max_response_bytes=65536, max_total_tokens=10000000, url='http://127.0.0.1:1/test', protocol=protocol)

def verifier(p):
    return dict(profile_sha256=c.digest(p), launch_boundary=True, budget_enforced=True, data_boundary=True)

def response(protocol, text):
    if protocol == 'gemini_generate_content':
        return {'modelVersion': 'test-model', 'candidates': [{'finishReason': 'STOP', 'content': {
            'role': 'model', 'parts': [{'text': 'omitted summary', 'thought': True},
                                       {'text': text[:12]}, {'text': text[12:]}]}}],
            'usageMetadata': {'promptTokenCount': 20, 'candidatesTokenCount': 25,
                              'thoughtsTokenCount': 5, 'totalTokenCount': 50}}
    common=dict(model='test-model')
    if protocol=='openai_chat':
        common.update(choices=[dict(finish_reason='stop',message=dict(role='assistant',content=text))], usage=dict(prompt_tokens=20,completion_tokens=30,total_tokens=50))
    elif protocol=='openai_responses':
        common.update(status='completed',output=[dict(type='message',role='assistant',status='completed',content=[dict(type='output_text',text=text)])],usage=dict(input_tokens=20,output_tokens=30,total_tokens=50))
    else:
        common.update(type='message',role='assistant',stop_reason='end_turn',content=[dict(type='text',text=text)],usage=dict(input_tokens=20,output_tokens=30))
    return common

@pytest.mark.parametrize('protocol', sorted(c.PROTOCOLS | {'gemini_generate_content'}))
def test_external_native_protocol_retains_original_source_and_shared_budget(tmp_path,monkeypatch,protocol):
    calls=[]
    class Process:
        returncode=0
        def __init__(self,*a,**k): pass
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def communicate(self,input,timeout):
            calls.append(json.loads(input))
            return json.dumps(response(protocol,SOURCE)).encode(),None
    monkeypatch.setattr(c.subprocess,'Popen',Process)
    budget=TokenBudget(tmp_path/'budget')
    adapter=c.ExternalHTTPAdapter(profile(protocol),token='synthetic-token',verifier=verifier,budget=budget)
    assert adapter.readiness()['ready']
    assert adapter.call(PUBLIC)==SOURCE.encode('utf-8')
    body=json.loads(calls[0]['body'])
    if protocol == 'gemini_generate_content':
        text = body['contents'][0]['parts'][0]['text']
        assert body['generationConfig'] == {'candidateCount': 1, 'maxOutputTokens': 256}
        assert set(body) == {'contents', 'generationConfig'}
        assert calls[0]['headers'] == {'Content-Type': 'application/json', 'x-goog-api-key': 'synthetic-token'}
        assert adapter.last_receipt['usage'] == {'input_tokens': 20, 'output_tokens': 30, 'total_tokens': 50}
    else:
        text=body['input'][0]['content'][0]['text'] if protocol=='openai_responses' else body['messages'][0]['content']
    assert json.loads(text)==PUBLIC
    assert adapter.last_receipt['source']=='external_http'
    assert budget.snapshot()['charged']==50
    with pytest.raises(c.ConnectorError): adapter.call(PUBLIC)
    assert len(calls)==1


@pytest.mark.parametrize('protocol,base,path', [
    ('openai_responses', 'https://api.invalid/', '/v1/responses'),
    ('openai_chat', 'https://api.invalid/gateway/v2/coding/', '/gateway/v2/coding/chat/completions'),
    ('anthropic_messages', 'https://api.invalid/v1', '/v1/messages'),
    ('gemini_generate_content', 'https://api.invalid', '/v1beta/models/test-model:generateContent'),
    ('gemini_generate_content', 'https://api.invalid/gemini/v1', '/gemini/v1/models/test-model:generateContent'),
])
def test_external_base_url_resolves_exact_endpoint(protocol, base, path):
    assert c.external_endpoint(protocol, base, 'test-model', 'base_url') == 'https://api.invalid' + path
    assert c.external_endpoint(protocol, 'https://api.invalid' + path, 'test-model', 'full_url') == 'https://api.invalid' + path
    custom = 'https://api.invalid/custom/session'
    assert c.external_endpoint(protocol, custom, 'test-model', 'full_url') == custom


@pytest.mark.parametrize('url,model,mode', [
    ('https://api.invalid/v1beta/models/wrong-model:generateContent', 'test-model', 'full_url'),
    ('https://api.invalid/v1beta/models/wrong-model%3AgenerateContent', 'test-model', 'full_url'),
    ('https://api.invalid/v1beta/models/test-model%3AgenerateContent', 'test-model', 'full_url'),
    ('https://api.invalid/v1beta/models/test-model:streamGenerateContent', 'test-model', 'full_url'),
    ('https://api.invalid/v1beta/models/test-model:generateContent', 'test-model', 'base_url'),
    ('https://api.invalid/v1beta', 'wrong/path', 'base_url'),
    ('https://api.invalid/v1beta?key=secret', 'test-model', 'base_url'),
    ('http://remote.invalid/v1beta', 'test-model', 'base_url'),
    ([], 'test-model', 'full_url'),
    ('https://api.invalid:bad', 'test-model', 'base_url'),
    ('https://api.invalid/v1beta', 'test-model', 'automatic'),
])
def test_gemini_endpoint_rejects_unpinned_or_unsafe_config(url, model, mode):
    with pytest.raises(c.ConnectorError):
        c.external_endpoint('gemini_generate_content', url, model, mode)


@pytest.mark.parametrize('mutation', [
    lambda data: data.update(modelVersion='unexpected-model'),
    lambda data: data.update(promptFeedback={'blockReason': 'SAFETY'}),
    lambda data: data['candidates'][0].update(finishReason='MAX_TOKENS'),
    lambda data: data['candidates'][0]['content']['parts'].append({'functionCall': {'name': 'tool'}}),
    lambda data: data['candidates'][0]['content']['parts'].append({'thought': True, 'functionCall': {}}),
])
def test_gemini_rejects_unexpected_model_or_nonfinal_code(mutation):
    protocol = 'gemini_generate_content'
    result = response(protocol, SOURCE)
    mutation(result)
    with pytest.raises(c.ConnectorError):
        c.ExternalHTTPAdapter(profile(protocol))._parse(result)


def test_gemini_version_alias_and_missing_total_are_explicit():
    protocol = 'gemini_generate_content'
    result = response(protocol, SOURCE)
    result['modelVersion'] = 'test-model-001'
    del result['usageMetadata']['totalTokenCount']
    p = dict(profile(protocol), model_aliases=['test-model-001'])
    raw, receipt = c.ExternalHTTPAdapter(p)._parse(result)
    assert raw == SOURCE.encode('utf-8')
    assert receipt['observed_model'] == 'test-model-001'
    assert receipt['usage'] is None

@pytest.mark.parametrize('bad', [dict(PUBLIC,task='PRIVATE_TASK'), dict(PUBLIC,private={}), dict(PUBLIC,objective='x'*65537)])
def test_public_adapter_rejects_nonbundle_or_oversize_before_send(tmp_path,monkeypatch,bad):
    monkeypatch.setattr(c.subprocess,'Popen',lambda *a,**k: pytest.fail('must not send'))
    budget=TokenBudget(tmp_path/'budget')
    adapter=c.ExternalHTTPAdapter(profile('openai_chat'),token='synthetic',verifier=verifier,budget=budget)
    with pytest.raises(c.ConnectorError): adapter.call(bad)
    assert budget.snapshot()['charged']==0
    assert adapter.used==0

@pytest.mark.parametrize('bad', ['', '```python\ndef analyze(df): pass\n```', 'x'*65537], ids=['empty','fenced','oversize'])
def test_source_response_is_not_rewritten_or_truncated(bad):
    adapter=c.ExternalHTTPAdapter(profile('openai_chat'))
    with pytest.raises(c.ConnectorError): adapter._parse(response('openai_chat',bad))

def test_public_adapter_rejects_internal_data_authorization(tmp_path):
    p=profile('openai_chat'); p['data_scope']='synthetic_internal_only'
    adapter=c.ExternalHTTPAdapter(p,token='synthetic',verifier=verifier,budget=TokenBudget(tmp_path/'budget'))
    assert not adapter.readiness()['ready']


@pytest.mark.parametrize('protocol', sorted(c.PROTOCOLS))
def test_actual_loopback_worker_delivers_public_source(tmp_path,protocol):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            raw=json.dumps(response(protocol,SOURCE)).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    p=profile(protocol);p['url']='http://127.0.0.1:%d/api'%server.server_port
    adapter=c.ExternalHTTPAdapter(p,token='synthetic-only',verifier=verifier,budget=TokenBudget(tmp_path/'budget'))
    try:
        assert adapter.call(PUBLIC)==SOURCE.encode()
        assert len(calls)==1
        assert adapter.operation() is None
        assert adapter.last_receipt['operation_id'].startswith('external-')
        assert adapter.last_receipt['observed_model']=='test-model'
    finally:
        server.shutdown();server.server_close();thread.join(3)


def test_external_failure_is_labelled_external_and_never_retried(tmp_path,monkeypatch):
    class Failed:
        returncode=2
        def __init__(self,*a,**k):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def communicate(self,**k):return b'',None
    monkeypatch.setattr(c.subprocess,'Popen',Failed)
    budget=TokenBudget(tmp_path/'budget')
    adapter=c.ExternalHTTPAdapter(profile('openai_chat'),token='synthetic',verifier=verifier,budget=budget)
    with pytest.raises(c.ConnectorError) as error:adapter.call(PUBLIC)
    assert error.value.code=='external_request_unresolved'
    assert budget.snapshot()['charged']==adapter.last_receipt['reserved_tokens']
    assert adapter.operation() is None
    with pytest.raises(c.ConnectorError):adapter.call(PUBLIC)


@pytest.mark.parametrize('protocol', ['openai_responses', 'gemini_generate_content'])
def test_second_adapter_full_publication_public_test_and_internal_execution(tmp_path, protocol):
    import threading
    import pandas as pd
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from tools.workbench_agent import AgentWorkbench
    good='def analyze(df):\n    return {"rows":[{"squared":int((df["COL_0001"]**2).sum())}]}\n'
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            calls.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            raw=json.dumps(response(protocol,good)).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    class Internal:
        source='local_test_double'
        def readiness(self):return dict(ready=True,live=False,source=self.source,missing=[])
        def call(self,payload):
            return dict(objective='Compute squared total',roles=[dict(column='COL_0001',meaning='amount')],output=dict(columns=[dict(key='squared',label='Squared total',type='integer',nullable=False,restore_from=None)],max_rows=10))
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    p=profile(protocol)
    p['url']=c.external_endpoint(protocol, 'http://127.0.0.1:%d'%server.server_port, p['model'], 'base_url')
    external=c.ExternalHTTPAdapter(p,token='synthetic-only',verifier=verifier,budget=TokenBudget(tmp_path/'budget'))
    service=AgentWorkbench(tmp_path/'agent',internal=Internal(),external=external,execution_authorization=dict(authorized=True,data_scope='synthetic_only',risk_acknowledgement='existing_supervisor_not_hostile_isolation'))
    try:
        task=service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X':[4,6]}),'PRIVATE_TASK_X')
        assert task['public_bundle']['program_contract'].startswith('Return only original UTF-8 Python source')
        service.publish(task['id'],task['proposal_sha256'])
        ready=service.generate(task['id'],task['bundle_sha256'])
        assert ready['candidate_source']=='external_http'
        assert service.public_test(task['id'],ready['candidate_sha256'])['public_test']['status']=='PASSED'
        result=service.run(task['id'],ready['candidate_sha256'])
        assert result['rows']==[{'squared':52}] and result['source']=='external_http'
        assert service.reuse(task['id'], pd.DataFrame({'PRIVATE_AMOUNT_X':[2,4]}))['result']['rows']==[{'squared':20}]
        assert len(calls)==1 and 'PRIVATE_' not in json.dumps(calls)
        if protocol == 'gemini_generate_content':
            assert calls[0][0] == '/v1beta/models/test-model:generateContent'
        assert (service._path(task['id'])/'candidate.py').read_bytes()==good.encode()
    finally:
        server.shutdown();server.server_close();thread.join(3)
