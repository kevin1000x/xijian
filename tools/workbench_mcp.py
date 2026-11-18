"""Scoped stdio MCP bridge for an existing INTERNAL enterprise Agent.

JSON-RPC newline framing follows MCP 2025-06-18. This process never calls a
model and cannot approve publication. Only the local workbench controls Qoder.
"""
import argparse
import ctypes
from ctypes import wintypes
import hmac
import http.client
import json
import os
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit

MAX_LINE=4*1024*1024
VERSION='2025-06-18'

def protect_token(raw, decrypt=False):
    if os.name!='nt': raise ValueError('windows_credential_protection_required')
    class Blob(ctypes.Structure):
        _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_ubyte))]
    buffer=ctypes.create_string_buffer(raw)
    source=Blob(len(raw),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    output=Blob()
    crypt=ctypes.WinDLL('crypt32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    kernel.LocalFree.restype=ctypes.c_void_p
    if decrypt:
        fn=crypt.CryptUnprotectData
        fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.POINTER(Blob),ctypes.c_void_p,
                     ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        ok=fn(ctypes.byref(source),None,None,None,None,1,ctypes.byref(output))
    else:
        fn=crypt.CryptProtectData
        fn.argtypes=[ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.POINTER(Blob),ctypes.c_void_p,
                     ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        ok=fn(ctypes.byref(source),'LocalWorkbench MCP session',None,None,None,1,ctypes.byref(output))
    if not ok: raise ValueError('credential_protection_failed')
    try: return ctypes.string_at(output.pbData,output.cbData)
    finally: kernel.LocalFree(ctypes.cast(output.pbData,ctypes.c_void_p))

def _schema(properties=None,required=None):
    return {'type':'object','properties':properties or {},'required':required or [],
            'additionalProperties':False}

TOOLS=[
 {'name':'get_task_context','description':'Read the authorized internal task/schema/rows for this session.',
  'inputSchema':_schema()},
 {'name':'submit_proposal','description':'Submit an abstract public proposal. Publication still requires GUI approval.',
  'inputSchema':_schema({'context_sha256':{'type':'string'},'proposal':{'type':'object'}},
                        ['context_sha256','proposal'])},
 {'name':'get_task_status','description':'Read current scoped task progress, candidate and verified local result.',
  'inputSchema':_schema()},
 {'name':'generate_program','description':'Request external Qoder ONLY after exact GUI publication approval.',
  'inputSchema':_schema({'bundle_sha256':{'type':'string'}},['bundle_sha256'])},
 {'name':'execute_program','description':'Execute the scoped original candidate on the internal synthetic snapshot.',
  'inputSchema':_schema({'candidate_sha256':{'type':'string'}},['candidate_sha256'])},
 {'name':'test_public_program','description':'Test original candidate on exact published examples only; not mathematical validation.',
  'inputSchema':_schema({'candidate_sha256':{'type':'string'}},['candidate_sha256'])},
 {'name':'prepare_repair','description':'Prepare a bounded public-failure repair child. Only GUI can approve its new public envelope.',
  'inputSchema':_schema({'candidate_sha256':{'type':'string'}},['candidate_sha256'])},
 {'name':'reuse_program','description':'Reuse exact candidate on the dataset explicitly scoped by this session.',
  'inputSchema':_schema()},
 {'name':'get_result_context','description':'Read this task actual result for internal interpretation.',
  'inputSchema':_schema()},
 {'name':'submit_interpretation','description':'Submit interpretation bound to the exact actual result digest.',
  'inputSchema':_schema({'run_id':{'type':'string'},'result_sha256':{'type':'string'},
                         'text':{'type':'string'}},['run_id','result_sha256','text'])}
]

for tool in TOOLS:
    tool['inputSchema']['properties']['proposal_id']={'type':'string'}
    if tool['name']=='reuse_program':
        tool['inputSchema']['properties']['dataset_id']={'type':'string'}
TOOLS.extend([
 {'name':'heartbeat','description':'Observe this approved host connection; configuration alone is not online.',
  'inputSchema':_schema()},
 {'name':'list_authorized_inputs','description':'List only inputs explicitly admitted by the local user.',
  'inputSchema':_schema()},
 {'name':'list_tasks','description':'List only tasks using inputs admitted to this approved actor.',
  'inputSchema':_schema()},
 {'name':'create_task','description':'Create an internal task from an explicitly admitted dataset. No publication approval.',
  'inputSchema':_schema({'dataset_id':{'type':'string'},'task':{'type':'string'}},['dataset_id','task'])},
 {'name':'claim_task','description':'Claim or renew ownership of an authorized task for five minutes.',
  'inputSchema':_schema({'proposal_id':{'type':'string'}},['proposal_id'])},
 {'name':'cancel_task','description':'Persist task cancellation and prevent later stages. Remote billing outcome can remain unknown.',
  'inputSchema':_schema({'proposal_id':{'type':'string'}})}
])

class LocalBridge:
    def __init__(self,url,session,credential,state_file=None,state_id=None):
        self.state_file=Path(state_file) if state_file else None
        self.state_id=state_id
        if self.state_file:
            metadata=json.loads(self.state_file.read_text('utf-8'))
            if metadata.get('state_id')!=state_id or metadata.get('app_id')!='local-analysis-workbench-v1':
                raise ValueError('state_identity_changed')
            url=metadata['url']
        parsed=urlsplit(url)
        if (parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','localhost'}
            or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('loopback_server_required')
        self.host,self.port,self.session,self.credential=parsed.hostname,parsed.port,session,credential

    def call(self,name,arguments):
        if self.state_file:
            try:
                metadata=json.loads(self.state_file.read_text('utf-8'))
                parsed=urlsplit(metadata['url'])
                if (metadata.get('state_id')!=self.state_id or metadata.get('app_id')!='local-analysis-workbench-v1'
                    or parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                    raise ValueError('state_identity_changed')
                probe=http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=5)
                try:
                    probe.request('GET','/api/bootstrap')
                    response=probe.getresponse(); identity=json.loads(response.read(MAX_LINE+1))
                    if (response.status!=200 or identity.get('app_id')!='local-analysis-workbench-v1'
                        or identity.get('state_id')!=self.state_id):
                        raise ValueError('app_identity_changed')
                finally: probe.close()
                self.host,self.port=parsed.hostname,parsed.port
            except Exception:
                return {'content':[{'type':'text','text':'Pinned local workbench is unavailable or changed.'}],'isError':True}
        connection=http.client.HTTPConnection(self.host,self.port,timeout=300)
        try:
            raw=json.dumps(arguments,ensure_ascii=False,allow_nan=False).encode()
            connection.request('POST','/api/mcp/'+name,body=raw,headers={
                'Content-Type':'application/json','X-Workbench-MCP-Session':self.session,
                'X-Workbench-MCP-Token':self.credential})
            response=connection.getresponse()
            body=response.read(MAX_LINE+1)
            if len(body)>MAX_LINE: raise ValueError('response_limit')
            value=json.loads(body)
            if response.status!=200:
                error=value.get('error',{})
                return {'content':[{'type':'text','text':error.get('message','Local workbench action rejected.')}],
                        'isError':True}
            return {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}],
                    'structuredContent':value if type(value) is dict else {'items':value},
                    'isError':False}
        except (OSError,ValueError,http.client.HTTPException):
            return {'content':[{'type':'text','text':'Local bridge unavailable; operation may be unresolved. Do not repeat blindly.'}],
                    'isError':True}
        finally: connection.close()

def serve(reader,writer,bridge):
    initialized=False
    announced=False
    output_lock=threading.Lock()
    active={}
    jobs=[]
    def send(value):
        with output_lock:
            writer.write(json.dumps(value,ensure_ascii=False,allow_nan=False)+'\n'); writer.flush()
    def dispatch(identifier,name,arguments):
        try:
            result=bridge.call(name,arguments)
            send({'jsonrpc':'2.0','id':identifier,'result':result})
        finally:
            active.pop(identifier,None)
    def join_jobs():
        for job in jobs: job.join(310)

    def error(identifier,code,message):
        send({'jsonrpc':'2.0','id':identifier,'error':{'code':code,'message':message}})
    while True:
        line=reader.readline(MAX_LINE+1)
        if not line:
            join_jobs(); return
        if len(line)>MAX_LINE:
            error(None,-32700,'Message size exceeded.'); return
        try: message=json.loads(line)
        except (ValueError,TypeError):
            error(None,-32700,'Invalid JSON.'); continue
        if type(message) is not dict or message.get('jsonrpc')!='2.0' or type(message.get('method')) is not str:
            error(message.get('id') if type(message) is dict else None,-32600,'Invalid request.'); continue
        method=message['method']; identifier=message.get('id'); params=message.get('params',{})
        if 'id' not in message:
            if method=='notifications/initialized' and announced: initialized=True
            if method=='notifications/cancelled' and type(params) is dict:
                target=active.get(params.get('requestId'))
                if params.get('requestId') in active:
                    bridge.call('cancel_task',{'proposal_id':target} if target else {})
            continue
        if type(identifier) not in (int,str) or type(params) is not dict:
            error(identifier,-32600,'Invalid request.'); continue
        if method=='initialize':
            if announced:
                error(identifier,-32600,'Already initialized.'); continue
            if type(params.get('protocolVersion')) is not str or type(params.get('capabilities')) is not dict or type(params.get('clientInfo')) is not dict:
                error(identifier,-32602,'Invalid initialization.'); continue
            announced=True
            result={'protocolVersion':VERSION,'capabilities':{'tools':{'listChanged':False}},
                    'serverInfo':{'name':'local-workbench-internal-bridge','version':'1.0.0'},
                    'instructions':'Internal actor only. GUI must approve publication. Unknown submissions must not be repeated.'}
        elif not initialized:
            error(identifier,-32002,'Initialization required.'); continue
        elif method=='ping': result={}
        elif method=='tools/list': result={'tools':TOOLS}
        elif method=='tools/call':
            name=params.get('name'); arguments=params.get('arguments',{})
            tool=next((t for t in TOOLS if t['name']==name),None)
            if tool is None or type(arguments) is not dict:
                error(identifier,-32602,'Unknown tool or invalid arguments.'); continue
            schema=tool['inputSchema']
            if set(arguments)-set(schema['properties']) or set(schema['required'])-set(arguments):
                error(identifier,-32602,'Invalid tool fields.'); continue
            if name in {'generate_program','execute_program','test_public_program','prepare_repair','reuse_program'}:
                if active:
                    error(identifier,-32000,'An operation is active; inspect or cancel it before another mutation.'); continue
                active[identifier]=arguments.get('proposal_id')
                job=threading.Thread(target=dispatch,args=(identifier,name,arguments),daemon=True)
                jobs.append(job);job.start();continue
            try:
                result=bridge.call(name,arguments)
            except Exception:
                result={'content':[{'type':'text','text':'Local bridge failed; preserve unresolved operation state.'}],'isError':True}
        else:
            error(identifier,-32601,'Method not found.'); continue
        send({'jsonrpc':'2.0','id':identifier,'result':result})

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--server'); parser.add_argument('--session',required=True)
    parser.add_argument('--state-file',type=Path);parser.add_argument('--state-id')
    parser.add_argument('--credential-file',type=Path,required=True)
    args=parser.parse_args()
    try:
        path=args.credential_file
        for item in (path,)+tuple(path.parents):
            info=item.lstat()
            if item.is_symlink() or getattr(info,'st_file_attributes',0)&1024:
                raise ValueError('unsafe_credential_path')
        raw=path.read_bytes()
        if len(raw)>16384: raise ValueError('credential_size')
        credential=protect_token(raw,decrypt=True).decode('ascii')
        if not 16<=len(credential)<=256: raise ValueError('invalid_credential')
        bridge=LocalBridge(args.server,args.session,credential,args.state_file,args.state_id)
        if hasattr(sys.stdin,'reconfigure'): sys.stdin.reconfigure(encoding='utf-8')
        if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
        serve(sys.stdin,sys.stdout,bridge)
    except Exception:
        sys.stderr.write('Internal MCP bridge could not start; verify the scoped client configuration.\n')
        return 1
    return 0

if __name__=='__main__': sys.exit(main())
