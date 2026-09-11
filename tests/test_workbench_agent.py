"""Prospective task publication and unchanged candidate execution tests."""
import json
import pytest
import pandas as pd
from tools import workbench_agent as agent

class InternalDouble:
    source = 'local_test_double'
    def readiness(self):
        return {'ready': True, 'live': False, 'missing': [], 'source': self.source}
    def call(self, payload):
        return {'objective': 'Compute squared total', 'roles': [
            {'column': 'COL_0001', 'meaning': 'amount'}],
            'output': {'columns': [{'key': 'squared', 'label': 'Squared total',
                'type': 'integer', 'nullable': False, 'restore_from': None}], 'max_rows': 10}}

class ExternalDouble:
    source = 'local_test_double'
    def __init__(self): self.calls = []
    def readiness(self):
        return {'ready': True, 'live': False, 'missing': [], 'source': self.source}
    def call(self, payload):
        self.calls.append(payload)
        return b'def analyze(df):\n    return {"rows": [{"squared": int((df["COL_0001"] ** 2).sum())}]}\n'

def test_publication_is_exact_approved_synthetic_bundle(tmp_path):
    external = ExternalDouble()
    service = agent.AgentWorkbench(tmp_path, internal=InternalDouble(), external=external)
    proposal = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X': [7, 9]}), 'PRIVATE_TASK_X')
    assert proposal['status'] == 'AWAITING_PUBLICATION_APPROVAL'
    raw = json.dumps(proposal['public_bundle'])
    assert 'PRIVATE_' not in raw and '7' not in json.dumps(proposal['public_bundle']['examples'])
    with pytest.raises(agent.AgentError):
        service.generate(proposal['id'], proposal['bundle_sha256'])
    with pytest.raises(agent.AgentError):
        service.publish(proposal['id'], '0' * 64)
    service.publish(proposal['id'], proposal['proposal_sha256'])
    candidate = service.generate(proposal['id'], proposal['bundle_sha256'])
    assert candidate['status'] == 'CANDIDATE_READY'
    assert external.calls == [proposal['public_bundle']]
    result = service.run(proposal['id'], candidate['candidate_sha256'])
    assert result['rows'] == [{'squared': 130}]
    assert result['source'] == 'local_test_double'
    assert result['status'] == 'EXECUTED_UNVERIFIED'
    assert service.get(proposal['id'])['result']['rows'] == [{'squared': 130}]
    run_root = tmp_path / 'runs' / result['run_id']
    assert (run_root / 'candidate.py').read_bytes() == (run_root / 'ledger/candidate.bin').read_bytes()


def test_private_marker_rejected_before_publication(tmp_path):
    class Leaking(InternalDouble):
        def call(self, payload):
            value = super().call(payload)
            value['objective'] = 'Sum PRIVATE_AMOUNT_X'
            return value
    external = ExternalDouble()
    service = agent.AgentWorkbench(tmp_path, internal=Leaking(), external=external)
    with pytest.raises(agent.AgentError, match='原始'):
        service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X': [7, 9]}), 'PRIVATE_TASK_X')
    assert not external.calls
    assert not list(tmp_path.rglob('public-bundle.json'))
    assert service.history()[0]['status'] == 'DRAFT_FAILED'


def test_unknown_submission_is_durable_and_never_retried(tmp_path):
    class Unknown(ExternalDouble):
        def call(self, payload):
            self.calls.append(payload)
            self.last_receipt={'status':'unresolved','events':[{'kind':'tool_boundary','tool':'Write','allowed':False}]}
            raise TimeoutError('sensitive remote text must not appear')
    external = Unknown()
    service = agent.AgentWorkbench(tmp_path, internal=InternalDouble(), external=external)
    p = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X': [7, 9]}), 'compute squared total')
    service.publish(p['id'], p['proposal_sha256'])
    with pytest.raises(agent.AgentError) as error:
        service.generate(p['id'], p['bundle_sha256'])
    assert 'sensitive' not in str(error.value)
    restarted = agent.AgentWorkbench(tmp_path, internal=InternalDouble(), external=external)
    assert restarted.get(p['id'])['status'] == 'GENERATION_UNKNOWN_BLOCKED'
    assert restarted.get(p['id'])['generation_receipt']==external.last_receipt
    assert restarted.history()[0]['status']=='GENERATION_UNKNOWN_BLOCKED'
    with pytest.raises(agent.AgentError):
        restarted.publish(p['id'],p['proposal_sha256'])
    with pytest.raises(agent.AgentError):
        restarted.run(p['id'],'0'*64)
    with pytest.raises(agent.AgentError):
        restarted.generate(p['id'], p['bundle_sha256'])
    assert len(external.calls) == 1


