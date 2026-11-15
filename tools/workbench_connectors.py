"""Explicitly configured adapters. Constructors never inspect credentials or launch.

Qoder command capability follows https://docs.qoder.com/cli/cli-reference and
https://docs.qoder.com/cli/run-in-scripts (checked 2026-09-11). CLI flags alone
cannot prove request inventory, retry suppression, cost or filesystem isolation.
The Qoder launcher and boundary verifier therefore have NO default implementation.
"""
import copy
import json
import math
from pathlib import Path
import threading
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

from tools.workbench_agent import AgentError, digest
from tools import local_synthetic_workflow as storage


class ConnectorError(AgentError):
    pass


def require(ok, code, message):
    if not ok:
        raise ConnectorError(code, message)


# This fixed stdlib-only child performs no filesystem writes and cannot load project
# Python code (-I). URL, credential and private request travel through stdin only.
# Killing and joining the child ends DNS/connect/read work; no network-reading
# application thread survives the total deadline.
_HTTP_WORKER = 'import json\nimport sys\nfrom urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler\n\nclass NoRedirect(HTTPRedirectHandler):\n    def redirect_request(self, *args, **kwargs):\n        raise ValueError("redirect_rejected")\n\ntry:\n    raw_input = sys.stdin.buffer.read(4 * 1024 * 1024 + 65537)\n    if len(raw_input) > 4 * 1024 * 1024 + 65536:\n        raise ValueError("request_size")\n    config = json.loads(raw_input)\n    request = Request(config["url"], data=config["body"].encode("utf-8"),\n                      headers=config["headers"], method="POST")\n    opener = build_opener(ProxyHandler({}), NoRedirect())\n    with opener.open(request, timeout=config["timeout_seconds"]) as response:\n        raw = response.read(config["max_response_bytes"] + 1)\n    if len(raw) > config["max_response_bytes"]:\n        raise ValueError("response_size")\n    sys.stdout.buffer.write(raw)\n    sys.stdout.buffer.flush()\nexcept Exception:\n    # No traceback, response body, URL or credential on stderr.\n    sys.exit(2)\n'


PROTOCOLS = {'openai_responses','openai_chat','anthropic_messages'}


def encode_request(profile, token, payload):
    protocol=profile.get('protocol','openai_chat')
    require(protocol in PROTOCOLS,'unsupported_protocol','请选择支持的内侧 API 协议。')
    content=json.dumps(payload,ensure_ascii=False,allow_nan=False)
    headers={'Content-Type':'application/json'}
    body={'model':profile['model'],'stream':False}
    if protocol=='openai_responses':
        field='max_output_tokens'
        body.update(input=[{'role':'user','content':[{'type':'input_text','text':content}]}],
                    store=False,tools=[])
        headers['Authorization']='Bearer '+token
    elif protocol=='anthropic_messages':
        field='max_tokens'
        body['messages']=[{'role':'user','content':content}]
        headers.update({'x-api-key':token,'anthropic-version':'2023-06-01'})
    else:
        field=profile.get('chat_cap_field','max_completion_tokens' if 'protocol' in profile else 'max_tokens')
        require(field in {'max_tokens','max_completion_tokens'},'invalid_cap_field','Chat token 上限字段无效。')
        body.update(messages=[{'role':'user','content':content}],n=1)
        headers['Authorization']='Bearer '+token
    body[field]=profile['max_output_tokens']
    return storage._bytes(body),headers,field


def _usage(protocol,response):
    usage=response.get('usage')
    if type(usage) is not dict: return None
    if protocol=='openai_chat':
        values=[usage.get('prompt_tokens'),usage.get('completion_tokens')]
    else:
        values=[usage.get('input_tokens'),usage.get('output_tokens')]
    if any(type(v) is not int or v<0 for v in values): return None
    if protocol=='anthropic_messages':
        cache=[usage.get('cache_creation_input_tokens',0),usage.get('cache_read_input_tokens',0)]
        if any(type(v) is not int or v<0 for v in cache): return None
        values[0]+=sum(cache)
    total=sum(values)
    if protocol!='anthropic_messages' and usage.get('total_tokens',total)!=total: return None
    return {'input_tokens':values[0],'output_tokens':values[1],'total_tokens':total}


