import hashlib
import json
import os
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import pytest

from tools.workbench_budget import TokenBudget
from tools.workbench_connectors import ConnectorError
from tools.workbench_claude import ClaudeCodeAdapter, REVIEWED_VERSION


def adapter(tmp_path):
    exe = tmp_path / 'claude.exe'
    exe.write_bytes(b'synthetic executable')
    profile = dict(executable=str(exe), version=REVIEWED_VERSION,
        executable_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
        entry_id='synthetic', model='claude-sonnet-4-6', authorized=True,
        data_scope='public_bundle_only', allocation=500, max_invocations=1, timeout_seconds=30)
    value = ClaudeCodeAdapter(tmp_path / 'records', TokenBudget(tmp_path / 'budget', 5000), profile)
    value._compatible = value._authenticated = True
    return value


def stream(candidate='def analyze(df):\r\n    return df\r\n'):
    return [dict(type='system', subtype='init', tools=[], mcp_servers=[]),
        dict(type='assistant', message=dict(content=[dict(type='text', text=candidate)], stop_reason='end_turn')),
        dict(type='result', subtype='success', is_error=False, result=candidate, num_turns=1,
            stop_reason='end_turn')]


def encode(events):
    return b'\n'.join(json.dumps(event).encode('utf-8') for event in events)


def test_scope_authority_identity_and_budget_before_launch(tmp_path, monkeypatch):
    value = adapter(tmp_path)
    monkeypatch.setattr(value, '_run', lambda *a, **k: pytest.fail('unexpected launch'))
    value.profile['authorized'] = False
    with pytest.raises(ConnectorError): value.call({'synthetic': True})
    assert value.budget.snapshot()['charged'] == 0
    value.profile['authorized'] = True
    Path(value.profile['executable']).write_bytes(b'changed')
    assert 'reviewed_executable_identity' in value.readiness()['missing']


def test_managed_policy_requires_review_before_any_launch(tmp_path, monkeypatch):
    value = adapter(tmp_path)
    policy = tmp_path / 'machine' / 'ClaudeCode'
    policy.mkdir(parents=True)
    (policy / 'managed-settings.json').write_text('{"hooks":{"synthetic":true}}')
    monkeypatch.setenv('ProgramFiles', str(policy.parent))
    monkeypatch.setattr(value, '_run', lambda *a, **k: pytest.fail('unexpected launch'))
    assert 'managed_policy_review' in value.readiness()['missing']
    assert not value.probe()['compatible']
    with pytest.raises(ConnectorError): value.call({'synthetic':True})


def test_oversized_input_does_not_consume_allowance(tmp_path, monkeypatch):
    value = adapter(tmp_path)
    monkeypatch.setattr(value, '_run', lambda *a, **k: pytest.fail('unexpected launch'))
    with pytest.raises(ConnectorError) as caught: value.call({'synthetic':'x' * 65536})
    assert caught.value.code == 'public_bundle_too_large'
    assert value.budget.snapshot()['charged'] == 0
    assert value._used() == 0


def test_original_bytes_environment_and_conservative_settlement(tmp_path, monkeypatch):
    value = adapter(tmp_path)
    seen = []
    def run(args, workspace, operation, payload=None, timeout=30):
        seen.append((args, payload))
        return 0, encode(stream())
    monkeypatch.setattr(value, '_run', run)
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'NODE_OPTIONS'):
        monkeypatch.setenv(key, 'unapproved-inherited-value')
    result = value.call({'schema': ['COL_0001']})
    assert result == stream()[-1]['result'].encode('utf-8')
    args, payload = seen[0]
    assert args[args.index('--tools') + 1] == ''
    assert args[args.index('--setting-sources') + 1] == ''
    assert '--safe-mode' in args and '--strict-mcp-config' in args
    assert '--no-session-persistence' in args and '--disable-slash-commands' in args
    assert str(value.root).encode() not in payload
    env = value._environment()
    assert all(env.get(k) != 'unapproved-inherited-value' for k in
        ('ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_BASE_URL','NODE_OPTIONS'))
    assert env['CLAUDE_CODE_MAX_RETRIES'] == '0'
    assert env['USERPROFILE'] != os.environ.get('USERPROFILE')
    assert value.budget.snapshot()['charged'] == 500
    assert value.last_receipt['candidate_sha256'] == hashlib.sha256(result).hexdigest()
    with pytest.raises(ConnectorError): value.call({'schema': ['COL_0001']})
    assert len(seen) == 1


