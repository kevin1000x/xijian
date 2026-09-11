"""Task lifecycle tests use local doubles, never a provider."""
import time
import threading
import pytest
from tools.workbench_server import Store
from tools.workbench_agent import AgentError
from test_workbench_agent import InternalDouble, ExternalDouble

def make_fixture(tmp_path, external=None):
    store=Store(tmp_path,agent_internal=InternalDouble(),agent_external=external or ExternalDouble())
    data=store.import_table({'text':'PRIVATE_AMOUNT_X\n3\n4'})
    task=store.agent_action('draft',{'dataset_id':data['dataset_id'],'task':'PRIVATE_TASK_X'})
    return store,data,task

def test_cancel_persists_and_guards_direct_operations(tmp_path):
    store,data,p=make_fixture(tmp_path)
    cancelled=store.lifecycle.cancel(p['id'])
    assert cancelled['cancelled'] and cancelled['remote_outcome']=='unknown'
    with pytest.raises(AgentError) as error:
        store.agent.publish(p['id'],p['proposal_sha256'])
    assert error.value.code=='task_cancelled'
    restarted=Store(tmp_path)
    assert restarted.lifecycle.view(p['id'])['cancelled']

def test_unknown_started_operation_never_replayed(tmp_path):
    store,data,p=make_fixture(tmp_path)
    store.lifecycle.begin(p['id'],'generate')
    restarted=Store(tmp_path,agent_internal=InternalDouble(),agent_external=ExternalDouble())
    assert restarted.lifecycle.view(p['id'])['state']=='UNKNOWN_BLOCKED'
    with pytest.raises(AgentError):
        restarted.lifecycle.advance(p['id'])
    assert not restarted.agent.external.calls

def test_cancel_during_generation_preserves_late_candidate(tmp_path):
    entered=threading.Event(); release=threading.Event()
    class Slow(ExternalDouble):
        def call(self,payload):
            entered.set(); release.wait(5)
            return super().call(payload)
        def cancel(self):
            release.set()
            return {'cancelled':True}
    store,data,p=make_fixture(tmp_path,Slow())
    store.agent.publish(p['id'],p['proposal_sha256'])
    thread=threading.Thread(target=lambda:store.agent.generate(p['id'],p['bundle_sha256']))
    thread.start(); assert entered.wait(2)
    cancelled=store.lifecycle.cancel(p['id'])
    thread.join(5)
    assert not thread.is_alive() and cancelled['cancelled']
    current=store.agent.get(p['id'])
    assert current['candidate_sha256']
    assert current['lifecycle']['state']=='CANCELLED'
    with pytest.raises(AgentError): store.agent.run(p['id'],current['candidate_sha256'])

def test_hosted_advance_finishes_without_browser(tmp_path):
    class Internal(InternalDouble):
        def call(self,payload):
            if payload.get('purpose')=='interpret': return {'text':'Observed synthetic result.'}
            return super().call(payload)
    store,data,p=make_fixture(tmp_path)
    store.agent.internal=Internal()
    store.lifecycle.advance(p['id'],p['proposal_sha256'])
    deadline=time.time()+15
    while store.lifecycle.running(p['id']) and time.time()<deadline: time.sleep(.03)
    current=store.agent.get(p['id'])
    assert current['status']=='INTERPRETED_UNVERIFIED'
    assert current['result']['rows']==[{'squared':25}]

def test_budget_projection_does_not_rewrite_ledger(tmp_path):
    store=Store(tmp_path)
    store.token_budget.reserve('unknown',100)
    store.token_budget.settle('unknown',None)
    store.token_budget.reserve('observed',100)
    store.token_budget.settle('observed',40)
    raw=(tmp_path/'token-budget'/'budget.json').read_bytes()
    view=store.token_budget.snapshot()
    assert view['conservative_charged']==100 and view['observed_tokens']==40
    assert view['unknown_operations']==1 and view['ledger_allocatable']==9999860
    assert (tmp_path/'token-budget'/'budget.json').read_bytes()==raw

def configured_host(tmp_path):
    store=Store(tmp_path,agent_external=ExternalDouble())
    store.configure_agent({'mode':'enterprise_mcp','actor_id':'codex-synthetic-test','authorized':True,
        'host_budget_enforced':True,'host_token_reservation':2000})
    data=store.import_table({'text':'PRIVATE_AMOUNT_X\n3\n4'})
    reg=store.register_host({'dataset_ids':[data['dataset_id']]})
    from tools.workbench_mcp import protect_token
    from pathlib import Path
    secret=protect_token(Path(reg['credential_file']).read_bytes(),decrypt=True).decode()
    return store,data,reg,secret