def parse_response(profile,response, *, text_only=False):
    protocol=profile.get('protocol','openai_chat')
    aliases=profile.get('model_aliases',[])
    require(type(aliases) is list and all(type(x) is str for x in aliases) and
            type(response) is dict and response.get('model') in [profile['model']]+aliases,
            'model_identity_changed','返回模型不在明确批准的名称或别名中。')
    if protocol=='openai_chat':
        choices=response.get('choices')
        require(type(choices) is list and len(choices)==1 and choices[0].get('finish_reason')=='stop',
                'incomplete_response','Chat 响应未完整结束。')
        message=choices[0].get('message',{})
        require(type(message) is dict and message.get('role','assistant')=='assistant' and
                not message.get('tool_calls') and not message.get('function_call') and
                not message.get('refusal') and type(message.get('content')) is str,
                'unexpected_content','Chat 返回了工具、拒绝或非文本内容。')
        text=message['content']
    elif protocol=='anthropic_messages':
        require(response.get('type')=='message' and response.get('role')=='assistant' and
                response.get('stop_reason')=='end_turn','incomplete_response','Messages 响应未完整结束。')
        blocks=response.get('content')
        require(type(blocks) is list and blocks and all(type(b) is dict and b.get('type')=='text'
                and type(b.get('text')) is str for b in blocks),
                'unexpected_content','Messages 返回了工具、思考或非文本交付。')
        text=''.join(b['text'] for b in blocks)
    elif protocol=='openai_responses':
        require(response.get('status')=='completed' and not response.get('error') and
                not response.get('incomplete_details'),'incomplete_response','Responses 响应未完整结束。')
        output=response.get('output')
        require(type(output) is list and output,'unexpected_content','Responses 没有交付。')
        texts=[]
        for item in output:
            require(type(item) is dict,'unexpected_content','Responses 交付无效。')
            if item.get('type')=='reasoning': continue
            require(item.get('type')=='message' and item.get('role')=='assistant' and
                    item.get('status')=='completed','unexpected_content','Responses 包含工具或未完成交付。')
            content=item.get('content')
            require(type(content) is list and content and all(type(b) is dict and
                    b.get('type')=='output_text' and type(b.get('text')) is str for b in content),
                    'unexpected_content','Responses 返回拒绝或非文本内容。')
            texts.extend(b['text'] for b in content)
        require(texts,'unexpected_content','Responses 缺少结果文本。')
        text=''.join(texts)
    else:
        raise ConnectorError('unsupported_protocol','不支持的 API 协议。')
    result=text if text_only else json.loads(text,parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite')))
    return result,{'source':'internal_http','protocol':protocol,'configured_model':profile['model'],
                   'observed_model':response['model'],'usage':_usage(protocol,response)}


class _Configured:
    source = 'unconfigured'
    def __init__(self, profile=None, verifier=None):
        self.profile = copy.deepcopy(profile or {})
        self.verifier = verifier
        self.used = 0
        self.lock = threading.Lock()

    def readiness(self):
        p = self.profile
        missing = []
        for key in ('route_id', 'account_id', 'model', 'approval_id'):
            if type(p.get(key)) is not str or not p[key].strip():
                missing.append(key)
        if p.get('authorized') is not True:
            missing.append('explicit_live_authorization')
        if p.get('data_scope') != ('public_bundle_only' if self.source in {'qoder', 'external_http'} else 'synthetic_internal_only'):
            missing.append('data_scope')
        for key in ('max_invocations', 'max_output_tokens', 'timeout_seconds', 'max_response_bytes'):
            if type(p.get(key)) is not int or not 1 <= p[key] <= 1024 * 1024:
                missing.append(key)
        token_ceiling=p.get('max_total_tokens')
        token_budget=getattr(self,'budget',None)
        if type(token_ceiling) is int and 1<=token_ceiling<=10_000_000 and token_budget is not None:
            if token_budget.snapshot()['remaining']<=0:
                missing.append('token_budget_exhausted')
        else:
            cost = p.get('max_cost_usd')
            if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                missing.append('numeric_token_boundary')
        if self.verifier is None:
            missing.append('verified_launch_boundary')
        elif not missing:
            try:
                proof = self.verifier(copy.deepcopy(p))
                needed = {'profile_sha256', 'launch_boundary', 'budget_enforced', 'data_boundary'}
                if (type(proof) is not dict or set(proof) != needed or
                    proof['profile_sha256'] != digest(p) or
                    not all(proof[k] is True for k in needed - {'profile_sha256'})):
                    missing.append('verified_launch_boundary')
            except Exception:
                missing.append('verified_launch_boundary')
        if type(p.get('max_invocations')) is int and self.used >= p['max_invocations']:
            missing.append('request_budget_exhausted')
        return {'ready': not missing, 'live': not missing, 'source': self.source, 'missing': missing,
                'used_invocations': self.used}

    def _claim(self):
        with self.lock:
            require(self.readiness()['ready'], 'connector_not_ready',
                    '模型连接缺少明确授权或经验证的调用边界；未发起请求。')
            # Claimed before any I/O. Exception consumes the request, never retries.
            self.used += 1


class InternalHTTPAdapter(_Configured):
    """One bounded OpenAI-compatible chat completion request, with redirects/proxies off."""
    source = 'internal_http'
    def __init__(self, profile=None, token=None, verifier=None, budget=None):
        super().__init__(profile, verifier)
        self.token = token
        self.budget = budget
        self._active_lock=threading.Lock()
        self._active=None
        self._cancelled=False

    def operation(self):
        with self._active_lock:
            return {'id':self._active['id'],'kind':self.source} if self._active else None

    def cancel(self, operation_id=None):
        with self._active_lock:
            if self._active is None or operation_id is not None and operation_id!=self._active['id']:
                return {'cancelled':False,'reason':'no_matching_active_operation'}
            self._cancelled=True
            child=self._active.get('child')
            if child is not None and child.poll() is None:
                child.kill()
            return {'cancelled':True,'operation_id':self._active['id'],
                    'status':'termination_requested'}

    def readiness(self):
        state = super().readiness()
        parsed = urlsplit(self.profile.get('url', ''))
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or parsed.query or
                parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost'}):
            state['missing'].append('exact_secure_internal_url')
        if type(self.token) is not str or not self.token:
            state['missing'].append('explicit_injected_credential')
        if self.profile.get('protocol','openai_chat') not in PROTOCOLS:
            state['missing'].append('supported_protocol')
        if self._active is not None:
            state['missing'].append('operation_active')
        state['ready'] = state['live'] = not state['missing']
        return state

    def _parse(self, response):
        return parse_response(self.profile, response)

    def call(self, payload):
        self._claim()
        p = self.profile
        deadline = time.monotonic() + p['timeout_seconds']
        body,headers,cap_field=encode_request(p,self.token,payload)
        require(len(body) <= 2 * 1024 * 1024, 'request_size', '内侧请求超过大小上限。')
        wire = storage._bytes({'url': p['url'], 'headers':headers,
            'body': body.decode('utf-8'), 'timeout_seconds': p['timeout_seconds'],
            'max_response_bytes': p['max_response_bytes']})
        require(len(wire) <= 4 * 1024 * 1024 + 65536,
                'request_size', '内侧请求超过大小上限。')
        operation_id=('external-' if self.source=='external_http' else 'internal-')+uuid.uuid4().hex
        reservation=len(body)+p['max_output_tokens']+1024
        if self.budget is not None:
            require(reservation<=p['max_total_tokens'],'token_budget_rejected','本次请求超过授权 Token 上限。')
            self.budget.reserve(operation_id,reservation)
        settled=False
        self.last_receipt={'source':self.source,'operation_id':operation_id,
            'protocol':p.get('protocol','openai_chat'),'configured_model':p['model'],
            'observed_model':None,'wire_token_cap_field':cap_field,
            'reserved_tokens':reservation,'usage':None,'status':'UNKNOWN'}
        with self._active_lock:
            self._active={'id':operation_id,'child':None}
            self._cancelled=False
        try:
            with subprocess.Popen([sys.executable, '-I', '-B', '-c', _HTTP_WORKER],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as child:
                try:
                    with self._active_lock:
                        self._active['child']=child
                        if self._cancelled:
                            child.kill()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired('internal_http', p['timeout_seconds'])
                    raw, _ = child.communicate(input=wire, timeout=remaining)
                    if time.monotonic() > deadline:
                        raise subprocess.TimeoutExpired('internal_http', p['timeout_seconds'])
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()  # reap and close pipes before reporting unknown
                    raise ConnectorError('internal_deadline_exceeded',
                        '内侧请求超过总时限；本地传输进程已结束，本次请求已计数，不会重试。') from None
                require(child.returncode == 0 and len(raw) <= p['max_response_bytes'],
                        'invalid_response', '内侧传输未返回完整且大小有效的响应。')
            require(not self._cancelled,'internal_cancelled','内侧传输已结束；远端状态未知，不会重试。')
            result,receipt=self._parse(json.loads(raw))
            self.last_receipt.update(receipt,status='COMPLETE')
            if self.budget is not None:
                usage=receipt['usage']
                self.last_receipt['budget']=self.budget.settle(operation_id,
                    usage['total_tokens'] if usage else None)
                settled=True
            return result
        except ConnectorError as error:
            if error.code in {'internal_deadline_exceeded','internal_cancelled'}:
                raise
            raise ConnectorError('internal_request_unresolved',
                                 '内侧请求未得到完整有效响应；本次请求已计数，不会重试。') from None
        except Exception:
            raise ConnectorError('internal_request_unresolved',
                                 '内侧请求未得到完整有效响应；本次请求已计数，不会重试。') from None
        finally:
            if self.budget is not None and not settled:
                self.last_receipt['budget']=self.budget.settle(operation_id,None)
            with self._active_lock:
                self._active=None


class ExternalHTTPAdapter(InternalHTTPAdapter):
    """One public-only code response over the existing native HTTP transports.

    No tools, filesystem, sessions, fallback or code cleanup. The orchestrator
    supplies the exact approved bundle; provider text becomes the UTF-8 candidate.
    """
    source = 'external_http'

    def call(self, public_bundle):
        required = {'version', 'objective', 'roles', 'schema', 'examples', 'output', 'program_contract'}
        require(type(public_bundle) is dict and set(public_bundle) in
                (required, required | {'repair'}),
                'public_bundle_required', '外侧 API 只接受完整的已批准公开包。')
        require(len(storage._bytes(public_bundle)) <= 65536,
                'public_bundle_too_large', '公开包超过 64 KB；未发送。')
        try:
            return super().call(public_bundle)
        except ConnectorError as error:
            raise ConnectorError(error.code.replace('internal_', 'external_'),
                                 error.message.replace('内侧', '外侧')) from None

    def _parse(self, response):
        text, receipt = parse_response(self.profile, response, text_only=True)
        raw = text.encode('utf-8')
        require(0 < len(raw) <= 65536 and text.strip() and
                not text.lstrip().startswith('```'),
                'invalid_candidate', '外侧 API 未返回原始 Python 文本；不自动清理或截断。')
        receipt.update(source=self.source, delivery='original_utf8_response')
        return raw, receipt


class QoderAdapter(_Configured):
    """Build verified headless invocation; a trusted bounded launcher must be injected."""
    source = 'qoder'
    def __init__(self, profile=None, launcher=None, verifier=None):
        super().__init__(profile, verifier)
        self.launcher = launcher

    def readiness(self):
        state = super().readiness()
        if self.launcher is None:
            state['missing'].append('bounded_isolated_launcher')
        if type(self.profile.get('executable')) is not str or not Path(self.profile['executable']).is_absolute():
            state['missing'].append('verified_qoder_executable')
        if type(self.profile.get('max_turns')) is not int or not 1 <= self.profile['max_turns'] <= 100:
            state['missing'].append('bounded_turns')
        # A cwd string or an isolation_verified boolean is deliberately insufficient.
        state['ready'] = state['live'] = not state['missing']
        return state

    def call(self, public_bundle):
        require(type(public_bundle) is dict and set(public_bundle) == {
                'version', 'objective', 'roles', 'schema', 'examples', 'output', 'program_contract'},
                'public_bundle_required', '外侧连接仅接受已批准的公开包结构。')
        self._claim()
        p = self.profile
        argv = [p['executable'], '-p', '--output-format', 'text', '--model', p['model'],
                '--max-turns', str(p['max_turns']), '--max-output-tokens', str(p['max_output_tokens']),
                '--no-session-persistence', '--tools=Read,Write,Edit,Glob,Grep',
                '--allowed-tools=Read,Write,Edit,Glob,Grep', '--strict-mcp-config',
                '--max-model-request-retries', '0', '--permission-mode', 'dont_ask']
        # Launcher owns exact approved isolated environment/cwd, process tree limits,
        # approved route/subscription budget enforcement and no blind retries/fallback.
        # It stages only public-bundle.json, lets the Coding Agent write analysis.py,
        # then reads its original bytes with path/reparse-point and size checks.
        # Actual backend request count may be unknown; one CLI invocation is not one request.
        # It receives no private path, binding, prompt, values or interpretation.
        try:
            result = self.launcher(argv=argv, stdin=storage._bytes(public_bundle),
                timeout_seconds=p['timeout_seconds'], max_output_bytes=p['max_response_bytes'],
                profile_sha256=digest(p))
            require(type(result) is dict and set(result) == {'candidate', 'exit_code', 'observed_requests',
                    'observed_model', 'complete'} and result['exit_code'] == 0
                    and result['complete'] is True
                    and (result['observed_requests'] is None or type(result['observed_requests']) is int
                         and result['observed_requests'] >= 0)
                    and (result['observed_model'] is None or type(result['observed_model']) is str)
                    and type(result['candidate']) is bytes and 0 < len(result['candidate']) <= p['max_response_bytes'],
                    'incomplete_response', 'Qoder 文件交付或终态不完整。')
            self.last_receipt = {'source': self.source, 'delivery': 'public_workspace_analysis.py',
                'configured_model': p['model'], 'configured_route': p['route_id'],
                'observed_model': result['observed_model'], 'observed_requests': result['observed_requests']}
            return result['candidate']
        except Exception:
            raise ConnectorError('qoder_request_unresolved',
                                 'Qoder 提交状态未知；已计数并阻止自动重试。') from None