@pytest.mark.parametrize('change', ['tool', 'init_tools', 'duplicate', 'error', 'truncate', 'mismatch', 'malformed'])
def test_invalid_lifecycle_never_delivers(tmp_path, monkeypatch, change):
    value = adapter(tmp_path)
    events = stream()
    if change == 'tool': events[1]['message']['content'].append(dict(type='tool_use', name='Bash'))
    elif change == 'init_tools': events[0]['tools'] = ['Read']
    elif change == 'duplicate': events.append(events[-1])
    elif change == 'error': events[-1]['is_error'] = True
    elif change == 'truncate': events[-1]['stop_reason'] = 'max_tokens'
    elif change == 'mismatch': events[-1]['result'] += '# changed'
    elif change == 'malformed': events.append(None)
    monkeypatch.setattr(value, '_run', lambda *a, **k: (0, encode(events)))
    with pytest.raises(ConnectorError): value.call({'synthetic': True})
    assert value.budget.snapshot()['charged'] == 500
    assert value.last_receipt['status'] == 'unresolved'


def test_timeout_stops_owned_process(tmp_path):
    value = adapter(tmp_path)
    value.profile['executable'] = sys.executable
    start = time.monotonic()
    with pytest.raises(ConnectorError) as caught:
        value._run(['-I', '-c', 'import time; time.sleep(20)'], tmp_path, 'test-stall',
            b'x' * 65536, timeout=.2)
    assert caught.value.code == 'claude_timeout'
    assert time.monotonic() - start < 6
    assert value.operation() is None


def test_cancel_stops_owned_process(tmp_path):
    value = adapter(tmp_path)
    value.profile['executable'] = sys.executable
    errors = []
    def run():
        try: value._run(['-I', '-c', 'import time; time.sleep(20)'], tmp_path, 'test-cancel', timeout=5)
        except ConnectorError as error: errors.append(error.code)
    thread = threading.Thread(target=run); thread.start()
    deadline = time.monotonic() + 3
    while value.operation() is None and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert value.cancel()['cancelled']
    thread.join(5)
    assert not thread.is_alive() and errors == ['claude_cancelled']


