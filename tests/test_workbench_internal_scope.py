import pandas as pd
import pytest
from pathlib import Path

from tools.workbench_agent import AgentError, AgentWorkbench
from test_workbench_agent import InternalDouble, ExternalDouble
from tools.workbench_server import Store
from tools.workbench_mcp import protect_token


class Capture(InternalDouble):
    def call(self, payload):
        self.seen = payload
        return super().call(payload)


def test_only_selected_schema_and_explicit_cells_reach_internal_connector(tmp_path):
    frame = pd.DataFrame({'unrelated_x': ['never-a', 'never-b'], 'measure_x': [10, 20],
                          'region_x': ['west-test', 'east-test']})
    inside = Capture()
    wb = AgentWorkbench(tmp_path, internal=inside, external=ExternalDouble())
    scope = {'columns': ['measure_x', 'region_x'], 'value_columns': [], 'rows': []}
    p = wb.draft(frame, 'Synthetic analysis', input_scope=scope)
    assert inside.seen['records'] == []
    assert [f['name'] for f in inside.seen['internal_schema']] == scope['columns']
    assert 'never-' not in str(inside.seen) and 'unrelated_x' not in str(inside.seen)
    assert p['public_bundle']['schema']['fields'] == [{'name': 'COL_0001', 'dtype': 'int64'}]
    wb.draft(frame, 'Synthetic analysis', input_scope=dict(scope, value_columns=['region_x'], rows=[2]))
    assert inside.seen['records'] == [{'region_x': 'east-test'}]
    assert 'west-test' not in str(inside.seen)
    del inside.seen
    for invalid in [dict(scope, value_columns=['unrelated_x'],rows=[1]),dict(scope, rows=[99])]:
        with pytest.raises(AgentError):
            wb.draft(frame, 'Synthetic analysis', input_scope=invalid)
        assert not hasattr(inside,'seen')


def test_desktop_rejects_missing_scope_or_changed_recipient_before_model(tmp_path):
    inside=Capture()
    store=Store(tmp_path,agent_internal=inside,agent_external=ExternalDouble())
    data=store.import_table({'text':'measure_x,never_x\n42,forbidden-cell'})
    draft={'dataset_id':data['dataset_id'],'task':'synthetic task'}
    route=store.agent.readiness()['input_recipient']
    for attrs in [{},{'input_recipient':route},{'input_scope':{'columns':['measure_x']},'input_recipient':{}}]:
        with pytest.raises(ValueError): store.agent_action('draft',dict(draft,**attrs))
        assert not hasattr(inside,'seen')
    proposal=store.agent_action('draft',dict(draft,input_recipient=route,input_scope={'columns':['measure_x']}))
    assert inside.seen['records']==[] and proposal['input_schema_columns']==['measure_x']
    assert 'never_x' not in str(inside.seen) and 'forbidden-cell' not in str(inside.seen)
    assert store.agent.input_scope(proposal)['allow_result_rows'] is False


@pytest.mark.parametrize('connection',['registered','legacy'])
def test_mcp_default_keeps_local_result_out_of_status_execute_and_interpretation(tmp_path,connection):
    store=Store(tmp_path,agent_external=ExternalDouble())
    store.server_url='http://127.0.0.1:65534'
    store.configure_agent({'mode':'enterprise_mcp','actor_id':'scope-fixture','authorized':True,
        'host_budget_enforced':True,'host_token_reservation':1000})
    dataset=store.import_table({'text':'left_out,measure_x\nsecret-fixture-a,3123\nsecret-fixture-b,4578'})['dataset_id']
    scope={'columns':['measure_x']}
    if connection=='registered':
        with pytest.raises(ValueError):store.register_host({'dataset_ids':[dataset]})
        session=store.register_host({'dataset_ids':[dataset],'input_scopes':{dataset:scope}})
    else:
        task=store.agent_action('draft',{'dataset_id':dataset,'task':'synthetic total','input_scope':scope,
                                        'input_recipient':store.agent.enterprise_actor})
        session=store.create_mcp_session({'proposal_id':task['id'],'dataset_id':dataset})
    token=protect_token(Path(session['credential_file']).read_bytes(),decrypt=True).decode()
    def call(name,**args):
        return store.mcp_action(name,args,session['session_id'],token)
    if connection=='registered':
        task=call('create_task',dataset_id=dataset,task='synthetic total')
    key=task['id']
    context=call('get_task_context')
    assert context['context']['records']==[] and 'left_out' not in str(context)
    proposal=call('submit_proposal',context_sha256=context['context_sha256'],proposal=InternalDouble().call({}))
    store.agent.publish(key,proposal['proposal_sha256'])
    candidate=call('generate_program',bundle_sha256=proposal['bundle_sha256'])
    result=call('execute_program',candidate_sha256=candidate['candidate_sha256'])
    local=store.agent.get(key)['result']['rows']
    assert local==[{'squared':3123**2+4578**2}]
    assert 'rows' not in result and call('get_task_status')['result'] is None
    with pytest.raises(AgentError,match='结果行仅在本机'):call('get_result_context')
    if connection=='registered':
        store.admit_host({'registration_id':session['session_id'],'dataset_ids':[dataset],
                          'input_scopes':{dataset:{'columns':['left_out']}}})
        with pytest.raises(ValueError):call('get_task_status',proposal_id=key)
        assert call('list_tasks')['tasks']==[]