def test_stable_host_scopes_claim_revoke_restart(tmp_path):
    store,data,reg,secret=configured_host(tmp_path)
    assert not store.host_status()['connected']
    def call(name,args={}):
        return store.mcp_action(name,args,reg['registration_id'],secret)
    denied=store.import_table({'text':'OTHER_PRIVATE\n7\n8'})
    with pytest.raises(ValueError):
        call('create_task',{'dataset_id':denied['dataset_id'],'task':'private task'})
    task=call('create_task',{'dataset_id':data['dataset_id'],'task':'private task'})
    assert call('list_tasks')['tasks'][0]['id']==task['id']
    assert store.host_status()['connected']
    restarted=Store(tmp_path)
    assert restarted.agent.enterprise_actor==store.agent.enterprise_actor
    assert not restarted.host_status()['connected']
    assert restarted.mcp_action('list_authorized_inputs',{},reg['registration_id'],secret)['inputs'][0]['dataset_id']==data['dataset_id']
    assert restarted.mcp_action('get_task_context',{'proposal_id':task['id']},reg['registration_id'],secret)['context']['records']
    restarted.revoke_host({'registration_id':reg['registration_id']})
    with pytest.raises(ValueError):
        restarted.mcp_action('list_tasks',{},reg['registration_id'],secret)

def test_second_host_cannot_steal_live_claim_or_discover_unadmitted_task(tmp_path):
    store,data,reg,secret=configured_host(tmp_path)
    task=store.mcp_action('create_task',{'dataset_id':data['dataset_id'],'task':'private task'},reg['registration_id'],secret)
    second=store.register_host({'dataset_ids':[data['dataset_id']]})
    from pathlib import Path
    from tools.workbench_mcp import protect_token
    token=protect_token(Path(second['credential_file']).read_bytes(),decrypt=True).decode()
    with pytest.raises(AgentError) as error:
        store.mcp_action('claim_task',{'proposal_id':task['id']},second['registration_id'],token)
    assert error.value.code=='task_claimed'
    store.admit_host({'registration_id':second['registration_id'],'dataset_ids':[]})
    assert store.mcp_action('list_tasks',{},second['registration_id'],token)=={'tasks':[]}
    with pytest.raises(ValueError):
        store.mcp_action('claim_task',{'proposal_id':task['id']},second['registration_id'],token)

def test_claim_expiry_prevents_further_context(tmp_path):
    store,data,reg,secret=configured_host(tmp_path)
    task=store.mcp_action('create_task',{'dataset_id':data['dataset_id'],'task':'private task'},reg['registration_id'],secret)
    from tools.workbench_lifecycle import atomic
    state=store.lifecycle.view(task['id']);state['claim_expires']=0
    atomic(store.lifecycle._path(task['id']),state)
    with pytest.raises(AgentError) as error:
        store.mcp_action('get_task_context',{},reg['registration_id'],secret)
    assert error.value.code=='claim_required'

def test_stable_bridge_discovers_new_port_and_rejects_wrong_state(tmp_path):
    from tools.workbench_server import make_server
    from tools.workbench_mcp import LocalBridge
    store,data,reg,secret=configured_host(tmp_path)
    server=make_server(store)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    bridge=LocalBridge(None,reg['registration_id'],secret,tmp_path/'server.json',store.state_id)
    try: assert not bridge.call('heartbeat',{})['isError']
    finally: server.shutdown();server.server_close();thread.join(2)
    restarted=Store(tmp_path);server=make_server(restarted)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        assert not bridge.call('heartbeat',{})['isError']
        from tools.workbench_lifecycle import atomic
        atomic(tmp_path/'server.json',{'app_id':'wrong','state_id':store.state_id,'url':restarted.server_url})
        assert bridge.call('heartbeat',{})['isError']
    finally: server.shutdown();server.server_close();thread.join(2)

def test_qoder_live_bound_unverified_before_any_submission(tmp_path):
    class Unbounded(ExternalDouble):
        source='qoder'
    external=Unbounded()
    store,data,p=make_fixture(tmp_path,external)
    store.agent.publish(p['id'],p['proposal_sha256'])
    with pytest.raises(AgentError) as error: store.agent.generate(p['id'],p['bundle_sha256'])
    assert error.value.code=='live_budget_bound_unverified'
    assert external.calls==[]

