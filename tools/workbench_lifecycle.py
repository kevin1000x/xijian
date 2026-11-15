"""Durable per-task control; network work never holds the cancellation lock."""
import copy
import json
import os
from pathlib import Path
import threading
import time
import uuid

def atomic(path,value):
    temporary=path.with_name('.tmp-'+uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8'))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(str(temporary),str(path))
    finally:
        if temporary.exists(): temporary.unlink()

class StateLock:
    """Reentrant in-process lock plus the existing Windows writer primitive."""
    def __init__(self,root):
        self.root=root
        self.mutex=threading.RLock()
        self.local=threading.local()
        try:
            with (root/'writer.lock').open('xb') as stream:
                stream.write(b'0');stream.flush();os.fsync(stream.fileno())
        except FileExistsError: pass

    def __enter__(self):
        self.mutex.acquire()
        depth=getattr(self.local,'depth',0)
        try:
            if not depth:
                from tools.local_synthetic_workflow import _Writer
                self.local.writer=_Writer(self.root)
                self.local.writer.__enter__()
            self.local.depth=depth+1
            return self
        except Exception:
            self.mutex.release()
            raise

    def __exit__(self,*args):
        try:
            self.local.depth-=1
            if not self.local.depth: self.local.writer.__exit__(*args)
        finally: self.mutex.release()

class TaskLifecycle:
    def __init__(self,agent):
        self.agent=agent
        self.root=agent.root/'lifecycle'
        self.root.mkdir(exist_ok=True)
        self.lock=StateLock(self.root)
        self.local=threading.local()
        self.threads={}
        self.active={}
        self.transports={}
        self.budget=None
        with self.lock:
            for path in self.root.glob('*.json'):
                value=json.loads(path.read_text('utf-8'))
                if value.get('operation',{}).get('status')=='started':
                    value['state']='UNKNOWN_BLOCKED'
                    value['operation']['status']='unknown'
                    value['last_event']='process_restarted_after_operation_start'
                    atomic(path,value)

    def qoder_admission_status(self,connector):
        # Prospective, version-pinned conservative policy, not a wire inventory.
        # The adapter separately enforces this contract before reserving/spending.
        from tools.workbench_qoder import REVIEWED_RUNTIMES
        expected={'policy':'qoder-sdk-conservative-v2','runtime_reviewed':True,
            'allocation':3200000,'sdk_queries':1,'max_turns':4,'context_window':200000,
            'max_output_tokens':8192,'model_request_retries':0,'public_bytes':65536,
            'wire_inventory_verified':False,'actual_usage_known':False,
            'coverage':'sdk_limits_conservative_not_wire_verified'}
        try:
            contract=connector.admission_contract()
            runtime=contract.get('runtime_sha256')
            identity=REVIEWED_RUNTIMES.get(runtime)
            if identity is None:
                return 'pending_bound_review'
            expected.update(runtime_sha256=runtime, **identity)
            budget=connector.budget
            snapshot=budget.snapshot()
            if not (contract==expected and callable(self.budget) and budget is self.budget()
                    and snapshot['ceiling']<=10000000):
                return 'pending_bound_review'
            if snapshot['breached'] or snapshot['remaining']<expected['allocation']:
                return 'budget_exhausted'
            return 'reviewed_conservative_contract'
        except Exception:
            return 'pending_bound_review'

    def qoder_admission_verified(self,connector):
        return self.qoder_admission_status(connector)=='reviewed_conservative_contract'

    def _path(self,key):
        from tools.workbench_agent import _id
        return self.root/(_id(key)+'.json')

    def _load(self,key):
        path=self._path(key)
        if path.exists(): return json.loads(path.read_text('utf-8'))
        return {'task_id':key,'mode':'host_mcp' if self.agent.enterprise_actor else 'hosted_api',
                'state':'IDLE','cancelled':False,'owner':None,'claim_expires':None,
                'operation':{},'last_event':'task_available','remote_outcome':'unknown'}

    def view(self,key):
        with self.lock: value=self._load(key)
        if value.get('owner') and (value.get('claim_expires') or 0)<=time.time():
            value['owner_expired']=True
        else: value['owner_expired']=False
        # Project completed older tasks honestly without rewriting their evidence.
        path=self.agent._path(key)/'record.json'
        if (not value['cancelled'] and value['state']!='UNKNOWN_BLOCKED'
                and value.get('operation',{}).get('status')!='started' and path.is_file()
                and json.loads(path.read_text('utf-8')).get('status')=='INTERPRETED_UNVERIFIED'):
            value['state']='COMPLETE'
        return value

    def guard(self,key, *, allow_unknown=False):
        from tools.workbench_agent import require
        value=self.view(key)
        require(not value['cancelled'],'task_cancelled','任务已取消；保留迟到记录，不再推进。')
        require(allow_unknown or value['state']!='UNKNOWN_BLOCKED',
                'operation_unknown','前次操作结果未知，禁止重放。')

        record_path=self.agent._path(key)/'record.json'
        seen=set()
        while record_path.exists():
            record=json.loads(record_path.read_text('utf-8'))
            parent=record.get('parent_id')
            if not parent: break
            require(parent not in seen,'lineage_changed','Invalid task ancestry.')
            seen.add(parent)
            require(not self.view(parent)['cancelled'],'task_cancelled','Parent task cancelled; descendant work blocked.')
            record_path=self.agent._path(parent)/'record.json'

    def guard_remote_recovery(self, key):
        from tools.workbench_agent import require
        with self.lock:
            self.guard(key, allow_unknown=True)
            value = self._load(key)
            operation = value['operation']
            require(operation.get('status') != 'started' and key not in self.active
                    and not self.running(key), 'operation_active', '请等待当前操作结束再取回程序。')
            require(value['state'] != 'UNKNOWN_BLOCKED' or
                    (operation.get('status') == 'unknown' and operation.get('action') == 'generate'),
                    'operation_unknown', '只有未知的节点生成可以取回程序。')

    def finish_remote_recovery(self, key):
        value = self._load(key)
        value.update(state='IDLE', auto_advance=False, last_event='remote_candidate_recovered')
        atomic(self._path(key), value)

    def begin(self,key,action):
        from tools.workbench_agent import require
        with self.lock:
            self.guard(key)
            value=self._load(key)
            require(not value['operation'].get('status')=='started','operation_active','该任务已有活动操作。')
            op={'id':str(uuid.uuid4()),'action':action,'status':'started','started_at':time.time()}
            value.update(operation=op,state='RUNNING',last_event=action+'_started')
            atomic(self._path(key),value)
            self.active[key]=action
            return op['id']

    def finish(self,key,op,error=False):
        with self.lock:
            value=self._load(key)
            if value['operation'].get('id')!=op: return
            unknown=False
            try:
                record_status=self.agent._load(key)['status']
                if error: unknown='UNKNOWN' in record_status
            except Exception:
                record_status=None
                unknown=bool(error)
            value['operation'].update(status='unknown' if unknown else 'failed' if error else 'completed',
                                      finished_at=time.time())
            value.update(state='CANCELLED' if value['cancelled'] else 'UNKNOWN_BLOCKED' if unknown else 'COMPLETE' if record_status=='INTERPRETED_UNVERIFIED' else 'IDLE',
                         last_event='late_operation_retained' if value['cancelled'] else 'operation_failed' if error else 'operation_completed')
            atomic(self._path(key),value)
            self.active.pop(key,None)

    def execute(self,key,action,fn):
        # Nested existing operations are guarded but share one durable outer admission.
        self.guard(key)
        if getattr(self.local,'depth',0):
            return fn()
        op=self.begin(key,action)
        self.local.depth=1
        self.local.key=key
        try:
            result=fn()
        except Exception:
            self.finish(key,op,True)
            raise
        else:
            self.finish(key,op)
            if isinstance(result,dict) and result.get('proposal_id')==key:
                result['lifecycle']=self.view(key)
            return result
        finally:
            self.local.depth=0
            self.local.key=None

    def cancel(self,key):
        # No agent lock here: durable intent must win against a blocked transport.
        self.agent._load(key,public_only=True)
        with self.lock:
            value=self._load(key)
            value.update(cancelled=True,state='CANCELLED',last_event='cancel_requested',
                         cancel_requested_at=time.time(),remote_outcome='unknown')
            action=self.active.get(key)
            if action is None:
                record=self.agent._load(key,public_only=True)
                if record['status']=='DRAFT_UNKNOWN_BLOCKED': action='draft'
                elif record['status']=='INTERPRETATION_UNKNOWN_BLOCKED': action='interpret'
            atomic(self._path(key),value)
        kind=next((kind for kind,owner in self.transports.items() if owner==key),None)
        signaled=False
        if kind:
            try:
                response=self.agent.cancel(kind)
                signaled=bool(response)
            except Exception: pass
        with self.lock:
            value=self._load(key)
            value['transport_cancel_signaled']=signaled
            # Transport acknowledgement does not prove remote termination/billing.
            atomic(self._path(key),value)
        return value

    def claim(self,key,owner,ttl=300):
        from tools.workbench_agent import require
        with self.lock:
            self.guard(key)
            value=self._load(key)
            require(not value['owner'] or value['owner']==owner or
                    (value['claim_expires'] or 0)<=time.time(),'task_claimed','任务由另一连接持有。')
            value.update(owner=owner,claim_expires=time.time()+ttl,last_event='host_claimed',mode='host_mcp')
            atomic(self._path(key),value)
            return value

    def require_claim(self,key,owner):
        from tools.workbench_agent import require
        value=self.view(key)
        require(value['owner']==owner and not value['owner_expired'],'claim_required','请先领取当前任务；原领取可能已过期。')

    def running(self,key):
        with self.lock: return key in self.threads and self.threads[key].is_alive()

    def advance(self,key,proposal_sha256=None):
        self.guard(key)
        if proposal_sha256: self.agent.publish(key,proposal_sha256)
        with self.lock:
            if not self.running(key):
                value=self._load(key)
                value['auto_advance']=True
                atomic(self._path(key),value)
                thread=threading.Thread(target=self._drive,args=(key,),daemon=True)
                self.threads[key]=thread
                thread.start()
        return self.agent.get(key)

    def _drive(self,key):
        try:
            while True:
                self.guard(key)
                record=self.agent.get(key)
                status=record['status']
                if status=='PUBLISHED':
                    self.agent.generate(key,record['bundle_sha256'])
                elif status=='CANDIDATE_READY':
                    tested=record.get('public_test')
                    if tested is None:
                        self.agent.public_test(key,record['candidate_sha256'])
                    elif tested['status']=='PASSED':
                        self.agent.run(key,record['candidate_sha256'])
                    elif tested['status']=='FAILED':
                        if not record.get('repair_child_id') and record.get('repair_slots_used',0)<2:
                            self.agent.repair(key,record['candidate_sha256'])
                        self._waiting(key,'WAITING_PUBLIC_REPAIR_APPROVAL')
                        return
                    else:
                        self._waiting(key,'UNKNOWN_BLOCKED'); return
                elif status=='EXECUTED_UNVERIFIED':
                    self.agent.interpret(key,record['run_id'])
                else:
                    self._waiting(key,'WAITING_FOR_HOST' if status in {
                        'AWAITING_INTERNAL_PROPOSAL','AWAITING_INTERNAL_INTERPRETATION'}
                        else 'WAITING_APPROVAL' if status=='AWAITING_PUBLICATION_APPROVAL'
                        else 'COMPLETE' if status=='INTERPRETED_UNVERIFIED' else status)
                    return
        except Exception as error:
            with self.lock:
                value=self._load(key)
                if not value['cancelled']:
                    value.update(last_event=getattr(error,'code','operation_failed'),
                                 state='UNKNOWN_BLOCKED' if value['operation'].get('status')=='unknown' else 'BLOCKED')
                    atomic(self._path(key),value)

    def _waiting(self,key,state):
        with self.lock:
            value=self._load(key)
            if value['cancelled']: return
            value.update(state=state,last_event=state.lower())
            atomic(self._path(key),value)

    def stop(self):
        for key in set(self.active)|{key for key in self.threads if self.running(key)}:
            try: self.cancel(key)
            except Exception: pass

    def resume_safe(self):
        # Called only on explicit route configuration/startup, never by status reads.
        for path in self.root.glob('*.json'):
            value=json.loads(path.read_text('utf-8'))
            if value.get('auto_advance') and not value['cancelled'] and value['state']!='UNKNOWN_BLOCKED':
                record=self.agent._load(value['task_id'])
                from tools.workbench_agent import route_identity
                internal=self.agent.enterprise_actor or route_identity(self.agent.internal)
                if record['internal_route']==internal and record['external_route']==route_identity(self.agent.external):
                    self.advance(value['task_id'])
