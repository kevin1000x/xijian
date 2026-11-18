import base64
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from tools.workbench_agent import AgentWorkbench
from tools.workbench_budget import TokenBudget
from tools.workbench_connectors import ConnectorError
from tools.workbench_cursor_sdk import CursorSDKAdapter, DISALLOWED, VERSION

MODEL = 'composer-2.5'
SOURCE = b'def analyze(df):\r\n    return {"rows":[{"squared":int((df["COL_0001"]**2).sum())}]}\r\n'


class Proposal:
    source = 'local_test_double'

    def readiness(self):
        return {'ready': True, 'live': False}

    def call(self, context):
        return dict(objective='Sum squared values in the opaque amount field.',
            roles=[{'column':'COL_0001','meaning':'amount used by the calculation'}],
            output=dict(columns=[dict(key='squared',label='Squared',type='integer',
                                      nullable=False,restore_from=None)],max_rows=10))


@pytest.mark.parametrize("settlement_fails", [False, True])
def test_sdk_workbench_preserves_exact_bytes_and_consumes_one_invocation(tmp_path, monkeypatch, settlement_fails):
    connector = CursorSDKAdapter(tmp_path/'route', TokenBudget(tmp_path/'budget', ceiling=500),
        tmp_path/'sdk', tmp_path/'node.exe', MODEL, 500)
    modes = []
    monkeypatch.setattr(connector, '_verify_installation', lambda: None)

    def worker(mode, payload=None):
        modes.append(mode)
        if mode == 'offline':
            return dict(status='ready',version=VERSION,tools=[],excluded=DISALLOWED,
                        setting_sources=[],virtual_entries=0,host_file_unavailable=True)
        if mode == 'catalog':
            return dict(status='ready',models=[MODEL])
        if mode == 'prepare':
            assert payload['model'] == MODEL and 'public_bundle' not in payload
            return dict(status='ready',model=MODEL)
        bundle = payload['public_bundle']
        assert 'PRIVATE_' not in bundle
        assert payload['model'] == MODEL
        assert 'Return only original UTF-8' in json.loads(bundle)['program_contract']
        Path(payload['candidate_file']).write_bytes(SOURCE)
        return dict(model=MODEL,tool_events=0,reported_empty_tools=True,observed_tokens=120,
            candidate_b64=base64.b64encode(SOURCE).decode('ascii'),
            candidate_sha256=hashlib.sha256(SOURCE).hexdigest())

    monkeypatch.setattr(connector, '_worker', worker)
    connector.probe()
    if settlement_fails:
        def unavailable(*args):
            raise OSError('synthetic ledger lock')
        monkeypatch.setattr(connector.budget, 'settle', unavailable)
    agent = AgentWorkbench(tmp_path/'agent', internal=Proposal(), external=connector,
        execution_authorization=dict(authorized=True,data_scope='synthetic_only',
            risk_acknowledgement='existing_supervisor_not_hostile_isolation'))
    task = agent.draft(pd.DataFrame({'PRIVATE_AMOUNT':[7,9]}), 'PRIVATE_TEST_LABEL')
    key = task['id']
    agent.publish(key, task['proposal_sha256'])
    generated = agent.generate(key, task['bundle_sha256'])
    assert (agent._path(key)/'candidate.py').read_bytes() == SOURCE
    assert generated['generation_receipt']['model'] == MODEL
    assert agent.public_test(key, generated['candidate_sha256'])['public_test']['status'] == 'PASSED'
    assert agent.run(key, generated['candidate_sha256'])['rows'] == [{'squared':130}]
    assert agent.reuse(key,pd.DataFrame({'PRIVATE_AMOUNT':[2,4]}))['result']['rows'] == [{'squared':20}]
    if settlement_fails:
        assert generated['generation_receipt']['budget_settlement'] == 'unconfirmed'
        assert connector.budget.snapshot()['reserved'] == 500
    else:
        assert connector.budget.snapshot()['observed_tokens'] == 120
    assert (connector.root/'original.py').read_bytes() == SOURCE
    with pytest.raises(ConnectorError) as error:
        connector.call(task['public_bundle'])
    assert error.value.code == 'sdk_not_ready'
    assert modes == ['offline', 'catalog', 'prepare', 'generate']
