import hashlib
import json
import os
import shutil
import threading
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tools.workbench_budget import TokenBudget
from tools.workbench_connectors import ConnectorError
from tools.workbench_codex import CodexAdapter, REVIEWED_VERSION


def adapter(tmp_path):
    exe = tmp_path / 'codex.exe'
    exe.write_bytes(b'synthetic executable')
    profile = dict(executable=str(exe), version=REVIEWED_VERSION,
        executable_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
        entry_id='synthetic', model='test-model', authorized=True,
        data_scope='public_bundle_only', allocation=500, max_invocations=1,
        timeout_seconds=10)
    value = CodexAdapter(tmp_path / 'records', TokenBudget(tmp_path / 'budget', 5000), profile)
    value._compatible = True
    value._authenticated = True
    value._features = ['hooks', 'plugins', 'shell_tool', 'multi_agent', 'skip_host_skill_discovery']
    return value


def test_no_authority_no_invocation(tmp_path, monkeypatch):
    value = adapter(tmp_path)
    value.profile['authorized'] = False
    monkeypatch.setattr(value, '_run', lambda *a, **k: pytest.fail('must not start'))
    with pytest.raises(ConnectorError):
        value.call({'synthetic': True})
    assert value.budget.snapshot()['charged'] == 0


def test_changed_binary_rejected(tmp_path):
    value = adapter(tmp_path)
    Path(value.profile['executable']).write_bytes(b'changed')
    assert not value.readiness()['ready']


def test_added_external_home_instructions_prevent_launch(tmp_path, monkeypatch):
    value = adapter(tmp_path)
    home = Path(value._environment()['CODEX_HOME'])
    (home/'AGENTS.md').write_text('synthetic unwanted instructions')
    monkeypatch.setattr(value, '_run', lambda *a, **k: pytest.fail('must not launch'))
    assert 'external_home_instructions' in value.readiness()['missing']
    with pytest.raises(ConnectorError): value.call({'synthetic':True})


def test_prompt_write_is_bounded_when_child_does_not_read(tmp_path):
    value = adapter(tmp_path)
    value.profile['executable'] = sys.executable
    start = time.monotonic()
    with pytest.raises(ConnectorError) as caught:
        value._run(['-I','-c','import time;time.sleep(20)'], tmp_path, 'synthetic-stall',
                   b'x'*65536, timeout=.2)
    assert caught.value.code == 'codex_timeout'
    assert time.monotonic() - start < 6
    assert value.operation() is None


def test_cancel_stops_process_and_keeps_terminal_unknown(tmp_path):
    value = adapter(tmp_path)
    value.profile['executable'] = sys.executable
    errors = []
    def run():
        try: value._run(['-I','-c','import time;time.sleep(20)'], tmp_path, 'synthetic-cancel',timeout=5)
        except ConnectorError as error: errors.append(error.code)
    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic()+3
    while value.operation() is None and thread.is_alive() and time.monotonic()<deadline:
        time.sleep(.01)
    assert value.cancel()['cancelled']
    thread.join(5)
    assert not thread.is_alive() and errors == ['codex_cancelled']


def test_original_bytes_scope_and_conservative_settlement(tmp_path, monkeypatch):
    value = adapter(tmp_path)
    candidate = b'def analyze(df):\r\n    return df\r\n'
    seen = []
    def run(args, workspace, operation, payload=None, timeout=30):
        seen.append((args, payload, workspace))
        Path(args[args.index('--output-last-message') + 1]).write_bytes(candidate)
        return 0, b'{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":8}}\n'
    monkeypatch.setattr(value, '_run', run)
    monkeypatch.setenv('OPENAI_API_KEY', 'should-not-pass')
    monkeypatch.setenv('NODE_OPTIONS', 'should-not-pass')
    assert value.call({'shadow_schema': ['field_1']}) == candidate
    args, payload, workspace = seen[0]
    assert '--ignore-user-config' in args and '--ignore-rules' in args
    assert 'features.hooks=false' in args and 'skills.include_instructions=false' in args
    assert str(value.root).encode() not in payload
    assert 'OPENAI_API_KEY' not in value._environment()
    assert 'NODE_OPTIONS' not in value._environment()
    assert value.budget.snapshot()['charged'] == 500
    assert value.last_receipt['candidate_sha256'] == hashlib.sha256(candidate).hexdigest()
    with pytest.raises(ConnectorError):
        value.call({'shadow_schema': ['field_1']})
    assert len(seen) == 1


def test_incomplete_or_tool_event_never_delivers(tmp_path, monkeypatch):
    value = adapter(tmp_path)
    def run(args, workspace, operation, payload=None, timeout=30):
        Path(args[args.index('--output-last-message') + 1]).write_bytes(b'candidate')
        return 0, b'{"type":"item.completed","item":{"type":"command_execution"}}\n{"type":"turn.completed"}\n'
    monkeypatch.setattr(value, '_run', run)
    with pytest.raises(ConnectorError):
        value.call({'synthetic': True})
    assert value.budget.snapshot()['charged'] == 500
    assert value.last_receipt['status'] == 'unresolved'


