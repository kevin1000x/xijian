"""Actual MCP framing and lifecycle; the test bridge is not a model connector."""
import io
import json
import pytest
from tools import workbench_mcp as mcp

def messages(*items):
    return ''.join(json.dumps(item)+'\n' for item in items)

def initialize():
    return {'jsonrpc':'2.0','id':1,'method':'initialize','params':{
        'protocolVersion':'2025-06-18','capabilities':{},
        'clientInfo':{'name':'synthetic-enterprise-test','version':'1'}}}

def test_stdio_initialize_tools_call_and_eof():
    class Bridge:
        def __init__(self): self.calls=[]
        def call(self,name,args):
            self.calls.append((name,args))
            return {'content':[{'type':'text','text':'synthetic-context'}],'isError':False}
    bridge=Bridge(); out=io.StringIO()
    mcp.serve(io.StringIO(messages(
        {'jsonrpc':'2.0','id':0,'method':'tools/list'},
        initialize(),{'jsonrpc':'2.0','method':'notifications/initialized'},
        {'jsonrpc':'2.0','id':2,'method':'tools/list'},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'get_task_context','arguments':{}}},
        {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'publish','arguments':{}}}
    )),out,bridge)
    result=[json.loads(line) for line in out.getvalue().splitlines()]
    assert result[0]['error']['code']==-32002
    assert result[1]['result']['protocolVersion']=='2025-06-18'
    assert result[2]['result']['tools']
    assert all(t['name'] not in {'publish','approve'} for t in result[2]['result']['tools'])
    assert result[3]['result']['isError'] is False
    assert result[4]['error']['code']==-32602
    assert bridge.calls==[('get_task_context',{})]

def test_malformed_eof_reports_error_and_empty_eof_returns():
    out=io.StringIO()
    mcp.serve(io.StringIO('{"jsonrpc":'),out,object())
    assert json.loads(out.getvalue())['error']['code']==-32700
    out=io.StringIO(); mcp.serve(io.StringIO(''),out,object())
    assert out.getvalue()==''

def test_bridge_failure_is_sanitized_protocol_result():
    class Broken:
        def call(self,*args): raise OSError('sensitive local secret')
    out=io.StringIO()
    mcp.serve(io.StringIO(messages(initialize(),{'jsonrpc':'2.0','method':'notifications/initialized'},
        {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_task_context','arguments':{}}}
    )),out,Broken())
    result=json.loads(out.getvalue().splitlines()[-1])
    assert result['result']['isError'] and 'sensitive' not in out.getvalue()


def test_real_stdio_scoped_bridge_submits_proposal_and_requires_gui_publication(tmp_path):
    import subprocess
    import threading
    from tools.workbench_server import Store,make_server
    class ExternalSyntheticTransport:
        source='local_test_double'
        calls=0
        def readiness(self): return {'ready':True,'live':False,'missing':[],'source':self.source}
        def call(self,bundle):
            self.calls+=1
            return b'def analyze(df):\n    return {"rows":[{"squared":int((df["COL_0001"]**2).sum())}]}\n'
    external=ExternalSyntheticTransport()
    store=Store(tmp_path,agent_external=external)
    store.configure_agent({'mode':'enterprise_mcp','actor_id':'synthetic-enterprise-client',
        'authorized':True,'host_budget_enforced':True,'host_token_reservation':2000})
    server=make_server(store,0)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        data=store.import_table({'format':'csv','text':'PRIVATE_AMOUNT_X\n7\n9'})
        record=store.agent_action('draft',{'dataset_id':data['dataset_id'],'task':'PRIVATE_TASK_X'})
        session=store.create_mcp_session({'proposal_id':record['id'],'dataset_id':data['dataset_id']})
        # Independent fixture response stands in for enterprise reasoning, not a model call.
        context=store.agent.enterprise_context(record['id'],store.agent.enterprise_actor)
        proposal={'objective':'Compute squared total','roles':[{'column':'COL_0001','meaning':'amount'}],
            'output':{'columns':[{'key':'squared','label':'Squared total','type':'integer',
                                 'nullable':False,'restore_from':None}],'max_rows':10}}
        config=session['client_config']['mcpServers']['local-workbench-internal']
        request=messages(initialize(),{'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_task_context','arguments':{}}},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'submit_proposal','arguments':{
                'context_sha256':context['context_sha256'],'proposal':proposal}}},
            {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'generate_program','arguments':{
                'bundle_sha256':'0'*64}}})
        completed=subprocess.run([config['command']]+config['args'],input=request.encode(),
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
        assert completed.returncode==0
        result=[json.loads(line) for line in completed.stdout.decode().splitlines()]
        assert result[1]['result']['structuredContent']['context']['records']==[
            {'PRIVATE_AMOUNT_X':7},{'PRIVATE_AMOUNT_X':9}]
        task=result[2]['result']['structuredContent']
        assert task['status']=='AWAITING_PUBLICATION_APPROVAL'
        assert result[3]['result']['isError'] is True and external.calls==0
        credential=mcp.protect_token(__import__('pathlib').Path(session['credential_file']).read_bytes(),
                                     decrypt=True).decode()
        bridge=mcp.LocalBridge(store.server_url,session['session_id'],credential)
        store.agent.publish(task['id'],task['proposal_sha256'])  # explicit GUI authority seam
        generated=bridge.call('generate_program',{'bundle_sha256':task['bundle_sha256']})['structuredContent']
        executed=bridge.call('execute_program',{'candidate_sha256':generated['candidate_sha256']})['structuredContent']
        assert executed['rows']==[{'squared':130}] and external.calls==1
        result_context=bridge.call('get_result_context',{})['structuredContent']
        denied=bridge.call('submit_interpretation',{'run_id':executed['run_id'],
                    'result_sha256':'0'*64,'text':'wrong association'})
        assert denied['isError'] is True
        interpreted=bridge.call('submit_interpretation',{'run_id':executed['run_id'],
                    'result_sha256':result_context['result_sha256'],'text':'Synthetic fixture: squared total is 130.'})
        assert interpreted['structuredContent']['status']=='INTERPRETED_UNVERIFIED'
        assert store.token_budget.snapshot()['charged']==4000
    finally:
        server.shutdown(); server.server_close(); thread.join(3)