def test_changed_route_and_private_snapshot_cannot_use_approval(tmp_path):
    external = ExternalDouble()
    external.profile = {'route_id': 'approved-route'}
    service = agent.AgentWorkbench(tmp_path, internal=InternalDouble(), external=external)
    p = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X': [7, 9]}), 'compute squared total')
    service.publish(p['id'], p['proposal_sha256'])
    external.profile['route_id'] = 'different-route'
    with pytest.raises(agent.AgentError):
        service.generate(p['id'], p['bundle_sha256'])
    assert not external.calls
    private_path = tmp_path / 'tasks' / p['id'] / 'private.json'
    value = json.loads(private_path.read_bytes())
    value['records'][0]['PRIVATE_AMOUNT_X'] = 900
    private_path.write_bytes(agent.storage._bytes(value))
    with pytest.raises(agent.AgentError):
        service.get(p['id'])


def test_restart_recovers_table_without_execution_and_interpretation_is_internal(tmp_path, monkeypatch):
    class Interpret(InternalDouble):
        def __init__(self): self.calls = []
        def call(self, payload):
            self.calls.append(payload)
            if payload['purpose'] == 'interpret':
                return {'text': 'Squared total is 130; correctness not independently evaluated.'}
            return super().call(payload)
    internal, external = Interpret(), ExternalDouble()
    service = agent.AgentWorkbench(tmp_path, internal=internal, external=external)
    p = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X': [7, 9]}), 'PRIVATE_TASK_X')
    assert p['public_preview'].encode('utf-8') == agent.storage._bytes(p['public_bundle'])
    service.publish(p['id'], p['proposal_sha256'])
    candidate = service.generate(p['id'], p['bundle_sha256'])
    result = service.run(p['id'], candidate['candidate_sha256'])
    from tools import workbench_analysis
    monkeypatch.setattr(workbench_analysis, 'execute', lambda *a, **k: pytest.fail('recovery executed'))
    restarted = agent.AgentWorkbench(tmp_path, internal=internal, external=external)
    assert restarted.recover(p['id']) == result
    interpreted = restarted.interpret(p['id'], result['run_id'])
    assert interpreted['interpretation']['result_sha256'] == agent.digest(result)
    assert internal.calls[-1]['task'] == 'PRIVATE_TASK_X'
    assert 'PRIVATE_' not in json.dumps(external.calls)
    with pytest.raises(agent.AgentError):
        restarted.interpret(p['id'], result['run_id'])


def test_unicode_task_contract_survives_restart(tmp_path):
    class Chinese(InternalDouble):
        def call(self, payload):
            result = super().call(payload)
            result['objective'] = '计算平方和'
            result['output']['columns'][0]['label'] = '平方和'
            return result
    service = agent.AgentWorkbench(tmp_path, internal=Chinese(), external=ExternalDouble())
    p = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X': [7, 9]}), 'PRIVATE_TASK_X')
    assert agent.AgentWorkbench(tmp_path).get(p['id'])['public_bundle']['objective'] == '计算平方和'


def test_dynamic_invalid_table_is_failed_not_a_success(tmp_path):
    class BadOutput(ExternalDouble):
        def call(self, payload):
            return b'def analyze(df):\n    return {"rows": [{"other": 130}]}\n'
    service = agent.AgentWorkbench(tmp_path, internal=InternalDouble(), external=BadOutput())
    p = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X': [7, 9]}), 'PRIVATE_TASK_X')
    service.publish(p['id'], p['proposal_sha256'])
    candidate = service.generate(p['id'], p['bundle_sha256'])
    result = service.run(p['id'], candidate['candidate_sha256'])
    assert result['status'] == 'EXECUTION_FAILED'
    assert result['rows'] == [] and result['reason'] == 'invalid_table_output'
    assert service.recover(p['id']) == result


