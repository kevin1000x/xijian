"""Synthetic stdio peers; these tests never launch a real Agent or provider."""
import hashlib
import json
import sys

import pytest

from tools.workbench_acp import ACPAdapter, ACPTransport
from tools.workbench_budget import TokenBudget
from tools.workbench_connectors import ConnectorError
from tools import workbench_acp


PEER = r'''
import json, sys
for line in sys.stdin:
    m=json.loads(line)
    if m.get('method')=='initialize':
        result={'protocolVersion':1,'agentCapabilities':{},'authMethods':[]}
    elif m.get('method')=='session/new': result={'sessionId':'synthetic'}
    elif m.get('method')=='session/prompt': result={'stopReason':'end_turn'}
    else: continue
    print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''


def test_initialize_and_fresh_session(tmp_path):
    with ACPTransport([sys.executable, '-I', '-u', '-c', PEER], tmp_path, timeout=3) as client:
        assert client.initialize()['protocolVersion'] == 1
        assert client.request('session/new', {'cwd':str(tmp_path),'mcpServers':[]})['sessionId'] == 'synthetic'


@pytest.mark.parametrize('reply,code', [
    ("print('no JSON',flush=True)", 'acp_protocol'),
    ("print('{\"jsonrpc\":\"2.0\",\"id\":999,\"result\":{}}',flush=True)", 'acp_response_id'),
    ("print('x'*200000,flush=True)", 'acp_output_limit'),
    ("import time; time.sleep(10)", 'acp_timeout'),
    ("print('{\"jsonrpc\":\"2.0\",\"id\":1,\"error\":{\"code\":-32000,\"message\":\"secret\"}}',flush=True)", 'acp_auth_required'),
])
def test_protocol_failure_is_bounded_and_redacted(tmp_path, reply, code):
    with ACPTransport([sys.executable, '-I', '-u', '-c', 'import sys; sys.stdin.readline(); '+reply], tmp_path, timeout=.3) as client:
        with pytest.raises(ConnectorError) as caught:
            client.initialize()
        assert caught.value.code == code
        assert 'secret' not in str(caught.value)
    assert client.process.poll() is not None


def test_unknown_runtime_cannot_probe_or_generate(tmp_path):
    profile={'executable':sys.executable, 'executable_sha256':hashlib.sha256(open(sys.executable,'rb').read()).hexdigest(),
             'agent_kind':'custom','authorized':True,'data_scope':'public_bundle_only','allocation':100,'max_invocations':1}
    budget=TokenBudget(tmp_path/'budget')
    adapter=ACPAdapter(tmp_path/'adapter',budget,profile)
    assert not adapter.readiness()['ready']
    assert adapter.probe()['model_requests'] == 0
    with pytest.raises(ConnectorError): adapter.call({'synthetic':True})
    assert budget.snapshot()['charged'] == 0


def test_filesystem_confined_and_permission_cancelled(tmp_path):
    (tmp_path/'public-bundle.json').write_text('{}')
    with ACPTransport([sys.executable,'-I','-u','-c',PEER],tmp_path) as client:
        client.session_id='synthetic'
        assert client.client_request('fs/read_text_file',{'sessionId':'synthetic','path':str(tmp_path/'public-bundle.json')}) == {'content':'{}'}
        for path in [str(tmp_path.parent/'outside'),str(tmp_path/'other.txt')]:
            with pytest.raises(ConnectorError): client.client_request('fs/read_text_file',{'sessionId':'synthetic','path':path})
        assert client.client_request('session/request_permission',{'sessionId':'synthetic'}) == {'outcome':{'outcome':'cancelled'}}
        with pytest.raises(ConnectorError): client.client_request('terminal/create',{'sessionId':'synthetic'})
        with pytest.raises(ConnectorError): client.client_request('fs/write_text_file',{'sessionId':'wrong','path':str(tmp_path/'analysis.py'),'content':'bad'})


def test_cancel_terminates_without_retry(tmp_path):
    with ACPTransport([sys.executable,'-I','-u','-c','import time; time.sleep(20)'],tmp_path) as client:
        client.cancel()
        with pytest.raises(ConnectorError) as caught: client.initialize()
        assert caught.value.code == 'acp_cancelled'
    assert client.process.poll() is not None


def test_text_candidate_preserves_utf8_chunks_and_ignores_thoughts(tmp_path):
    peer = PEER.replace("elif m.get('method')=='session/prompt': result={'stopReason':'end_turn'}", """elif m.get('method')=='session/prompt':
        for kind, text in [('agent_thought_chunk','private reasoning'), ('agent_message_chunk','def analyze(df):\\r\\n'), ('agent_message_chunk','    return df.copy()\\r\\n')]:
            print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'synthetic','update':{'sessionUpdate':kind,'content':{'type':'text','text':text}}}}), flush=True)
        result={'stopReason':'end_turn'}""")
    with ACPTransport([sys.executable, '-I', '-u', '-c', peer], tmp_path) as client:
        client.initialize()
        client.session_id = client.request('session/new', {})['sessionId']
        client.request('session/prompt', {'sessionId':client.session_id, 'prompt':[]})
        assert client.candidate_text() == b'def analyze(df):\r\n    return df.copy()\r\n'


def test_text_candidate_is_bounded_and_rejects_nontext_content(tmp_path):
    with ACPTransport([sys.executable, '-I', '-u', '-c', PEER], tmp_path) as client:
        client.session_id = 'synthetic'
        with pytest.raises(ConnectorError) as caught:
            client._session_update({'sessionId':'synthetic','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'x' * (workbench_acp.MAX_FILE + 1)}}})
        assert caught.value.code == 'acp_file'
        with pytest.raises(ConnectorError):
            client._session_update({'sessionId':'synthetic','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'resource','resource':{}}}})


def test_text_policy_delivers_public_bundle_without_file_tools(tmp_path, monkeypatch):
    candidate = 'def analyze(df):\r\n    return df.copy()\r\n'
    peer = PEER.replace("elif m.get('method')=='session/prompt': result={'stopReason':'end_turn'}", """elif m.get('method')=='session/prompt':
        assert 'SYNTHETIC_PUBLIC_MARKER' in m['params']['prompt'][0]['text']
        print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'synthetic','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':%s}}}}), flush=True)
        result={'stopReason':'end_turn'}""" % repr(candidate))
    digest = hashlib.sha256(open(sys.executable, 'rb').read()).hexdigest()
    monkeypatch.setitem(workbench_acp.REVIEWED_RUNTIMES, ('synthetic','text',digest),
        {'args':['-I','-u','-c',peer], 'environment':lambda workspace:None, 'candidate_mode':'text'})
    profile = dict(executable=sys.executable, executable_sha256=digest, agent_kind='synthetic', version='text',
        authorized=True, data_scope='public_bundle_only', allocation=100, max_invocations=1)
    adapter = ACPAdapter(tmp_path/'records', TokenBudget(tmp_path/'budget'), profile)
    adapter.probe()
    assert adapter.call({'synthetic':'SYNTHETIC_PUBLIC_MARKER'}) == candidate.encode('utf-8')


@pytest.mark.parametrize('notice_session', ['synthetic', 'wrong'])
def test_new_session_correlates_early_native_updates(tmp_path, notice_session):
    peer = PEER.replace("elif m.get('method')=='session/new': result={'sessionId':'synthetic'}", """elif m.get('method')=='session/new':
        print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':%s,'update':{'sessionUpdate':'available_commands_update','availableCommands':[]}}}), flush=True)
        result={'sessionId':'synthetic'}""" % repr(notice_session))
    with ACPTransport([sys.executable, '-I', '-u', '-c', peer], tmp_path) as client:
        client.initialize()
        if notice_session == 'wrong':
            with pytest.raises(ConnectorError) as caught: client.request('session/new', {})
            assert caught.value.code == 'acp_notification'
        else:
            assert client.request('session/new', {})['sessionId'] == 'synthetic'


@pytest.mark.parametrize('stop,expected', [('end_turn','complete'),('cancelled','unresolved')])
def test_synthetic_adapter_delivery_and_unknown_budget(tmp_path, monkeypatch, stop, expected):
    candidate=b'def analyze(df):\r\n    return df.copy()\r\n'
    peer=PEER.replace("elif m.get('method')=='session/prompt': result={'stopReason':'end_turn'}",
        "elif m.get('method')=='session/prompt':\n        open('analysis.py','wb').write("+repr(candidate)+")\n        result={'stopReason':"+repr(stop)+"}")
    digest=hashlib.sha256(open(sys.executable,'rb').read()).hexdigest()
    monkeypatch.setitem(workbench_acp.REVIEWED_RUNTIMES, ('synthetic','test',digest),
        {'args':['-I','-u','-c',peer], 'environment':lambda workspace:None})
    profile={'executable':sys.executable,'executable_sha256':digest,'agent_kind':'synthetic','version':'test',
             'authorized':True,'data_scope':'public_bundle_only','allocation':100,'max_invocations':1,'timeout_seconds':3}
    budget=TokenBudget(tmp_path/'budget')
    adapter=ACPAdapter(tmp_path/'adapter',budget,profile)
    assert adapter.probe()['status']=='protocol_checked'
    assert adapter.readiness()['ready']
    if stop=='end_turn': assert adapter.call({'synthetic':True}) == candidate
    else:
        with pytest.raises(ConnectorError): adapter.call({'synthetic':True})
    assert adapter.last_receipt['status']==expected
    assert budget.snapshot()['charged']==100
    assert budget.snapshot()['unknown_operations']==1
    assert not adapter.readiness()['ready']
    assert adapter.operation() is None
    restarted=ACPAdapter(tmp_path/'adapter',budget,profile)
    restarted.probe()
    assert 'invocation_limit' in restarted.readiness()['missing']
