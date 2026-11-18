import json

import pytest

from tools.workbench_connectors import ConnectorError
from tools.workbench_cursor import CursorAdapter, CursorTransport, _status_from_output, cursor_policy


class Peer:
    def __init__(self, selected='chosen-model'):
        self.selected = selected
        self.calls = []
        self.session_id = None
        self.expected_model = None

    def request(self, method, params):
        self.calls.append((method, params))
        if method == 'authenticate':
            assert params == {'methodId': 'cursor_login'}
            return {}
        if method == 'session/new':
            assert params['mcpServers'] == []
            return {'sessionId': 'synthetic-1', 'configOptions': self.config('prior-model', 'agent')}
        assert method == 'session/set_config_option'
        mode = 'ask'
        model = self.selected if params['configId'] == 'model' else 'prior-model'
        return {'configOptions': self.config(model, mode)}

    def config(self, model, mode):
        return [
            {'id': 'model', 'category': 'model', 'type': 'select',
             'currentValue': model, 'options': [{'value': 'chosen-model'}, {'value': 'auto'}]},
            {'id': 'mode', 'category': 'mode', 'type': 'select',
             'currentValue': mode, 'options': [{'value': 'ask'}, {'value': 'agent'}]},
        ]


def test_cursor_config_acknowledges_exact_model_and_ask_mode(tmp_path):
    peer = Peer()
    adapter = CursorAdapter(tmp_path, None, {'model': 'chosen-model'})
    adapter._new_session(peer, tmp_path)
    assert peer.expected_model == 'chosen-model'
    assert peer.session_id == 'synthetic-1'
    assert [method for method, _ in peer.calls] == [
        'authenticate', 'session/new', 'session/set_config_option', 'session/set_config_option']


@pytest.mark.parametrize('requested,returned', [
    ('chosen-model', 'another-model'), ('auto', 'auto'), ('not-listed', 'not-listed')])
def test_cursor_refuses_fallback_and_auto(tmp_path, requested, returned):
    adapter = CursorAdapter(tmp_path, None, {'model': requested})
    with pytest.raises(ConnectorError):
        adapter._new_session(Peer(returned), tmp_path)


def test_cursor_transport_only_accepts_text_candidate_and_denies_tools():
    client = CursorTransport.__new__(CursorTransport)
    client.session_id = 'synthetic-1'
    client.expected_model = 'chosen-model'
    client._candidate_chunks = []
    client._candidate_size = 0
    params = {'sessionId': client.session_id}
    assert client.client_request('session/request_permission', params) == {
        'outcome': {'outcome': 'cancelled'}}
    with pytest.raises(ConnectorError):
        client.client_request('fs/read_text_file', params)
    with pytest.raises(ConnectorError):
        client._session_update(dict(params, update={'sessionUpdate': 'tool_call'}))
    with pytest.raises(ConnectorError):
        client._session_update(dict(params, update={
            'sessionUpdate': 'config_option_update',
            'configOptions': Peer().config('changed-model', 'ask')}))
    for fragment in ('def analyze(df):\r\n', '    return {"rows":[]}\r\n'):
        client._session_update(dict(params, update={
            'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': fragment}}))
    assert client.candidate_text() == b'def analyze(df):\r\n    return {"rows":[]}\r\n'


def test_cursor_policy_covers_workspace_and_windows_drives():
    policy = cursor_policy()['permissions']
    assert not policy['allow']
    assert {'Shell(*)', 'Read(**)', 'Write(**)', 'Read(C:/**)', 'Write(Z:/**)',
            'WebFetch(*)', 'Mcp(*:*)'}.issubset(policy['deny'])


class TextPeer(Peer):
    def __init__(self):
        super().__init__()
        self.candidate = b'def analyze(df):\r\n    return {"rows":[{"squared":int((df["COL_0001"]**2).sum())}]}\r\n'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def initialize(self):
        pass

    def request(self, method, params):
        if method == 'session/prompt':
            self.calls.append((method, params))
            assert self.expected_model == 'chosen-model'
            assert 'PRIVATE_' not in json.dumps(params)
            return {'stopReason': 'end_turn'}
        return super().request(method, params)

    def candidate_text(self):
        return self.candidate


def test_cursor_workbench_approval_exact_program_and_reuse(tmp_path):
    import pandas as pd
    from tools.workbench_agent import AgentWorkbench
    from tools.workbench_budget import TokenBudget

    class Internal:
        source = 'local_test_double'

        def readiness(self):
            return {'ready': True, 'live': False}

        def call(self, payload):
            return dict(objective='Compute squared total', roles=[{'column': 'COL_0001', 'meaning': 'measure'}],
                output=dict(columns=[dict(key='squared', label='Squared',
                    type='integer', nullable=False, restore_from=None)], max_rows=10))

    class SyntheticCursor(CursorAdapter):
        def _generation_policy_verified(self):
            return True

        def _runtime_policy(self):
            return {'native': 'cursor', 'candidate_mode': 'text'}

        def _verified_executable(self):
            return self.root/'node.exe'

        def _login_status(self, executable, env):
            return 'authenticated'

        def _open(self, workspace):
            return peer

    peer = TextPeer()
    budget = TokenBudget(tmp_path/'budget')
    connector = SyntheticCursor(tmp_path/'cursor', budget,
        {'model':'chosen-model','authorized':True,'data_scope':'public_bundle_only',
         'allocation':500,'max_invocations':1})
    connector._authentication = 'authenticated'
    connector._protocol_checked = True
    connector._models = ('chosen-model',)
    agent = AgentWorkbench(tmp_path/'tasks', internal=Internal(), external=connector,
        execution_authorization={'authorized':True,'data_scope':'synthetic_only',
            'risk_acknowledgement':'existing_supervisor_not_hostile_isolation'})
    task = agent.draft(pd.DataFrame({'PRIVATE_AMOUNT':[7,9]}),'PRIVATE_TASK')
    assert not peer.calls
    key = task['id']
    agent.publish(key, task['proposal_sha256'])
    result = agent.generate(key, task['bundle_sha256'])
    assert result['generation_receipt']['session_selection'] == {
        'model':'chosen-model','mode':'ask','acknowledgement':'acp_session_config'}
    assert (agent._path(key)/'candidate.py').read_bytes() == peer.candidate
    assert agent.public_test(key, result['candidate_sha256'])['public_test']['status'] == 'PASSED'
    assert agent.run(key, result['candidate_sha256'])['rows'] == [{'squared':130}]
    assert agent.reuse(key,pd.DataFrame({'PRIVATE_AMOUNT':[2,4]}))['result']['rows'] == [{'squared':20}]
    assert [method for method, _ in peer.calls].count('session/prompt') == 1
    assert budget.snapshot()['charged'] == 500


@pytest.mark.parametrize('payload,status', [
    ({'status':'authenticated','isAuthenticated':True}, 'authenticated'),
    ({'status':'unauthenticated','isAuthenticated':False}, 'unauthenticated'),
    ({'status':'partially-authenticated','isAuthenticated':False}, 'unauthenticated'),
    ({'status':'authenticated','isAuthenticated':False}, 'unknown'),
])
def test_cursor_accepts_structured_authentication_without_user_details(payload, status):
    assert _status_from_output(json.dumps(payload)) == status


def test_unbounded_native_cursor_refuses_generation_before_process_start(tmp_path):
    private = {'synthetic': True}
    cursor = CursorAdapter(tmp_path, None, {})
    with pytest.raises(ConnectorError) as error:
        cursor.call(private)
    assert error.value.code == 'cursor_native_tool_policy_unverified'
    assert list(tmp_path.iterdir()) == []