def test_interrupted_execution_recovery_is_unknown_and_never_reexecutes(tmp_path, monkeypatch):
    external=ExternalDouble()
    service = agent.AgentWorkbench(tmp_path, internal=InternalDouble(), external=external)
    p = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X': [7, 9]}), 'PRIVATE_TASK_X')
    service.publish(p['id'], p['proposal_sha256'])
    candidate = service.generate(p['id'], p['bundle_sha256'])
    from tools import workbench_analysis
    class Crash(BaseException): pass
    def crash(label):
        if label == 'start:published': raise Crash()
    monkeypatch.setattr(workbench_analysis, '_boundary', crash)
    with pytest.raises(Crash):
        service.run(p['id'], candidate['candidate_sha256'])
    monkeypatch.setattr(workbench_analysis, 'execute', lambda *a, **k: pytest.fail('reexecution'))
    reopened=agent.AgentWorkbench(tmp_path,internal=InternalDouble(),external=external)
    assert reopened.history()[0]['status']=='EXECUTION_UNKNOWN_BLOCKED'
    assert reopened.get(p['id'])['status']=='EXECUTION_UNKNOWN_BLOCKED'
    with pytest.raises(agent.AgentError):
        reopened.generate(p['id'],p['bundle_sha256'])
    with pytest.raises(agent.AgentError):
        reopened.run(p['id'],candidate['candidate_sha256'])
    assert len(external.calls)==1
    recovered = reopened.recover(p['id'])
    assert recovered['status'] == 'UNKNOWN_BLOCKED'
    assert recovered['rows'] == []
    with pytest.raises(agent.AgentError):
        service.run(p['id'], candidate['candidate_sha256'])


@pytest.mark.parametrize('retarget_start_identity', [False, True])
def test_cross_task_run_retargeting_rejected_before_interpretation(tmp_path, retarget_start_identity):
    class Interpret(InternalDouble):
        def __init__(self): self.interpretations = []
        def call(self, payload):
            if payload['purpose'] == 'interpret':
                self.interpretations.append(payload)
                return {'text': 'should never see wrong task result'}
            return super().call(payload)
    internal = Interpret()
    service = agent.AgentWorkbench(tmp_path, internal=internal, external=ExternalDouble())
    tasks = []
    for values in ([7, 9], [70, 90]):
        p = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X': values}), 'PRIVATE_TASK_X')
        service.publish(p['id'], p['proposal_sha256'])
        candidate = service.generate(p['id'], p['bundle_sha256'])
        result = service.run(p['id'], candidate['candidate_sha256'])
        tasks.append((p, result))
    assert tasks[0][1]['rows'] == [{'squared': 130}]
    assert tasks[1][1]['rows'] == [{'squared': 13000}]
    path = tmp_path / 'tasks' / tasks[0][0]['id'] / 'record.json'
    record = json.loads(path.read_bytes())
    record['run_id'] = tasks[1][1]['run_id']
    if retarget_start_identity:
        record['submitted_run_id'] = tasks[1][1]['run_id']
    path.write_bytes(agent.storage._bytes(record))
    key = tasks[0][0]['id']
    for action in (lambda: service.get(key), lambda: service.recover(key),
                   lambda: service.interpret(key, tasks[1][1]['run_id'])):
        with pytest.raises(agent.AgentError) as error:
            action()
        assert error.value.code == 'execution_association_changed'
    assert internal.interpretations == []


