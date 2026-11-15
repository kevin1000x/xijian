"""Opt-in exact official OpenCode executable, synthetic localhost model only."""
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import threading

import pytest

from tools import workbench_acp
from tools.workbench_acp import ACPAdapter
from tools.workbench_budget import TokenBudget
from tools import workbench_opencode


def test_production_environment_uses_dedicated_login_and_disables_tools(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'PERSONAL_MUST_NOT_INHERIT')
    env = workbench_opencode.environment(tmp_path, tmp_path/'dedicated', 'openai/gpt-test', 600)
    assert 'OPENAI_API_KEY' not in env
    assert Path(env['XDG_DATA_HOME']).parent == tmp_path/'dedicated'
    assert Path(env['XDG_CONFIG_HOME']).parent == tmp_path/'opencode-state'
    config = json.loads(env['OPENCODE_CONFIG_CONTENT'])
    assert config['permission'] == {'*':'deny'}
    assert config['enabled_providers'] == ['openai']
    assert config['agent']['title']['disable']
    assert env['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'] == '600'
    assert env['OPENCODE_DISABLE_DEFAULT_PLUGINS'] == 'true'


@pytest.mark.parametrize('output,model,status',[
    ('│  ● OpenAI api\n└ 1 credentials', 'openai/test', 'authenticated'),
    ('│  ● Anthropic api\n└ 1 credentials', 'openai/test', 'unauthenticated'),
    ('│  ● OpenAI oauth\n└ 1 credentials', 'openai/test', 'unauthenticated'),
    ('└ 0 credentials', 'openai/test', 'unauthenticated'),
])
def test_only_selected_provider_api_credential_is_accepted(output, model, status):
    assert workbench_opencode.authentication_from_list(output,model) == status


def isolated_environment(root, port):
    root = Path(root)
    env = {key:os.environ[key] for key in ('SystemRoot', 'WINDIR', 'COMSPEC') if key in os.environ}
    for key in ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP','XDG_CONFIG_HOME',
                'XDG_DATA_HOME','XDG_CACHE_HOME','XDG_STATE_HOME'):
        folder = root/key.lower()
        folder.mkdir(parents=True, exist_ok=True)
        env[key] = str(folder)
    env['OPENCODE_TEST_HOME'] = env['HOME']
    env['OPENCODE_TEST_MANAGED_CONFIG_DIR'] = str(root/'empty-managed')
    env['PATH'] = os.environ.get('SystemRoot', 'C:\\Windows') + '\\System32'
    env.update(HTTP_PROXY='http://127.0.0.1:1', HTTPS_PROXY='http://127.0.0.1:1',
        NO_PROXY='127.0.0.1,localhost', npm_config_registry='http://127.0.0.1:1', npm_config_offline='true')
    for flag in ('PURE','DISABLE_DEFAULT_PLUGINS','DISABLE_EXTERNAL_SKILLS','DISABLE_LSP_DOWNLOAD',
                 'DISABLE_CLAUDE_CODE','DISABLE_MODELS_FETCH','DISABLE_AUTOUPDATE','DISABLE_PROJECT_CONFIG',
                 'DISABLE_AUTOCOMPACT','DISABLE_PRUNE','EXPERIMENTAL_DISABLE_FILEWATCHER'):
        env['OPENCODE_'+flag] = 'true'
    env['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'] = '500'
    # Config.load always checks a plugin dependency even with plugins disabled.
    # These test-only lock records suppress reify; no plugin is installed/loaded.
    config = Path(env['XDG_CONFIG_HOME'])/'opencode'
    (config/'node_modules').mkdir(parents=True)
    (config/'package.json').write_text('{}', encoding='utf-8')
    (config/'package-lock.json').write_text(json.dumps({'packages':{'':{'dependencies':{'@opencode-ai/plugin':'1.18.23'}}}}), encoding='utf-8')
    settings = dict(model='xijian/fixture', small_model='xijian/fixture', enabled_providers=['xijian'],
        permission={'*':'deny'}, plugin=[], mcp={}, instructions=[], share='disabled', autoupdate=False,
        lsp=False, formatter=False, agent={name:{'disable':True} for name in ('title','summary','compaction','explore','general')},
        provider={'xijian':{'npm':'@ai-sdk/openai-compatible','name':'Local synthetic fixture',
            'options':{'baseURL':'http://127.0.0.1:%d/v1' % port, 'apiKey':'synthetic-test-only', 'timeout':5000},
            'models':{'fixture':{'name':'fixture','tool_call':False,'limit':{'context':32000,'output':500}}}}})
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(settings)
    return env