def test_older_pending_task_has_no_authority_to_release_rows(tmp_path):
    from tools import workbench_agent as agent
    store=Store(tmp_path)
    store.configure_agent({'mode':'enterprise_mcp','actor_id':'scope-fixture','authorized':True,
        'host_budget_enforced':True,'host_token_reservation':1000})
    task=store.agent.draft(pd.DataFrame({'measure_x':[22,42]}),'synthetic task',input_scope={'columns':['measure_x']},
                           expected_route=store.agent.enterprise_actor)
    key=task['id']
    directory=store.agent._path(key)
    private=agent.storage._json(directory/'private.json')
    private.pop('input_scope')
    record=store.agent._load(key)
    record['private_sha256']=agent.digest(private)
    (directory/'private.json').write_bytes(agent.storage._bytes(private))
    store.agent._save(record)
    with pytest.raises(AgentError) as error:store.agent.enterprise_context(key,store.agent.enterprise_actor)
    assert error.value.code=='input_scope_required'


def test_shared_entry_requires_recipient_and_scope_even_for_direct_live_shaped_adapter(tmp_path):
    class Hosted(Capture):
        source='synthetic_http_shape'
        def readiness(self):
            return dict(super().readiness(),live=True)
    inside=Hosted()
    wb=AgentWorkbench(tmp_path,internal=inside,external=ExternalDouble())
    frame=pd.DataFrame({'measure_x':[42],'hidden_x':[77]})
    route=wb.readiness()['input_recipient']
    scope={'columns':['measure_x']}
    for args in [{},{'expected_route':route},{'input_scope':scope}]:
        with pytest.raises(AgentError):wb.draft(frame,'synthetic task',**args)
        assert not hasattr(inside,'seen')
    wb.draft(frame,'synthetic task',input_scope=scope,expected_route=route)
    assert inside.seen['records']==[] and 'hidden_x' not in str(inside.seen)


def test_recurring_value_consent_survives_restart_and_can_only_be_narrowed(tmp_path):
    store=Store(tmp_path,agent_external=ExternalDouble())
    store.configure_agent({'mode':'enterprise_mcp','actor_id':'scope-fixture','authorized':True,
        'host_budget_enforced':True,'host_token_reservation':1000})
    dataset=store.import_table({'text':'measure_x,region_x\n10,west-fixture\n20,east-fixture'})['dataset_id']
    scope={'columns':['measure_x','region_x'],'value_columns':['region_x'],'rows':[2],'allow_result_rows':True}
    grant={'dataset_ids':[dataset],'input_scopes':{dataset:scope}}
    with pytest.raises(ValueError,match='持续'):store.register_host(grant)
    reg=store.register_host({'dataset_ids':[]})
    change=dict(grant,registration_id=reg['registration_id'])
    with pytest.raises(ValueError,match='持续'):store.admit_host(change)
    store.admit_host(dict(change,persistent_value_grants=[dataset]))
    token=protect_token(Path(reg['credential_file']).read_bytes(),decrypt=True).decode()
    store=Store(tmp_path)
    assert store.host_status()['registrations'][0]['input_scopes'][dataset]==scope
    def call(name,**args):return store.mcp_action(name,args,reg['session_id'],token)
    for task_text in ['first fixture task','second fixture task']:
        call('create_task',dataset_id=dataset,task=task_text)
        assert call('get_task_context')['context']['records']==[{'region_x':'east-fixture'}]
    with pytest.raises(ValueError):store.narrow_host(dict(change,dataset_id=dataset,columns=['new-field']))
    store.narrow_host(dict(change,dataset_id=dataset,columns=['measure_x']))
    assert store.host_status()['registrations'][0]['input_scopes'][dataset]=={
        'columns':['measure_x'],'value_columns':[],'rows':[],'allow_result_rows':False}
    assert call('list_tasks')['tasks']==[]
    call('create_task',dataset_id=dataset,task='third fixture task')
    assert call('get_task_context')['context']['records']==[]


def test_legacy_context_in_flight_is_withheld_when_session_configuration_changes(tmp_path,monkeypatch):
    store=Store(tmp_path,agent_external=ExternalDouble())
    store.server_url='http://127.0.0.1:65534'
    config={'mode':'enterprise_mcp','actor_id':'scope-fixture','authorized':True,
        'host_budget_enforced':True,'host_token_reservation':1000}
    store.configure_agent(config)
    dataset=store.import_table({'text':'measure_x\n42'})['dataset_id']
    task=store.agent_action('draft',{'dataset_id':dataset,'task':'synthetic task','input_recipient':store.agent.enterprise_actor,
        'input_scope':{'columns':['measure_x'],'value_columns':['measure_x'],'rows':[1]}})
    session=store.create_mcp_session({'proposal_id':task['id'],'dataset_id':dataset})
    token=protect_token(Path(session['credential_file']).read_bytes(),decrypt=True).decode()
    original=store.agent.enterprise_context
    def change_before_delivery(*args):
        context=original(*args)
        assert context['context']['records']==[{'measure_x':42}]
        store.configure_agent(dict(config,actor_id='new-fixture-host'))
        return context
    monkeypatch.setattr(store.agent,'enterprise_context',change_before_delivery)
    with pytest.raises(ValueError) as error:store.mcp_action('get_task_context',{},session['session_id'],token)
    assert error.value.code=='mcp_session_rejected'