def test_changed_internal_route_cannot_receive_original_private_task(tmp_path):
    class Interpret(InternalDouble):
        def __init__(self):
            self.profile = {'route_id': 'original-internal-route'}
            self.interpretations = []
        def call(self, payload):
            if payload['purpose'] == 'interpret':
                self.interpretations.append(payload)
                return {'text': 'must not be sent to changed route'}
            return super().call(payload)
    internal = Interpret()
    service = agent.AgentWorkbench(tmp_path, internal=internal, external=ExternalDouble())
    p = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X': [7, 9]}), 'PRIVATE_TASK_X')
    service.publish(p['id'], p['proposal_sha256'])
    candidate = service.generate(p['id'], p['bundle_sha256'])
    result = service.run(p['id'], candidate['candidate_sha256'])
    internal.profile['route_id'] = 'different-internal-route'
    with pytest.raises(agent.AgentError) as error:
        service.interpret(p['id'], result['run_id'])
    assert error.value.code == 'internal_route_changed'
    assert internal.interpretations == []


def test_reuse_binds_new_input_without_connectors_or_changing_original(tmp_path):
    class Counting(InternalDouble):
        def __init__(self): self.calls = []
        def call(self, payload):
            self.calls.append(payload)
            return super().call(payload)
    internal, external = Counting(), ExternalDouble()
    service = agent.AgentWorkbench(tmp_path, internal=internal, external=external)
    p = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X': [7, 9]}), 'PRIVATE_TASK_X')
    service.publish(p['id'], p['proposal_sha256'])
    candidate = service.generate(p['id'], p['bundle_sha256'])
    original = service.run(p['id'], candidate['candidate_sha256'])
    before = {str(path.relative_to(tmp_path)): path.read_bytes()
              for path in tmp_path.rglob('*') if path.is_file()}
    # Reuse remains offline even when no connectors are available after restart.
    restarted = agent.AgentWorkbench(tmp_path)
    reused = restarted.reuse(p['id'], pd.DataFrame({'PRIVATE_AMOUNT_X': [2, 3, 4]}))
    assert reused['reused_from'] == p['id'] and reused['proposal_id'] != p['id']
    assert reused['result']['rows'] == [{'squared': 29}]
    assert reused['result']['run_id'] != original['run_id']
    assert reused['candidate_sha256'] == candidate['candidate_sha256']
    assert reused['bundle_sha256'] == p['bundle_sha256']
    assert reused['interpretation'] is None
    assert len(internal.calls) == len(external.calls) == 1
    assert all((tmp_path / path).read_bytes() == raw for path, raw in before.items())
    assert restarted.get(reused['id'])['result'] == reused['result']


def test_reuse_incompatible_dtype_rejects_before_creating_task(tmp_path):
    service = agent.AgentWorkbench(tmp_path, internal=InternalDouble(), external=ExternalDouble())
    p = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X': [7, 9]}), 'PRIVATE_TASK_X')
    service.publish(p['id'], p['proposal_sha256'])
    candidate = service.generate(p['id'], p['bundle_sha256'])
    service.run(p['id'], candidate['candidate_sha256'])
    before = sorted((tmp_path / 'tasks').iterdir())
    with pytest.raises((agent.AgentError, agent.analysis.WorkbenchError)):
        service.reuse(p['id'], pd.DataFrame({'PRIVATE_AMOUNT_X': [2.5, 3.5]}))
    assert sorted((tmp_path / 'tasks').iterdir()) == before


def test_public_proposal_rejects_unicode_private_marker(tmp_path):
    class Leak(InternalDouble):
        def call(self,payload):
            response=super().call(payload)
            response['objective']='汇总内部客户密钥标记字段'
            return response
    service=agent.AgentWorkbench(tmp_path,internal=Leak(),external=ExternalDouble())
    with pytest.raises(agent.AgentError) as error:
        service.draft(pd.DataFrame({'内部客户密钥标记':[7,9]}),'PRIVATE_TASK_X')
    assert error.value.code=='private_text_in_public'


