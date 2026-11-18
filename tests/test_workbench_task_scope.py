import json

import pandas as pd
import pytest

from tools import workbench_agent as agent
from test_workbench_agent import ExternalDouble, InternalDouble
from test_workbench_agent import M2External


class MeasureProposal(InternalDouble):
    def call(self, payload):
        proposal = super().call(payload)
        proposal['objective'] = 'Square the amounts in COL_0002'
        proposal['roles'] = [{'column': 'COL_0002', 'meaning': 'measure'}]
        return proposal


def input_table():
    return pd.DataFrame({'LOCAL_RATIO': [0.25, 0.75], 'LOCAL_AMOUNT': [5, 8],
                         'LOCAL_GROUP': ['SAMPLE-A', 'SAMPLE-B']})


def test_task_fields_follow_approval_through_execution_reuse_and_reopen(tmp_path):
    external = ExternalDouble()
    service = agent.AgentWorkbench(tmp_path, internal=MeasureProposal(), external=external)
    draft = service.draft(input_table(), 'LOCAL_TASK')
    key = draft['id']
    bundle = draft['public_bundle']
    assert bundle['schema']['fields'] == [{'name': 'COL_0001', 'dtype': 'int64'}]
    assert bundle['roles'] == [{'column': 'COL_0001', 'meaning': 'measure'}]
    assert 'COL_0002' not in draft['public_preview']
    assert all(list(row) == ['COL_0001'] for row in bundle['examples'])
    assert 'LOCAL_' not in draft['public_preview']
    assert not external.calls

    preview = service.disclosure(key, draft['proposal_sha256'])
    assert preview['fields'] == [{'local_name': 'LOCAL_AMOUNT', 'symbol': 'COL_0001',
                                  'dtype': 'int64', 'meaning': 'measure', 'restores': []}]
    service.publish(key, draft['proposal_sha256'])
    candidate = service.generate(key, draft['bundle_sha256'])
    assert external.calls == [bundle]
    assert service.public_test(key, candidate['candidate_sha256'])['public_test']['status'] == 'PASSED'
    result = service.run(key, candidate['candidate_sha256'])
    assert result['rows'] == [{'squared': 89}]
    request = json.loads((tmp_path / 'runs' / result['run_id'] / 'request.json').read_bytes())
    assert request['records'] == [{'COL_0001': 5}, {'COL_0001': 8}]
    reopened = agent.AgentWorkbench(tmp_path, internal=MeasureProposal(), external=external)
    assert reopened.recover(key)['rows'] == result['rows']
    for frame in [pd.DataFrame({'LOCAL_AMOUNT': [2, 4]}),
                  pd.DataFrame({'EXTRA': ['new', 'other'], 'LOCAL_AMOUNT': [2, 4]})]:
        reused = reopened.reuse(key, frame)
        assert reused['candidate_sha256'] == candidate['candidate_sha256']
        assert reopened.recover(reused['id'])['rows'] == [{'squared': 20}]
    for frame in [pd.DataFrame({'EXTRA': [2, 4]}), pd.DataFrame({'LOCAL_AMOUNT': [2.5, 4.5]})]:
        with pytest.raises(agent.AgentError) as error:
            reopened.reuse(key, frame)
        assert error.value.code == 'reuse_input_incompatible'
    assert external.calls == [bundle]


def test_projected_repair_and_scoped_text_restore(tmp_path):
    class GroupProposal(MeasureProposal):
        def call(self, payload):
            result = super().call(payload)
            result['roles'].insert(0, {'column': 'COL_0003', 'meaning': 'category'})
            result['output']['columns'][0].update(key='group', type='string', restore_from='COL_0003')
            return result

    class TextExternal(M2External):
        def call(self, bundle):
            if 'repair' not in bundle:
                return super().call(bundle)
            self.calls.append(bundle)
            return b'def analyze(df):\n    return {"rows": [{"group": df["COL_0002"].iloc[0]}]}\n'

    ext = TextExternal(bad=True)
    service = agent.AgentWorkbench(tmp_path, internal=GroupProposal(), external=ext)
    p = service.draft(input_table(), 'LOCAL_TASK')
    bundle = p['public_bundle']
    assert bundle['output']['columns'][0]['restore_from'] == 'COL_0002'
    view = service.disclosure(p['id'], p['proposal_sha256'])
    assert view['fields'][1]['restores'] == ['group']
    service.publish(p['id'], p['proposal_sha256'])
    first = service.generate(p['id'], p['bundle_sha256'])
    assert service.public_test(p['id'], first['candidate_sha256'])['public_test']['status'] == 'FAILED'
    repair = service.repair(p['id'], first['candidate_sha256'])
    assert {k: v for k, v in repair['public_bundle'].items() if k != 'repair'} == bundle
    assert len(ext.calls) == 1
    with pytest.raises(agent.AgentError):
        service.generate(repair['id'], repair['bundle_sha256'])
    service.publish(repair['id'], repair['proposal_sha256'])
    fixed = service.generate(repair['id'], repair['bundle_sha256'])
    assert ext.calls == [bundle, repair['public_bundle']]
    assert 'LOCAL_' not in repair['public_preview'] and 'COL_0003' not in repair['public_preview']
    service.public_test(fixed['id'], fixed['candidate_sha256'])
    assert service.run(fixed['id'], fixed['candidate_sha256'])['rows'] == [{'group': 'SAMPLE-A'}]
    reused = service.reuse(fixed['id'], input_table().drop(columns='LOCAL_RATIO').iloc[::-1])
    assert reused['result']['rows'] == [{'group': 'SAMPLE-B'}]
    assert reused['candidate_sha256'] == fixed['candidate_sha256']
    assert service.get(p['id'])['public_test']['status'] == 'FAILED'