def test_stdio_cancellation_is_read_while_generation_is_running():
    import io,json
    from tools.workbench_mcp import serve
    from test_workbench_mcp import initialize
    entered=threading.Event(); stopped=threading.Event()
    class Reader:
        def __init__(self): self.index=0
        def readline(self,*args):
            values=[initialize(),{'jsonrpc':'2.0','method':'notifications/initialized'},
              {'jsonrpc':'2.0','id':8,'method':'tools/call','params':{'name':'generate_program','arguments':{'bundle_sha256':'a'*64}}},
              {'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':8}}]
            if self.index==3: assert entered.wait(2)
            if self.index>=len(values): return ''
            value=values[self.index];self.index+=1
            return json.dumps(value)+'\n'
    class Bridge:
        def call(self,name,args):
            if name=='generate_program':
                entered.set();assert stopped.wait(2)
            elif name=='cancel_task': stopped.set()
            return {'content':[],'isError':False}
    output=io.StringIO()
    serve(Reader(),output,Bridge())
    assert stopped.is_set()

def test_cancelling_an_unstarted_task_does_not_cancel_another_transport(tmp_path):
    entered=threading.Event();release=threading.Event()
    class Slow(ExternalDouble):
        cancelled=0
        def call(self,payload):
            entered.set();release.wait(5);return super().call(payload)
        def cancel(self): self.cancelled+=1;release.set();return True
    external=Slow();store,data,first=make_fixture(tmp_path,external)
    second=store.agent_action('draft',{'dataset_id':data['dataset_id'],'task':'other private task'})
    store.agent.publish(first['id'],first['proposal_sha256'])
    thread=threading.Thread(target=lambda:store.agent.generate(first['id'],first['bundle_sha256']))
    thread.start();assert entered.wait(2)
    store.lifecycle.cancel(second['id'])
    assert external.cancelled==0
    release.set();thread.join(5)
    assert not thread.is_alive()

def test_publication_unknown_is_not_replayed_by_polling(tmp_path):
    store,data,p=make_fixture(tmp_path)
    store.lifecycle.begin(p['id'],'publish')
    restarted=Store(tmp_path)
    for _ in range(3):
        assert restarted.agent.get(p['id'])['lifecycle']['state']=='UNKNOWN_BLOCKED'
    assert not restarted.lifecycle.threads

def test_receipt_credits_are_separate_and_deduplicated(tmp_path):
    store,data,p=make_fixture(tmp_path)
    record=store.agent._load(p['id'])
    record['generation_receipt']={'operation_id':'synthetic-op','total_credits':2.5,'usage':{'input_tokens':0,'output_tokens':0}}
    store.agent._save(record)
    view=store.budget_view()
    assert view['credits']==2.5 and view['observed_tokens']==0
    assert view['credit_basis']=='sdk_reported_not_tokens'

def test_cancellation_also_guards_existing_revision_child(tmp_path):
    store,data,parent=make_fixture(tmp_path)
    child=store.agent.revise(parent['id'],None,'another private question')
    store.lifecycle.cancel(parent['id'])
    with pytest.raises(AgentError) as error:
        store.agent.publish(child['id'],child['proposal_sha256'])
    assert error.value.code=='task_cancelled'

def test_valid_receipt_credit_zero_is_observed_not_missing(tmp_path):
    store,data,p=make_fixture(tmp_path)
    record=store.agent._load(p['id'])
    record['generation_receipt']={'operation_id':'synthetic-zero-credit','total_credits':0}
    store.agent._save(record)
    assert store.budget_view()['credits']==0

def test_reviewed_qoder_admission_requires_exact_contract_and_shared_ledger(tmp_path):
    from tools.workbench_qoder import QoderSDKAdapter,REVIEWED_RUNTIME_SHA256
    from tools.workbench_budget import TokenBudget
    class Reviewed(ExternalDouble):
        source='qoder'
        def _runtime_digest(self): return REVIEWED_RUNTIME_SHA256
        admission_contract=QoderSDKAdapter.admission_contract
    connector=Reviewed()
    store,data,p=make_fixture(tmp_path,connector)
    connector.budget=store.token_budget
    assert store.lifecycle.qoder_admission_verified(connector)
    assert store.agent._connector('external') is connector
    connector.budget=TokenBudget(tmp_path/'unrelated-ledger')
    assert not store.lifecycle.qoder_admission_verified(connector)
    with pytest.raises(AgentError):
        store.agent._connector('external')
    connector.budget=store.token_budget
    connector._runtime_digest=lambda:'0'*64
    assert not store.lifecycle.qoder_admission_verified(connector)
    assert connector.calls==[]