def test_resume_published_task_from_history_generates_once(tmp_path):
    external=ExternalDouble()
    external.profile={'route_id':'same-synthetic-route'}
    first=agent.AgentWorkbench(tmp_path,internal=InternalDouble(),external=external)
    p=first.draft(pd.DataFrame({'PRIVATE_AMOUNT_X':[4,6]}),'PRIVATE_TASK_X')
    published=first.publish(p['id'],p['proposal_sha256'])
    reopened=agent.AgentWorkbench(tmp_path,internal=InternalDouble(),external=external)
    assert reopened.history()[0]['status']=='PUBLISHED'
    restored=reopened.get(p['id'])
    assert restored==published
    assert external.calls==[]
    generated=reopened.generate(restored['id'],restored['bundle_sha256'])
    assert generated['status']=='CANDIDATE_READY'
    # A second coordinator with a stale page snapshot must reload persisted state.
    another=agent.AgentWorkbench(tmp_path,internal=InternalDouble(),external=external)
    with pytest.raises(agent.AgentError):
        another.generate(restored['id'],restored['bundle_sha256'])
    assert len(external.calls)==1
    assert another.get(p['id'])['candidate_sha256']==generated['candidate_sha256']


def test_resume_candidate_ready_executes_original_bytes_without_generation(tmp_path,monkeypatch):
    candidate=b'# synthetic original CRLF\r\ndef analyze(df):\r\n    return {"rows":[{"squared":int((df["COL_0001"] ** 2).sum())}]}\r\n'
    class Original(ExternalDouble):
        def call(self,payload):
            self.calls.append(payload)
            return candidate
    external=Original()
    first=agent.AgentWorkbench(tmp_path,internal=InternalDouble(),external=external)
    p=first.draft(pd.DataFrame({'PRIVATE_AMOUNT_X':[4,6]}),'PRIVATE_TASK_X')
    first.publish(p['id'],p['proposal_sha256'])
    generated=first.generate(p['id'],p['bundle_sha256'])
    # No connector exists after reopen: an already-delivered candidate is sufficient.
    reopened=agent.AgentWorkbench(tmp_path)
    assert reopened.history()[0]['status']=='CANDIDATE_READY'
    restored=reopened.get(p['id'])
    assert restored['candidate_sha256']==generated['candidate_sha256']
    original_run=agent.analysis.run_candidate
    submitted=[]
    def observe(frame,raw,*args,**kwargs):
        submitted.append(raw)
        return original_run(frame,raw,*args,**kwargs)
    monkeypatch.setattr(agent.analysis,'run_candidate',observe)
    result=reopened.run(restored['id'],restored['candidate_sha256'])
    assert result['rows']==[{'squared':52}]
    assert submitted==[candidate] and len(external.calls)==1
    run=tmp_path/'runs'/result['run_id']
    assert (run/'candidate.py').read_bytes()==candidate
    assert (run/'ledger'/'candidate.bin').read_bytes()==candidate
    with pytest.raises(agent.AgentError):
        agent.AgentWorkbench(tmp_path).run(restored['id'],restored['candidate_sha256'])
    assert submitted==[candidate]


def test_reuse_known_scoped_categories_and_classify_unknown_without_side_effects(tmp_path):
    external=ExternalDouble()
    service=agent.AgentWorkbench(tmp_path,internal=InternalDouble(),external=external)
    p=service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X':[7,9],
        'PRIVATE_CATEGORY_X':['known-A','known-B']}),'PRIVATE_TASK_X')
    service.publish(p['id'],p['proposal_sha256'])
    candidate=service.generate(p['id'],p['bundle_sha256'])
    service.run(p['id'],candidate['candidate_sha256'])
    reused=service.reuse(p['id'],pd.DataFrame({'PRIVATE_AMOUNT_X':[2,3],
        'PRIVATE_CATEGORY_X':['known-B','known-A']}))
    assert reused['result']['rows']==[{'squared':13}]
    assert len(external.calls)==1
    before={str(path.relative_to(tmp_path)):path.read_bytes()
        for path in tmp_path.rglob('*') if path.is_file()}
    tasks=set((tmp_path/'tasks').iterdir())
    runs=set((tmp_path/'runs').iterdir())
    with pytest.raises(agent.AgentError) as error:
        service.reuse(p['id'],pd.DataFrame({'PRIVATE_AMOUNT_X':[4,5],
            'PRIVATE_CATEGORY_X':['new-C','new-D']}))
    assert error.value.code=='reuse_input_incompatible'
    assert '创建新任务' in error.value.message
    assert '未执行' in error.value.message and '未调用模型' in error.value.message
    assert len(external.calls)==1
    assert set((tmp_path/'tasks').iterdir())==tasks
    assert set((tmp_path/'runs').iterdir())==runs
    assert all((tmp_path/path).read_bytes()==raw for path,raw in before.items())
    # A damaged historical snapshot remains an integrity error, never input advice.
    private_path=tmp_path/'tasks'/p['id']/'private.json'
    private=json.loads(private_path.read_bytes())
    private['binding']['digest']='0'*64
    private_path.write_bytes(agent.storage._bytes(private))
    with pytest.raises(agent.AgentError) as damaged:
        service.reuse(p['id'],pd.DataFrame({'PRIVATE_AMOUNT_X':[2,3],
            'PRIVATE_CATEGORY_X':['known-B','known-A']}))
    assert damaged.value.code!='reuse_input_incompatible'
    assert len(external.calls)==1