def test_legacy_proposal_digest_and_full_table_survive_reopening(tmp_path):
    # Construct a fresh synthetic record in the pre-scope format.
    class Both(InternalDouble):
        def call(self, payload):
            result = super().call(payload)
            result['roles'].append({'column': 'COL_0002', 'meaning': 'category'})
            return result
    ext = ExternalDouble()
    service = agent.AgentWorkbench(tmp_path, internal=Both(), external=ext)
    p = service.draft(input_table().drop(columns='LOCAL_RATIO'), 'LOCAL_TASK')
    path = tmp_path / 'tasks' / p['id'] / 'record.json'
    saved = json.loads(path.read_bytes())
    del saved['task_binding_sha256']
    saved['public_bundle']['roles'].pop()
    saved['bundle_sha256'] = agent.digest(saved['public_bundle'])
    saved['public_preview'] = agent.storage._bytes(saved['public_bundle']).decode('utf-8')
    saved['proposal_sha256'] = agent.digest({'bundle': saved['public_bundle'], 'route': saved['external_route'],
        'private_sha256': saved['private_sha256'], 'internal_route': saved['internal_route']})
    path.write_bytes(agent.storage._bytes(saved))
    reopened = agent.AgentWorkbench(tmp_path, external=ext)
    view = reopened.disclosure(p['id'], saved['proposal_sha256'])
    assert view['legacy'] and len(view['fields']) == 2
    reopened.publish(p['id'], saved['proposal_sha256'])
    original = reopened.generate(p['id'], saved['bundle_sha256'])
    reopened.run(p['id'], original['candidate_sha256'])
    assert reopened.recover(p['id'])['rows'] == [{'squared': 89}]
    with pytest.raises(agent.AgentError) as error:
        reopened.reuse(p['id'], pd.DataFrame({'LOCAL_AMOUNT': [1, 2]}))
    assert error.value.code == 'reuse_input_incompatible'


def test_undeclared_output_or_text_dependency_requires_new_proposal(tmp_path):
    class Undeclared(MeasureProposal):
        def call(self, payload):
            proposal = super().call(payload)
            if self.text:
                proposal['objective'] += ' grouped by COL_0003'
            else:
                proposal['output']['columns'][0].update(type='string', restore_from='COL_0003')
            return proposal

    internal, external = Undeclared(), ExternalDouble()
    service = agent.AgentWorkbench(tmp_path, internal=internal, external=external)
    for internal.text in [False, True]:
        with pytest.raises(agent.AgentError) as error:
            service.draft(input_table(), 'LOCAL_TASK')
        assert error.value.code == 'undeclared_task_field'
    assert not external.calls


def test_preview_always_uses_verified_bundle_bytes(tmp_path):
    service = agent.AgentWorkbench(tmp_path, internal=MeasureProposal(), external=ExternalDouble())
    proposal = service.draft(input_table(), 'LOCAL_TASK')
    path = tmp_path / 'tasks' / proposal['id'] / 'record.json'
    saved = json.loads(path.read_bytes())
    saved['public_preview'] = '{"objective":"stale preview"}'
    path.write_bytes(agent.storage._bytes(saved))
    displayed = service.get(proposal['id'])
    assert displayed['public_preview'].encode('utf-8') == agent.storage._bytes(saved['public_bundle'])


def test_damaged_scope_blocks_sending_and_repair_claims(tmp_path):
    ext = M2External(bad=True)
    service = agent.AgentWorkbench(tmp_path, internal=MeasureProposal(), external=ext)
    p = service.draft(input_table(), 'LOCAL_TASK')
    key = p['id']
    service.publish(key, p['proposal_sha256'])
    scope = tmp_path / 'tasks' / key / ('binding-' + p['task_binding_sha256'] + '.json')
    original = scope.read_bytes()
    scope.write_bytes(b'{}')
    with pytest.raises(agent.AgentError):
        service.generate(key, p['bundle_sha256'])
    assert not ext.calls
    scope.write_bytes(original)
    candidate = service.generate(key, p['bundle_sha256'])
    assert service.public_test(key, candidate['candidate_sha256'])['public_test']['status'] == 'FAILED'
    scope.write_bytes(b'{}')
    with pytest.raises(agent.AgentError):
        service.repair(key, candidate['candidate_sha256'])
    assert len(ext.calls) == 1
    assert not list((tmp_path / 'claims').glob('repair-*'))