@pytest.mark.skipif(not os.environ.get('XIJIAN_TEST_OPENCODE_EXE'), reason='explicit native ACP loopback test')
def test_native_acp_loopback_candidate_execution_and_reuse(tmp_path, monkeypatch):
    exe = Path(os.environ['XIJIAN_TEST_OPENCODE_EXE']).resolve()
    candidate = 'def analyze(df):\r\n    return {"rows": [{"squared": int((df["COL_0001"] ** 2).sum())}]}\r\n'
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            requests.append((self.path, json.loads(body)))
            chunks = [dict(id='local-fixture',object='chat.completion.chunk',created=0,model='fixture',
                choices=[dict(index=0,delta={'role':'assistant','content':candidate},finish_reason=None)]),
                dict(id='local-fixture',object='chat.completion.chunk',created=0,model='fixture',
                choices=[dict(index=0,delta={},finish_reason='stop')],usage={'prompt_tokens':12,'completion_tokens':8,'total_tokens':20})]
            payload = ''.join('data: '+json.dumps(x)+'\n\n' for x in chunks).encode()+b'data: [DONE]\n\n'
            self.send_response(200); self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
    server = HTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    assert digest == workbench_opencode.EXECUTABLE_SHA256
    monkeypatch.setitem(workbench_acp.REVIEWED_RUNTIMES, ('opencode','1.18.23',digest),
        dict(args=['acp','--hostname','127.0.0.1','--port','0'], candidate_mode='text',
             environment=lambda workspace:isolated_environment(workspace/'isolated', server.server_port)))
    profile = dict(executable=str(exe), executable_sha256=digest, agent_kind='opencode', version='1.18.23',
        authorized=True, data_scope='public_bundle_only', allocation=500, max_invocations=1,
        model='xijian/fixture', timeout_seconds=45, approval_id='local-synthetic-only')
    adapter = ACPAdapter(tmp_path/'records', TokenBudget(tmp_path/'budget',1000), profile)
    try:
        probe = adapter.probe()
        assert probe['agentInfo']['name'] == 'OpenCode'
        from tools.workbench_agent import AgentWorkbench
        import pandas as pd
        class InternalDouble:
            source = 'local_test_double'
            def readiness(self): return {'ready':True,'live':False,'source':self.source,'missing':[]}
            def call(self, payload):
                return {'objective':'Compute squared total','roles':[{'column':'COL_0001','meaning':'amount'}],
                    'output':{'columns':[{'key':'squared','label':'Squared total','type':'integer',
                    'nullable':False,'restore_from':None}], 'max_rows':10}}
        service = AgentWorkbench(tmp_path/'workbench', internal=InternalDouble(), external=adapter,
            execution_authorization={'authorized':True,'data_scope':'synthetic_only',
                'risk_acknowledgement':'existing_supervisor_not_hostile_isolation'})
        proposal = service.draft(pd.DataFrame({'PRIVATE_AMOUNT_X':[3,11]}), 'PRIVATE_TASK_X')
        service.publish(proposal['id'], proposal['proposal_sha256'])
        generated = service.generate(proposal['id'], proposal['bundle_sha256'])
        assert generated['candidate_sha256'] == hashlib.sha256(candidate.encode()).hexdigest()
        result = service.run(proposal['id'], generated['candidate_sha256'])
        assert result['rows'] == [{'squared':130}]
        assert (tmp_path/'workbench'/'runs'/result['run_id']/'candidate.py').read_bytes() == candidate.encode()
        reused = service.reuse(proposal['id'], pd.DataFrame({'PRIVATE_AMOUNT_X':[2,4]}))
        assert reused['result']['rows'] == [{'squared':20}]
        assert len(requests) == 1, requests
        path, request = requests[0]
        assert path == '/v1/chat/completions'
        assert not request.get('tools')
        assert request.get('max_tokens') == 500
        assert 'COL_0001' in json.dumps(request)
        assert 'PRIVATE_AMOUNT_X' not in json.dumps(request) and 'PRIVATE_TASK_X' not in json.dumps(request)
        print(json.dumps(dict(executable_sha256=digest, initialize=probe, model_requests=len(requests),
            stop='end_turn', candidate_sha256=generated['candidate_sha256'], output=result['rows'], reuse=reused['result']['rows'])))
    finally:
        server.shutdown(); server.server_close(); thread.join(5)


@pytest.mark.skipif(not os.environ.get('XIJIAN_TEST_OPENCODE_EXE'), reason='explicit native ACP probe test')
def test_native_production_probe_has_dedicated_login_state(tmp_path):
    exe = Path(os.environ['XIJIAN_TEST_OPENCODE_EXE']).resolve()
    profile = dict(executable=str(exe),executable_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
        agent_kind='opencode',version='1.18.23',model='opencode/big-pickle',allocation=500,
        max_invocations=1,authorized=True,data_scope='public_bundle_only',timeout_seconds=45)
    adapter = ACPAdapter(tmp_path/'records',TokenBudget(tmp_path/'budget',1000),profile)
    result = adapter.probe()
    assert result['compatible'] and result['generation_supported']
    assert result['authentication'] == 'not_required'
    assert result['model_requests'] == 0
    assert result['login_args'] == ['auth','login']
    assert Path(result['login_environment']['XDG_DATA_HOME']) == tmp_path/'records'/'opencode-home'/'data'
    assert adapter.readiness()['ready']
    print(json.dumps({key:value for key,value in result.items() if key != 'login_environment'}))