class M2External(ExternalDouble):
    def __init__(self, bad=False):
        super().__init__()
        self.bad=bad
    def call(self,payload):
        self.calls.append(payload)
        return (b'def analyze(df):\n    return {"rows":[{"wrong":1}]}\n' if self.bad else
            b'def analyze(df):\n    return {"rows":[{"squared":int((df["COL_0001"] ** 2).sum())}]}\n')

def m2_candidate(root,bad=False):
    external=M2External(bad)
    service=agent.AgentWorkbench(root,internal=InternalDouble(),external=external)
    p=service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X':[7,9]}),'PRIVATE_TASK_X')
    service.publish(p['id'],p['proposal_sha256'])
    p=service.generate(p['id'],p['bundle_sha256'])
    return service,external,p

def test_m2_revision_has_new_binding_and_preserves_parent_bytes(tmp_path):
    service,external,p=m2_candidate(tmp_path)
    before={str(x.relative_to(tmp_path)):x.read_bytes() for x in tmp_path.rglob('*') if x.is_file()}
    revised=service.revise(p['id'],pd.DataFrame({'PRIVATE_AMOUNT_X':[3,4],
        'PRIVATE_CATEGORY_X':['NEW_LABEL_A','NEW_LABEL_B']}),'REVISED_PRIVATE_TASK')
    assert revised['id']!=p['id'] and revised['parent_id']==p['id']
    assert revised['revision_root']==p['id'] and revised['version_kind']=='revision'
    assert revised['status']=='AWAITING_PUBLICATION_APPROVAL'
    assert 'candidate_sha256' not in revised and 'approved_proposal_sha256' not in revised
    assert revised['private_sha256']!=p['private_sha256']
    assert all((tmp_path/path).read_bytes()==raw for path,raw in before.items())
    assert len(external.calls)==1
    assert agent.AgentWorkbench(tmp_path).get(revised['id'])['parent_id']==p['id']

def test_m2_public_test_uses_no_private_snapshot_and_runs_once(tmp_path,monkeypatch):
    service,external,p=m2_candidate(tmp_path)
    real_json=agent.storage._json
    def no_private(path,*args,**kwargs):
        assert path.name!='private.json'
        return real_json(path,*args,**kwargs)
    monkeypatch.setattr(agent.storage,'_json',no_private)
    tested=service.public_test(p['id'],p['candidate_sha256'])
    assert tested['public_test']['status']=='PASSED'
    assert tested['public_test']['correctness']=='NOT_EVALUATED'
    run=tmp_path/'public-tests'/tested['public_test']['run_id']
    assert real_json(run/'request.json')['records']==p['public_bundle']['examples']
    assert (run/'candidate.py').read_bytes()==(tmp_path/'tasks'/p['id']/'candidate.py').read_bytes()
    with pytest.raises(agent.AgentError):
        service.public_test(p['id'],p['candidate_sha256'])
    assert len(external.calls)==1