@pytest.mark.skipif(not os.environ.get('XIJIAN_TEST_CLAUDE_EXE'), reason='explicit native Claude loopback test')
@pytest.mark.parametrize('fail_response', [False, True], ids=['generation', 'no-http-retry'])
def test_native_cli_local_anthropic_no_account(tmp_path, fail_response):
    candidate = 'def analyze(df):\n    return {"rows": [{"squared": int((df["COL_0001"] ** 2).sum())}]}\n'
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            requests.append((self.path, raw))
            if fail_response:
                payload = b'{"type":"error","error":{"type":"api_error","message":"synthetic local failure"}}'
                self.send_response(500); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
                return
            events = [
                dict(type='message_start', message=dict(id='msg_local', type='message', role='assistant',
                    model='claude-sonnet-4-6', content=[], stop_reason=None, stop_sequence=None,
                    usage=dict(input_tokens=12, output_tokens=0))),
                dict(type='content_block_start', index=0, content_block=dict(type='text', text='')),
                dict(type='content_block_delta', index=0, delta=dict(type='text_delta', text=candidate)),
                dict(type='content_block_stop', index=0),
                dict(type='message_delta', delta=dict(stop_reason='end_turn', stop_sequence=None), usage=dict(output_tokens=20)),
                dict(type='message_stop')]
            payload = ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    class LoopbackAdapter(ClaudeCodeAdapter):
        loopback_enabled = False
        def _environment(self):
            env = super()._environment()
            env['ANTHROPIC_BASE_URL'] = 'http://127.0.0.1:%d' % server.server_port
            if self.loopback_enabled: env['ANTHROPIC_API_KEY'] = 'synthetic-local-test-only'
            return env
        def _run(self, *args, **kwargs):
            result = super()._run(*args, **kwargs)
            self.trace = getattr(self, 'trace', []) + [(args[0], result)]
            return result
    exe = Path(os.environ['XIJIAN_TEST_CLAUDE_EXE'])
    profile = dict(executable=str(exe), version=REVIEWED_VERSION,
        executable_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(), entry_id='native-local-test',
        model='claude-sonnet-4-6', authorized=True, data_scope='public_bundle_only',
        allocation=500, max_invocations=1, timeout_seconds=45)
    value = LoopbackAdapter(tmp_path / 'records', TokenBudget(tmp_path / 'budget', 1000), profile)
    try:
        probe = value.probe()
        assert probe['compatible'], repr(value.trace)
        assert probe['authentication'] == 'unauthenticated'
        value.loopback_enabled = True
        value._authenticated = True
        env = value._environment()
        (Path(env['CLAUDE_CONFIG_DIR']) / 'CLAUDE.md').write_text('PERSONAL_CONTEXT_MUST_NOT_LEAK')
        (Path(env['CLAUDE_CONFIG_DIR']) / 'settings.json').write_text(json.dumps({
            'env': {'SYNTHETIC_PRIVATE_SETTING':'MUST_NOT_LOAD'},
            'hooks': {'SessionStart':[{'hooks':[{'type':'command','command':'echo HOOK_MUST_NOT_RUN'}]}]}}))
        from tools.workbench_agent import AgentWorkbench, AgentError
        class InternalDouble:
            source = 'local_test_double'
            def readiness(self): return dict(ready=True, live=False, source=self.source, missing=[])
            def call(self, payload):
                return {'objective':'Compute squared total', 'roles':[{'column':'COL_0001','meaning':'amount'}],
                    'output':{'columns':[{'key':'squared','label':'Squared total','type':'integer',
                        'nullable':False,'restore_from':None}], 'max_rows':10}}
        import pandas as pd
        service = AgentWorkbench(tmp_path / 'workbench', internal=InternalDouble(), external=value,
            execution_authorization={'authorized':True,'data_scope':'synthetic_only',
                'risk_acknowledgement':'existing_supervisor_not_hostile_isolation'})
        proposal = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X':[7,9]}), 'PRIVATE_TASK_X')
        service.publish(proposal['id'], proposal['proposal_sha256'])
        if fail_response:
            with pytest.raises(AgentError) as caught:
                service.generate(proposal['id'], proposal['bundle_sha256'])
            assert caught.value.code == 'generation_unknown'
            assert value.last_receipt['error_code'] == 'claude_unresolved'
            assert len(requests) == 1
            assert value.budget.snapshot()['charged'] == 500
            assert value.last_receipt['status'] == 'unresolved'
            if os.environ.get('XIJIAN_TEST_CLAUDE_RECEIPT'):
                Path(os.environ['XIJIAN_TEST_CLAUDE_RECEIPT'] + '.http500.json').write_text(json.dumps(dict(
                    version=REVIEWED_VERSION, executable_sha256=profile['executable_sha256'],
                    model_requests=len(requests), response_status=500, automatic_retries=0,
                    terminal='unresolved', charged=500, account_used=False, paid_provider_calls=0,
                    status='passed'), indent=2))
            return
        try: generated = service.generate(proposal['id'], proposal['bundle_sha256'])
        except AgentError: pytest.fail('Isolated CLI diagnostic: '+repr(value.trace[-1]))
        assert generated['candidate_sha256'] == hashlib.sha256(candidate.encode()).hexdigest()
        result = service.run(proposal['id'], generated['candidate_sha256'])
        assert result['rows'] == [{'squared':130}]
        assert (tmp_path/'workbench'/'runs'/result['run_id']/'candidate.py').read_bytes() == candidate.encode()
        reused = service.reuse(proposal['id'], pd.DataFrame({'PRIVATE_AMOUNT_X':[2,4]}))
        assert reused['result']['rows'] == [{'squared':20}]
        assert reused['candidate_sha256'] == generated['candidate_sha256']
        assert AgentWorkbench(tmp_path/'workbench').get(reused['id'])['result']['rows'] == [{'squared':20}]
        assert len(requests) == 1
        path, raw = requests[0]
        assert path.split('?')[0] == '/v1/messages'
        request = json.loads(raw)
        assert not request.get('tools')
        assert request['model'] == profile['model']
        for marker in (b'PRIVATE_AMOUNT_X', b'PRIVATE_TASK_X', b'PERSONAL_CONTEXT_MUST_NOT_LEAK',
                b'HOOK_MUST_NOT_RUN', b'MUST_NOT_LOAD', str(value.root).encode()):
            assert marker not in raw
        if os.environ.get('XIJIAN_TEST_CLAUDE_RECEIPT'):
            Path(os.environ['XIJIAN_TEST_CLAUDE_RECEIPT']).write_text(json.dumps(dict(
                version=REVIEWED_VERSION, executable_sha256=profile['executable_sha256'],
                model_requests=len(requests), tools=request.get('tools'), max_tokens=request.get('max_tokens'),
                candidate_sha256=generated['candidate_sha256'], executed_result=130, reused_result=20,
                restored=True, account_used=False, paid_provider_calls=0, status='passed'), indent=2))
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