@pytest.mark.skipif(os.environ.get('XIJIAN_TEST_CODEX_LOOPBACK') != '1', reason='explicit installed CLI loopback test')
def test_installed_cli_local_responses_no_account(tmp_path):
    """Real official CLI, local synthetic model server; zero paid generation."""
    requests = []
    candidate = 'def analyze(df):\n    return {"rows": [{"squared": int((df["COL_0001"] ** 2).sum())}]}\n'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            requests.append((self.path, dict(self.headers), raw))
            message = dict(id='msg_synthetic', type='message', role='assistant',
                status='completed', content=[dict(type='output_text', text=candidate, annotations=[])])
            response = dict(id='resp_synthetic', object='response', status='completed',
                output=[message], usage=dict(input_tokens=12, output_tokens=8, total_tokens=20))
            events = [dict(type='response.created', response=dict(response, status='in_progress', output=[])),
                dict(type='response.output_item.added', output_index=0, item=dict(message, status='in_progress', content=[])),
                dict(type='response.content_part.added', item_id='msg_synthetic', output_index=0, content_index=0,
                    part=dict(type='output_text', text='', annotations=[])),
                dict(type='response.output_text.delta', item_id='msg_synthetic', output_index=0, content_index=0, delta=candidate),
                dict(type='response.output_text.done', item_id='msg_synthetic', output_index=0, content_index=0, text=candidate),
                dict(type='response.output_item.done', output_index=0, item=message),
                dict(type='response.completed', response=response)]
            payload = ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    synthetic_home = tmp_path / 'empty-home'; synthetic_home.mkdir()
    # Deliberately planted synthetic personal context must not reach the request.
    personal = synthetic_home / '.codex'; personal.mkdir()
    (personal / 'AGENTS.md').write_text('PERSONAL_CONTEXT_MUST_NOT_LEAK', encoding='utf-8')
    class LoopbackAdapter(CodexAdapter):
        def _run(self, *args, **kwargs):
            result = super()._run(*args, **kwargs)
            self.test_diagnostic = result
            self.test_trace = getattr(self, 'test_trace', []) + [(args[0], result[0], result[1][:1500])]
            return result
        def _environment(self):
            env = super()._environment()
            env['USERPROFILE'] = str(synthetic_home)
            return env
        def _arguments(self, output):
            args = super()._arguments(output)
            args[-1:-1] = ['-c', 'model_provider="xijian_loopback"', '-c',
                'model_providers.xijian_loopback={name="local synthetic model",base_url="http://127.0.0.1:%d/v1",wire_api="responses",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}' % server.server_port]
            return args
    executable = Path(shutil.which('codex'))
    profile = dict(executable=str(executable), version=REVIEWED_VERSION,
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(), entry_id='loopback-test',
        approval_id='synthetic-local-only', authorized=True, data_scope='public_bundle_only',
        model='gpt-5.1-codex', allocation=500, max_invocations=1, timeout_seconds=30)
    value = LoopbackAdapter(tmp_path / 'records', TokenBudget(tmp_path / 'budget', 1000), profile)
    try:
        probe = value.probe()
        if not probe['compatible']:
            pytest.fail(repr((probe, value._features, value.test_trace)))
        assert probe['authentication'] == 'unauthenticated'
        # Test-only no-auth model server, never exposed in production profiles.
        value._authenticated = True
        from tools.workbench_agent import AgentWorkbench
        class InternalDouble:
            source = 'local_test_double'
            def readiness(self): return {'ready':True,'live':False,'source':self.source,'missing':[]}
            def call(self, payload):
                return {'objective':'Compute squared total',
                    'roles':[{'column':'COL_0001','meaning':'amount'}],
                    'output':{'columns':[{'key':'squared','label':'Squared total',
                        'type':'integer','nullable':False,'restore_from':None}], 'max_rows':10}}
        import pandas as pd
        service = AgentWorkbench(tmp_path/'workbench', internal=InternalDouble(), external=value,
            execution_authorization={'authorized':True,'data_scope':'synthetic_only',
                'risk_acknowledgement':'existing_supervisor_not_hostile_isolation'})
        proposal = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X':[7,9]}), 'PRIVATE_TASK_X')
        service.publish(proposal['id'], proposal['proposal_sha256'])
        try:
            generated = service.generate(proposal['id'], proposal['bundle_sha256'])
        except ConnectorError:
            pytest.fail('Isolated loopback CLI diagnostic: '+repr(value.test_diagnostic))
        assert generated['candidate_sha256'] == hashlib.sha256(candidate.encode('utf-8')).hexdigest()
        result = service.run(proposal['id'], generated['candidate_sha256'])
        assert result['rows'] == [{'squared':130}]
        assert (tmp_path/'workbench'/'runs'/result['run_id']/'candidate.py').read_bytes() == candidate.encode('utf-8')
        reused = service.reuse(proposal['id'], pd.DataFrame({'PRIVATE_AMOUNT_X':[2,4]}))
        assert reused['result']['rows'] == [{'squared':20}]
        assert reused['candidate_sha256'] == generated['candidate_sha256']
        assert reused['external_selection']['version'] == REVIEWED_VERSION
        restarted = AgentWorkbench(tmp_path/'workbench')
        assert restarted.get(reused['id'])['result']['rows'] == [{'squared':20}]
        assert len(requests) == 1
        path, headers, body = requests[0]
        assert path == '/v1/responses'
        assert not any(k.lower() == 'authorization' for k in headers)
        assert b'PERSONAL_CONTEXT_MUST_NOT_LEAK' not in body
        assert str(value.root).encode('utf-8') not in body
        request = json.loads(body)
        assert not request.get('tools'), request.get('tools')
        assert 'COL_0001' in json.dumps(request['input'])
        assert b'PRIVATE_AMOUNT_X' not in body and b'PRIVATE_TASK_X' not in body
    finally:
        server.shutdown(); server.server_close(); thread.join(5)


def test_current_desktop_codex_version_has_reviewed_identity(tmp_path):
    value = adapter(tmp_path)
    value.profile['version'] = '0.155.0-alpha.9'
    assert value._identity_ok()