def test_m2_repair_preserves_first_failure_limits_family_and_requires_approval(tmp_path):
    service,external,p=m2_candidate(tmp_path,bad=True)
    failure=service.public_test(p['id'],p['candidate_sha256'])['public_test']
    assert failure['status']=='FAILED'
    assert service.get(p['id'])['repair_slots_used']==0
    before={str(x.relative_to(tmp_path)):x.read_bytes() for x in (tmp_path/'tasks'/p['id']).rglob('*') if x.is_file()}
    child=service.repair(p['id'],p['candidate_sha256'])
    assert child['status']=='AWAITING_PUBLICATION_APPROVAL'
    assert child['parent_id']==p['id'] and child['repair_index']==1
    assert child['repair_slots_used']==1
    assert 'PRIVATE_' not in child['public_preview']
    assert child['public_bundle']['repair']['public_failure_code']=='PUBLIC_OUTPUT_INVALID'
    assert all((tmp_path/path).read_bytes()==raw for path,raw in before.items())
    with pytest.raises(agent.AgentError):service.generate(child['id'],child['bundle_sha256'])
    with pytest.raises(agent.AgentError):service.repair(p['id'],p['candidate_sha256'])
    for expected in (2,):
        service.publish(child['id'],child['proposal_sha256'])
        ready=service.generate(child['id'],child['bundle_sha256'])
        service.public_test(child['id'],ready['candidate_sha256'])
        child=service.repair(child['id'],ready['candidate_sha256'])
        assert child['repair_index']==expected
        assert 'repair' not in child['public_bundle']['repair']
    service.publish(child['id'],child['proposal_sha256'])
    ready=service.generate(child['id'],child['bundle_sha256'])
    service.public_test(child['id'],ready['candidate_sha256'])
    with pytest.raises(agent.AgentError):
        service.repair(child['id'],ready['candidate_sha256'])
    assert service.get(p['id'])['public_test']==failure
    assert service.get(p['id'])['repair_slots_used']==2
    revised=service.revise(p['id'],None,'SEMANTIC_REVISION_TASK')
    assert revised['repair_slots_used']==2
    service.publish(revised['id'],revised['proposal_sha256'])
    ready=service.generate(revised['id'],revised['bundle_sha256'])
    service.public_test(ready['id'],ready['candidate_sha256'])
    with pytest.raises(agent.AgentError) as capped:
        service.repair(ready['id'],ready['candidate_sha256'])
    assert capped.value.code=='repair_limit_reached'

def test_m2_unknown_public_test_is_durable_and_cannot_repair_or_replay(tmp_path,monkeypatch):
    service,external,p=m2_candidate(tmp_path,bad=True)
    class Crash(BaseException):pass
    def crash(label):
        if label=='start:published':raise Crash()
    monkeypatch.setattr(agent.analysis,'_boundary',crash)
    with pytest.raises(Crash):service.public_test(p['id'],p['candidate_sha256'])
    monkeypatch.setattr(agent.analysis,'execute',lambda *a,**kw:pytest.fail('replay'))
    restarted=agent.AgentWorkbench(tmp_path,internal=InternalDouble(),external=external)
    assert restarted.get(p['id'])['public_test']['status']=='UNKNOWN_BLOCKED'
    with pytest.raises(agent.AgentError):restarted.public_test(p['id'],p['candidate_sha256'])
    with pytest.raises(agent.AgentError):restarted.repair(p['id'],p['candidate_sha256'])
    with pytest.raises(agent.AgentError):restarted.revise(p['id'],None,'REVISED_UNKNOWN_TASK')
    assert len(external.calls)==1


def test_m2_public_pass_internal_failure_cannot_become_external_repair(tmp_path):
    service,external,p=m2_candidate(tmp_path)
    # A new candidate fixture succeeds for public examples and fails privately.
    # Replace before intake by preparing a fresh task through the original seam.
    external.call=lambda bundle: b'def analyze(df):\n    if int(df["COL_0001"].max()) > 3: raise ValueError("private diagnostic")\n    return {"rows":[{"squared":14}]}\n'
    p=service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X':[7,9]}),'PRIVATE_TASK_X')
    service.publish(p['id'],p['proposal_sha256'])
    p=service.generate(p['id'],p['bundle_sha256'])
    assert service.public_test(p['id'],p['candidate_sha256'])['public_test']['status']=='PASSED'
    assert service.run(p['id'],p['candidate_sha256'])['status']=='EXECUTION_FAILED'
    with pytest.raises(agent.AgentError) as error:service.repair(p['id'],p['candidate_sha256'])
    assert error.value.code=='repair_unavailable'
    assert not list((tmp_path/'claims').glob('repair-*'))

