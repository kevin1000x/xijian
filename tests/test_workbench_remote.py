import hashlib
import json
import threading
import time
import uuid

import pytest

from tools.workbench_budget import TokenBudget
from tools.workbench_connectors import ConnectorError
from tools.workbench_remote import RuntimeNode, RemoteAdapter, make_server

SOURCE = b'def analyze(df):\r\n    return {"rows":[{"squared":int((df["COL_0001"]**2).sum())}]}\r\n'
PUBLIC = dict(version=1, objective='Sum squares', roles=[], schema={}, examples=[],
              output={}, program_contract='Return Python')
TOKEN = 'synthetic-node-token-abcdefghijklmnopqrstuvwxyz'


class Stub:
    source = 'local_test_double'
    profile = dict(model='synthetic', allocation=500)
    def __init__(self):
        self.calls = []
        self.stopped = threading.Event()
        self.wait = False
    def readiness(self):
        return dict(ready=True, model='synthetic')
    def call(self, bundle):
        self.calls.append(bundle)
        if self.wait:
            self.stopped.wait(4)
        return SOURCE
    def cancel(self):
        self.stopped.set()


@pytest.fixture
def route(tmp_path):
    stub = Stub()
    node = RuntimeNode(tmp_path/'node', stub)
    server = make_server(node, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = RemoteAdapter(tmp_path/'client', TokenBudget(tmp_path/'budget'),
        dict(url='http://127.0.0.1:%s' % server.server_port, authorized=True,
             timeout_seconds=5), token=TOKEN)
    client.probe()
    yield node, stub, client
    stub.stopped.set()
    server.shutdown(); server.server_close(); thread.join(3)
    node.close()


def request(node, **changes):
    data = dict(task_id=str(uuid.uuid4()), node_id=node.node_id,
        runtime_sha256=node.runtime_sha256, bundle=PUBLIC,
        bundle_sha256=hashlib.sha256(node.encode(PUBLIC)).hexdigest())
    data.update(changes)
    return data


def terminal(node, task_id):
    for _ in range(100):
        value = node.status(task_id)
        if value['status'] not in ('running', 'cancelling'):
            return value
        time.sleep(.01)
    raise AssertionError('node did not finish')


def test_original_bytes_and_receipt(route):
    node, stub, client = route
    assert client.call(PUBLIC) == SOURCE
    assert stub.calls == [PUBLIC]
    assert client.last_receipt['candidate_sha256'] == hashlib.sha256(SOURCE).hexdigest()
    assert client.budget.snapshot()['charged'] == 500
    assert TOKEN not in json.dumps(client.last_receipt)


def test_duplicate_id_and_conflict(route):
    node, stub, _ = route
    data = request(node)
    node.submit(data)
    terminal(node, data['task_id'])
    node.submit(data)
    assert len(stub.calls) == 1
    changed = dict(PUBLIC, objective='changed')
    with pytest.raises(ConnectorError):
        node.submit(dict(data, bundle=changed, bundle_sha256=hashlib.sha256(node.encode(changed)).hexdigest()))


@pytest.mark.parametrize('changes', [dict(node_id='other'), dict(runtime_sha256='0'*64),
    dict(bundle_sha256='0'*64), dict(task_id='../escape'), dict(extra='private')])
def test_bad_submission_never_invokes(route, changes):
    node, stub, _ = route
    with pytest.raises(ConnectorError): node.submit(request(node, **changes))
    assert not stub.calls


def test_cancel_and_busy(route):
    node, stub, _ = route
    stub.wait = True
    data = request(node)
    node.submit(data)
    with pytest.raises(ConnectorError): node.submit(request(node))
    node.cancel(data['task_id'])
    assert terminal(node, data['task_id'])['status'] == 'cancelled'
    with pytest.raises(ConnectorError): node.candidate(data['task_id'])


def test_recovery_does_not_reinvoke(tmp_path):
    stub = Stub()
    node = RuntimeNode(tmp_path/'node', stub)
    data = request(node)
    node.submit(data); terminal(node, data['task_id']); node.close()
    reopened = RuntimeNode(tmp_path/'node', stub)
    assert reopened.candidate(data['task_id']) == SOURCE
    reopened.submit(data)
    assert len(stub.calls) == 1
    reopened.close()


def test_wrong_token_and_digest_rejected(route):
    node, stub, client = route
    client.token = 'x'*40
    with pytest.raises(ConnectorError): client.probe()
    assert not stub.calls


def test_local_execution_and_reuse(route, tmp_path):
    import pandas as pd
    from tools.workbench_agent import AgentWorkbench
    node, stub, client = route
    class Internal:
        source='local_test_double'
        def readiness(self): return dict(ready=True, live=False)
        def call(self, payload):
            return dict(objective='Compute squared total', roles=[dict(column='COL_0001', meaning='amount')],
                output=dict(columns=[dict(key='squared',label='Squared',type='integer',nullable=False,restore_from=None)],max_rows=10))
    agent = AgentWorkbench(tmp_path/'agent', internal=Internal(), external=client,
        execution_authorization=dict(authorized=True,data_scope='synthetic_only',risk_acknowledgement='existing_supervisor_not_hostile_isolation'))
    task = agent.draft(pd.DataFrame({'PRIVATE_AMOUNT':[7,9]}), 'PRIVATE_TASK')
    agent.publish(task['id'], task['proposal_sha256'])
    generated = agent.generate(task['id'], task['bundle_sha256'])
    assert agent.run(task['id'], generated['candidate_sha256'])['rows'] == [{'squared':130}]
    reused = agent.reuse(task['id'], pd.DataFrame({'PRIVATE_AMOUNT':[2,4]}))
    assert reused['result']['rows'] == [{'squared':20}]
    assert len(stub.calls) == 1 and 'PRIVATE_' not in json.dumps(stub.calls)


def test_lost_reply_keeps_id_and_no_resubmission(route, monkeypatch):
    node, stub, client = route
    real = client._request
    def dropped(path, data=None, **kw):
        if path == '/v1/tasks':
            node.submit(data)
            raise ConnectorError('dropped', 'synthetic disconnect')
        return real(path, data, **kw)
    monkeypatch.setattr(client, '_request', dropped)
    with pytest.raises(ConnectorError): client.call(PUBLIC)
    key = client.last_receipt['task_id']
    terminal(node, key)
    assert client.query(key)['status'] == 'complete'
    assert len(stub.calls) == 1 and client.last_receipt['status'] == 'unresolved'


def test_candidate_tamper_and_runtime_change(route):
    node, stub, client = route
    data = request(node)
    node.submit(data); terminal(node, data['task_id'])
    (node._path(data['task_id'])/'candidate.py').write_bytes(b'changed')
    with pytest.raises(ConnectorError): node.candidate(data['task_id'])
    node.runtime_sha256 = '1'*64
    with pytest.raises(ConnectorError): client.probe()
    assert not client.readiness()['ready']


def test_restart_consumes_running_id(tmp_path):
    from tools.workbench_lifecycle import atomic
    node = RuntimeNode(tmp_path/'node', Stub())
    data = request(node)
    folder=node._path(data['task_id']); folder.mkdir()
    record={k:v for k,v in data.items() if k!='bundle'}
    record['status']='running'
    atomic(folder/'receipt.json', record)
    node.close()
    stub=Stub(); node=RuntimeNode(tmp_path/'node',stub)
    assert node.submit(data)['status']=='unresolved'
    assert not stub.calls
    node.close()


def test_connection_api_and_status(route, tmp_path):
    from tools.workbench_server import Store
    node, stub, client = route
    store=Store(tmp_path/'store')
    result=store.configure_agent(dict(mode='remote_node',url=client.url,credential=TOKEN,authorized=True))
    assert result['node_id']==node.node_id and not stub.calls
    store.agent.external.call(PUBLIC)
    key=store.agent.external.last_receipt['task_id']
    from http.client import HTTPConnection
    from tools.workbench_server import make_server as make_workbench_server

    assert TOKEN not in json.dumps(store.external_configuration)
    with make_workbench_server(store) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        try:
            headers = {'Content-Type': 'application/json', 'X-Workbench-Token': store.token}
            connection.request('POST', '/api/external/remote-status',
                               json.dumps({'task_id': key}), headers)
            response = connection.getresponse()
            result = json.loads(response.read())
            assert response.status == 200, result
            assert result['task_id'] == key and result['status'] == 'complete'
        finally:
            connection.close()
            server.shutdown()
            thread.join(3)


def test_two_process_transport(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    code='''import sys, json
from pathlib import Path
from tools.workbench_remote import RuntimeNode, make_server
config=json.loads(sys.stdin.readline())
class Adapter:
    source='local_test_double'
    profile=dict(model='synthetic',allocation=500)
    def readiness(self): return dict(ready=True)
    def cancel(self): pass
    def call(self,bundle):
        with (Path(config['root'])/'calls.jsonl').open('ab') as f:
            f.write(json.dumps(bundle).encode()+b'\\n')
        return config['source'].encode()
node=RuntimeNode(config['root'],Adapter())
server=make_server(node,config['token'])
print(json.dumps(dict(port=server.server_port)),flush=True)
server.serve_forever()
'''
    process=subprocess.Popen([sys.executable,'-B','-u','-c',code],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=str(Path(__file__).resolve().parents[1]),
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        process.stdin.write((json.dumps(dict(root=str(tmp_path/'node'),token=TOKEN,source=SOURCE.decode()))+'\n').encode());process.stdin.flush()
        import queue
        output=queue.Queue()
        threading.Thread(target=lambda:output.put(process.stdout.readline()),daemon=True).start()
        port=json.loads(output.get(timeout=15))['port']
        client=RemoteAdapter(tmp_path/'client',TokenBudget(tmp_path/'budget'),
            dict(url='http://127.0.0.1:%s'%port,authorized=True,timeout_seconds=10),token=TOKEN)
        client.probe()
        assert client.call(PUBLIC)==SOURCE
        assert len((tmp_path/'node'/'calls.jsonl').read_text().splitlines())==1
        assert client.query(client.last_receipt['task_id'])['status']=='complete'
    finally:
        process.terminate();process.communicate(timeout=10)


def test_reconnect_cannot_relabel_original_task(route):
    node, stub, client=route
    client.call(PUBLIC)
    key=client.last_receipt['task_id']
    receipt=node.status(key)
    client.profile['node_id']=str(uuid.uuid4())
    receipt['node_id']=client.profile['node_id']
    with pytest.raises(ConnectorError):client._check_receipt(receipt,key)


def test_client_cancellation_is_terminal_and_not_retried(route):
    node, stub, client=route
    stub.wait=True
    errors=[]
    def call():
        try:client.call(PUBLIC)
        except ConnectorError as error:errors.append(error.code)
    thread=threading.Thread(target=call);thread.start()
    for _ in range(200):
        if stub.calls:break
        time.sleep(.01)
    assert stub.calls
    key=client.operation()
    client.cancel();thread.join(8)
    assert not thread.is_alive() and errors
    assert terminal(node,key)['status']=='cancelled'
    assert len(stub.calls)==1


def test_transport_has_hard_deadline(route):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    class Slow(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):time.sleep(1)
    server=ThreadingHTTPServer(('127.0.0.1',0),Slow)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=route[2];client.url='http://127.0.0.1:%s'%server.server_port
    started=time.monotonic()
    try:
        with pytest.raises(ConnectorError):client._request('/slow',timeout=.15)
        assert time.monotonic()-started<1.5
    finally:server.shutdown();server.server_close();thread.join(3)


def test_frozen_host_accepts_only_exact_remote_worker():
    from tools.workbench_desktop import worker_mode
    from tools.workbench_remote import HTTP_WORKER
    assert worker_mode(['-I','-B','-c',HTTP_WORKER])==('remote_http',[])
    with pytest.raises(ValueError): worker_mode(['-I','-B','-c',HTTP_WORKER+'\nprint(1)'])


def repair_bundle(base, candidate, code='PUBLIC_OUTPUT_INVALID'):
    identity = hashlib.sha256(RuntimeNode.encode(base)).hexdigest()
    return dict(base, repair=dict(base_bundle_sha256=identity,
        previous_candidate_sha256=hashlib.sha256(candidate).hexdigest(),
        previous_candidate_source=candidate.decode('utf-8'), public_failure_code=code))


@pytest.mark.parametrize('altered', [
    {'base_bundle_sha256': '0'*64},
    {'previous_candidate_sha256': '0'*64},
    {'public_failure_code': 'raw private exception'},
    {'unknown': 'private context'},
])
def test_remote_repair_rejects_invalid_envelope(route, altered):
    node, stub, _ = route
    bundle = repair_bundle(PUBLIC, SOURCE)
    bundle['repair'].update(altered)
    with pytest.raises(ConnectorError):
        node.submit(request(node, bundle=bundle,
            bundle_sha256=hashlib.sha256(node.encode(bundle)).hexdigest()))
    assert stub.calls == []


class InternalProposal:
    source = 'local_test_double'

    def readiness(self):
        return {'ready': True, 'live': False}

    def call(self, payload):
        return dict(objective='Compute squared total', roles=[],
            output=dict(columns=[dict(key='squared', label='Squared', type='integer',
                nullable=False, restore_from=None)], max_rows=10))


def remote_store(route, tmp_path):
    from tools.workbench_server import Store
    _, _, client = route
    store = Store(tmp_path/'store', agent_internal=InternalProposal())
    store.configure_agent(dict(mode='remote_node', url=client.url, credential=TOKEN,
        authorized=True))
    return store


def draft_remote(store):
    import pandas as pd
    return store.agent.draft(pd.DataFrame({'PRIVATE_AMOUNT': [7, 9]}), 'PRIVATE_TASK')


def test_remote_repair_requires_new_approval_and_preserves_first_failure(route, tmp_path, monkeypatch):
    from tools.workbench_agent import AgentError
    node, stub, _ = route
    store = remote_store(route, tmp_path)
    bad = b'def analyze(df):\r\n    return {"rows":[{"wrong": 0}]}\r\n'
    original_call = Stub.call

    def changed(self, bundle):
        result = original_call(self, bundle)
        return result if 'repair' in bundle else bad

    monkeypatch.setattr(Stub, 'call', changed)
    first = draft_remote(store)
    store.agent.publish(first['id'], first['proposal_sha256'])
    first = store.agent.generate(first['id'], first['bundle_sha256'])
    failure = store.agent.public_test(first['id'], first['candidate_sha256'])['public_test']
    assert failure['status'] == 'FAILED'
    connector = store.agent.external
    monkeypatch.setitem(connector._capability, 'public_repair', False)
    with pytest.raises(AgentError, match='公开修复'):
        store.agent.repair(first['id'], first['candidate_sha256'])
    assert store.agent.get(first['id'])['repair_slots_used'] == 0
    monkeypatch.setitem(connector._capability, 'public_repair', True)
    child = store.agent.repair(first['id'], first['candidate_sha256'])
    assert child['status'] == 'AWAITING_PUBLICATION_APPROVAL'
    assert child['public_bundle'] == repair_bundle(first['public_bundle'], bad, failure['reason_code'])
    with pytest.raises(AgentError):
        store.agent.generate(child['id'], child['bundle_sha256'])
    assert stub.calls == [first['public_bundle']]
    store.agent.publish(child['id'], child['proposal_sha256'])
    child = store.agent.generate(child['id'], child['bundle_sha256'])
    assert node.candidate(child['generation_receipt']['task_id']) == SOURCE
    assert stub.calls == [first['public_bundle'], child['public_bundle']]
    assert 'PRIVATE_' not in json.dumps(stub.calls)
    assert store.agent.public_test(child['id'], child['candidate_sha256'])['public_test']['status'] == 'PASSED'
    assert store.agent.run(child['id'], child['candidate_sha256'])['rows'] == [{'squared': 130}]
    assert store.agent.get(first['id'])['public_test'] == failure
    assert (store.agent._path(first['id'])/'candidate.py').read_bytes() == bad


def test_remote_recovery_reads_original_after_lost_reply_and_reconnect(route, tmp_path, monkeypatch):
    from tools.workbench_agent import AgentError
    from tools.workbench_server import Store
    node, stub, client = route
    store = remote_store(route, tmp_path)
    task = draft_remote(store)
    local_id = task['id']
    store.agent.publish(local_id, task['proposal_sha256'])
    connector = store.agent.external

    def lose_reply(path, data=None, **kwargs):
        assert path == '/v1/tasks'
        intent = store.agent.get(local_id)['generation_receipt']
        assert intent['task_id'] == data['task_id']
        node.submit(data)
        raise ConnectorError('dropped', 'synthetic disconnect')

    monkeypatch.setattr(connector, '_request', lose_reply)
    with pytest.raises(AgentError) as error:
        store.agent.generate(local_id, task['bundle_sha256'])
    assert error.value.code == 'generation_unknown'
    key = store.agent.get(local_id)['generation_receipt']['task_id']
    terminal(node, key)
    reopened = Store(store.root, agent_internal=InternalProposal())
    monkeypatch.setattr(stub, 'readiness', lambda: {'ready': False})
    connection = reopened.configure_agent(dict(mode='remote_node', url=client.url,
        credential=TOKEN, authorized=True))
    assert connection['readiness']['external']['ready'] is False
    payload = {'proposal_id': local_id}
    before = reopened.token_budget.snapshot()
    finish = reopened.lifecycle.finish_remote_recovery

    def interrupt(key):
        raise OSError('synthetic interruption after candidate publication')

    monkeypatch.setattr(reopened.lifecycle, 'finish_remote_recovery', interrupt)
    with pytest.raises(AgentError):
        reopened.agent_action('remote-recover', payload)
    partial = reopened.agent.get(local_id)
    assert partial['status'] == 'CANDIDATE_READY'
    assert partial['lifecycle']['state'] == 'UNKNOWN_BLOCKED'
    monkeypatch.setattr(reopened.lifecycle, 'finish_remote_recovery', finish)
    recovered = reopened.agent_action('remote-recover', payload)
    assert recovered['status'] == 'CANDIDATE_READY' and recovered.get('result') is None
    assert recovered['lifecycle']['state'] == 'IDLE'
    assert reopened.agent_action('remote-recover', payload)['candidate_sha256'] == recovered['candidate_sha256']
    assert (reopened.agent._path(local_id)/'candidate.py').read_bytes() == SOURCE
    assert reopened.token_budget.snapshot() == before
    assert len(stub.calls) == 1
    assert reopened.agent.run(local_id, recovered['candidate_sha256'])['rows'] == [{'squared': 130}]


@pytest.mark.parametrize('integrity_failure', ['candidate', 'bundle', 'cancel', 'cancel_during_fetch', 'foreign_receipt'])
def test_remote_recovery_cannot_admit_changed_or_cancelled_task(route, tmp_path, monkeypatch, integrity_failure):
    from tools.workbench_agent import AgentError
    node, stub, _ = route
    store = remote_store(route, tmp_path)
    task = draft_remote(store)
    local_id = task['id']
    store.agent.publish(local_id, task['proposal_sha256'])
    connector = store.agent.external
    actual_request = connector._request

    def lose_reply(path, data=None, **kwargs):
        if path == '/v1/tasks':
            node.submit(data)
            raise ConnectorError('dropped', 'synthetic disconnect')
        return actual_request(path, data, **kwargs)

    monkeypatch.setattr(connector, '_request', lose_reply)
    with pytest.raises(AgentError):
        store.agent.generate(local_id, task['bundle_sha256'])
    key = store.agent.get(local_id)['generation_receipt']['task_id']
    terminal(node, key)
    if integrity_failure == 'candidate':
        (node._path(key)/'candidate.py').write_bytes(b'tampered')
    elif integrity_failure == 'bundle':
        (store.agent._path(local_id)/'public-bundle.json').write_bytes(b'{}')
    elif integrity_failure == 'cancel':
        store.lifecycle.cancel(local_id)
    elif integrity_failure == 'foreign_receipt':
        record = store.agent._load(local_id)
        record['generation_receipt'] = connector.submission_receipt(task['public_bundle'], str(uuid.uuid4()))
        store.agent._save(record)

        def must_not_query(receipt):
            pytest.fail('receipt for another task reached the transport')

        monkeypatch.setattr(connector, 'recover_candidate', must_not_query)
    else:
        def cancel_after_fetch(receipt):
            outcome = recover(receipt)
            store.lifecycle.cancel(local_id)
            return outcome
        recover = connector.recover_candidate
        monkeypatch.setattr(connector, 'recover_candidate', cancel_after_fetch)
    with pytest.raises(AgentError):
        store.agent_action('remote-recover', {'proposal_id': local_id})
    assert store.agent.get(local_id)['status'] == 'GENERATION_UNKNOWN_BLOCKED'
    assert not (store.agent._path(local_id)/'candidate.py').exists()
    assert len(stub.calls) == 1