def test_reviewed_qoder_admission_cannot_reset_or_overdraw_existing_ceiling(tmp_path):
    from tools.workbench_qoder import QoderSDKAdapter,REVIEWED_RUNTIME_SHA256
    class Reviewed(ExternalDouble):
        source='qoder'
        def _runtime_digest(self): return REVIEWED_RUNTIME_SHA256
        admission_contract=QoderSDKAdapter.admission_contract
    connector=Reviewed();store,data,p=make_fixture(tmp_path,connector)
    connector.budget=store.token_budget
    store.token_budget.reserve('historical-conservative',7000000)
    store.token_budget.settle('historical-conservative',None)
    assert not store.lifecycle.qoder_admission_verified(connector)
    assert store.token_budget.snapshot()['charged']==7000000
    assert connector.calls==[]

def test_budget_snapshot_reads_committed_json_while_writer_is_held(tmp_path):
    from tools.workbench_budget import TokenBudget
    from tools.local_synthetic_workflow import _Writer
    budget=TokenBudget(tmp_path/'budget')
    budget.reserve('already-admitted',100)
    raw=(budget.root/'budget.json').read_bytes()
    with _Writer(budget.root):
        snapshot=budget.snapshot()
        assert snapshot['reserved']==100 and snapshot['remaining']==9999900
    assert (budget.root/'budget.json').read_bytes()==raw

def test_gui_polling_serializes_ledger_recovery_with_run_and_result(tmp_path,monkeypatch):
    from tools.local_attempt_ledger import Ledger
    store,data,p=make_fixture(tmp_path)
    store.agent.publish(p['id'],p['proposal_sha256'])
    candidate=store.agent.generate(p['id'],p['bundle_sha256'])
    assert store.agent.public_test(p['id'],candidate['candidate_sha256'])['public_test']['status']=='PASSED'
    held=threading.Event();release=threading.Event();started=threading.Event()
    original=Ledger._acquire
    def acquire(ledger,create):
        result=original(ledger,create)
        if threading.current_thread().name.startswith('holding-poll') and not held.is_set():
            held.set();assert release.wait(5)
        return result
    monkeypatch.setattr(Ledger,'_acquire',acquire)
    errors=[];results=[]
    def call(fn):
        try: results.append(fn())
        except Exception as error: errors.append(error)
    reader=threading.Thread(name='holding-poll-public',target=lambda:call(lambda:store.agent.get(p['id'])))
    reader.start();assert held.wait(3)
    def run():
        started.set()
        call(lambda:store.agent.run(p['id'],candidate['candidate_sha256']))
    runner=threading.Thread(target=run);runner.start();assert started.wait(1)
    time.sleep(.15);release.set()
    reader.join(5);runner.join(10)
    assert not reader.is_alive() and not runner.is_alive()
    assert not errors
    assert store.agent.get(p['id'])['result']['rows']==[{'squared':25}]
    # Independent result refreshes must also serialize the recovery writer.
    held.clear();release.clear();results.clear()
    reader=threading.Thread(name='holding-poll-result',target=lambda:call(lambda:store.agent.get(p['id'])))
    reader.start();assert held.wait(3)
    other=threading.Thread(target=lambda:call(lambda:store.agent.get(p['id'])))
    other.start();time.sleep(.15);release.set()
    reader.join(5);other.join(5)
    assert not errors and len(results)==2

def test_get_skips_public_ledger_while_public_execution_is_active(tmp_path,monkeypatch):
    from tools.local_attempt_ledger import Ledger
    store,data,p=make_fixture(tmp_path)
    store.agent.publish(p['id'],p['proposal_sha256'])
    candidate=store.agent.generate(p['id'],p['bundle_sha256'])
    held=threading.Event();release=threading.Event();errors=[]
    original=Ledger._acquire
    def acquire(ledger,create):
        result=original(ledger,create)
        if create and not held.is_set():
            held.set();assert release.wait(5)
        return result
    monkeypatch.setattr(Ledger,'_acquire',acquire)
    def run_public():
        try: store.agent.public_test(p['id'],candidate['candidate_sha256'])
        except Exception as error: errors.append(error)
    thread=threading.Thread(target=run_public);thread.start();assert held.wait(3)
    try:
        record=store.agent.get(p['id'])
        assert record['public_test']['in_flight'] is True
        assert record['lifecycle']['operation']['status']=='started'
    finally: release.set();thread.join(10)
    assert not errors and not thread.is_alive()
    assert store.agent.get(p['id'])['public_test']['status']=='PASSED'