def test_m2_public_failure_proof_cannot_be_retargeted_to_identical_other_run(tmp_path):
    service,external,first=m2_candidate(tmp_path,bad=True)
    second=service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X':[40,50]}),'PRIVATE_TASK_X')
    service.publish(second['id'],second['proposal_sha256'])
    second=service.generate(second['id'],second['bundle_sha256'])
    a=service.public_test(first['id'],first['candidate_sha256'])['public_test']
    b=service.public_test(second['id'],second['candidate_sha256'])['public_test']
    assert a['candidate_sha256']==b['candidate_sha256']
    path=tmp_path/'claims'/('public-test-'+first['id']+'.json')
    claim=json.loads(path.read_bytes());claim['run_id']=b['run_id']
    path.write_bytes(agent.storage._bytes(claim))
    for action in (lambda:service.get(first['id']),
                   lambda:service.repair(first['id'],first['candidate_sha256'])):
        with pytest.raises(agent.AgentError) as error:action()
        assert error.value.code=='public_test_association_changed'

def test_m2_duplicate_repair_coordinators_cannot_branch(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    service,external,p=m2_candidate(tmp_path,bad=True)
    service.public_test(p['id'],p['candidate_sha256'])
    barrier=threading.Barrier(2)
    def prepare():
        coordinator=agent.AgentWorkbench(tmp_path,internal=InternalDouble(),external=external)
        barrier.wait()
        try:return coordinator.repair(p['id'],p['candidate_sha256'])['id']
        except agent.AgentError:return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda _:prepare(),range(2)))
    assert len([r for r in results if r])==1
    assert len(list((tmp_path/'claims').glob('repair-family-*')))==1
    assert len(external.calls)==1


@pytest.mark.parametrize('public_status',['FAILED','UNKNOWN_BLOCKED'])
def test_reuse_cannot_bypass_failed_or_unknown_original_public_test(tmp_path,monkeypatch,public_status):
    class ConditionalPublicFailure(ExternalDouble):
        def call(self,bundle):
            self.calls.append(bundle)
            return b'def analyze(df):\n    if int(df["COL_0001"].max()) <= 3: return {"rows":[{"wrong":1}]}\n    return {"rows":[{"squared":int((df["COL_0001"]**2).sum())}]}\n'
    external=ConditionalPublicFailure()
    service=agent.AgentWorkbench(tmp_path,internal=InternalDouble(),external=external)
    p=service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X':[7,9]}),'PRIVATE_TASK_X')
    service.publish(p['id'],p['proposal_sha256'])
    p=service.generate(p['id'],p['bundle_sha256'])
    original=service.run(p['id'],p['candidate_sha256'])
    assert original['status']=='EXECUTED_UNVERIFIED' and original['rows']==[{'squared':130}]
    if public_status=='UNKNOWN_BLOCKED':
        class Crash(BaseException):pass
        original_boundary=agent.analysis._boundary
        def crash(label):
            if label=='start:published':raise Crash()
        monkeypatch.setattr(agent.analysis,'_boundary',crash)
        with pytest.raises(Crash):service.public_test(p['id'],p['candidate_sha256'])
        monkeypatch.setattr(agent.analysis,'_boundary',original_boundary)
    else:
        service.public_test(p['id'],p['candidate_sha256'])
    reopened=agent.AgentWorkbench(tmp_path,internal=InternalDouble(),external=external)
    assert reopened.get(p['id'])['public_test']['status']==public_status
    before={str(path.relative_to(tmp_path)):path.read_bytes()
        for path in tmp_path.rglob('*') if path.is_file()}
    paths={str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*')}
    with pytest.raises(agent.AgentError) as error:
        reopened.reuse(p['id'],pd.DataFrame({'PRIVATE_AMOUNT_X':[8,10]}))
    assert error.value.code=='public_test_not_passed'
    assert len(external.calls)==1
    assert {str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*')}==paths
    assert all((tmp_path/path).read_bytes()==raw for path,raw in before.items())