def test_qoder_reconnect_with_only_generation_budget_missing_keeps_local_execution(tmp_path,monkeypatch):
    import sys,types
    class Adapter:
        source='qoder'
        missing=['token_budget']
        def __init__(self,root,budget,profile,token=None): self.profile=profile
        def probe(self): return {'status':'ready','model_requests':0}
        def readiness(self): return {'ready':False,'source':'qoder','missing':self.missing}
        def admission_contract(self): return {'runtime_reviewed':True}
    monkeypatch.setitem(sys.modules,'tools.workbench_qoder',types.SimpleNamespace(QoderSDKAdapter=Adapter))
    store=Store(tmp_path)
    store.configure_agent({'mode':'qoder_sdk','authorized':True,'auth_mode':'local_login'})
    assert store.agent._execution_allowed()
    with pytest.raises(AgentError): store.agent._connector('external')
    Adapter.missing=['token_budget','authenticated_model_catalog']
    store.configure_agent({'mode':'qoder_sdk','authorized':True,'auth_mode':'local_login'})
    assert not store.agent._execution_allowed()

def test_host_interpretation_finishes_lifecycle_without_a_driver(tmp_path):
    store,data,reg,secret=configured_host(tmp_path)
    task=store.mcp_action('create_task',{'dataset_id':data['dataset_id'],'task':'PRIVATE_TASK_X'},reg['registration_id'],secret)
    key=task['id'];actor=store.agent.enterprise_actor
    context=store.agent.enterprise_context(key,actor)
    proposal=store.agent.enterprise_accept(key,actor,context['context_sha256'],InternalDouble().call({}))
    store.agent.publish(key,proposal['proposal_sha256'])
    candidate=store.agent.generate(key,proposal['bundle_sha256'])
    store.agent.run(key,candidate['candidate_sha256'])
    context=store.agent.enterprise_result_context(key,actor)
    result=store.agent.enterprise_interpret(key,actor,context['run_id'],context['result_sha256'],'Observed result.')
    assert result['status']=='INTERPRETED_UNVERIFIED'
    assert result['lifecycle']['state']=='COMPLETE'
    import json
    from tools.workbench_lifecycle import atomic
    path=store.lifecycle._path(key)
    persisted=json.loads(path.read_text('utf-8'))
    assert persisted['state']=='COMPLETE'
    persisted['state']='IDLE'
    atomic(path,persisted)
    old_bytes=path.read_bytes()
    assert store.lifecycle.view(key)['state']=='COMPLETE'
    assert path.read_bytes()==old_bytes
    persisted['state']='UNKNOWN_BLOCKED'
    atomic(path,persisted)
    assert store.lifecycle.view(key)['state']=='UNKNOWN_BLOCKED'
    store.lifecycle.cancel(key)
    assert store.lifecycle.view(key)['state']=='CANCELLED'

def test_qoder_low_budget_reports_exhaustion_not_unreviewed_runtime(tmp_path):
    from tools.workbench_qoder import QoderSDKAdapter,REVIEWED_RUNTIME_SHA256
    class Reviewed(ExternalDouble):
        source='qoder'
        def _runtime_digest(self): return REVIEWED_RUNTIME_SHA256
        admission_contract=QoderSDKAdapter.admission_contract
        def readiness(self):
            return {'ready':False,'source':'qoder','missing':['token_budget'],'live':True}
    external=Reviewed()
    store,data,p=make_fixture(tmp_path,external)
    external.budget=store.token_budget
    store.token_budget.reserve('previously-used',9500000)
    store.token_budget.settle('previously-used',None)
    readiness=store.agent.readiness()['external']
    assert readiness['live_admission']=='budget_exhausted'
    assert readiness['ready'] is False
    assert 'token_budget' in readiness['missing']
    assert 'live_budget_bound_unverified' not in readiness['missing']
    assert not store.lifecycle.qoder_admission_verified(external)
    external._runtime_digest=lambda:'0'*64
    assert store.agent.readiness()['external']['live_admission']=='pending_bound_review'
