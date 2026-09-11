"""One persistent token ceiling shared by internal and external invocations.

Reservations remain charged at their maximum when observations are unknown.
Local integrity/locking is not authentication against the workspace owner.
"""
import os
from pathlib import Path
import re
import uuid
from tools import local_synthetic_workflow as storage

class BudgetError(ValueError):
    code='token_budget_rejected'
    message='Token 预算不足、身份冲突或记录无效；未启动新的模型调用。'

class TokenBudget:
    def __init__(self, root, ceiling=10_000_000):
        if type(ceiling) is not int or not 1<=ceiling<=10_000_000: raise BudgetError()
        self.root=Path(root).resolve()
        self.ceiling=ceiling
        self.root.mkdir(parents=True,exist_ok=True)
        try:
            with (self.root/'writer.lock').open('xb') as stream:
                stream.write(b'0'); stream.flush(); os.fsync(stream.fileno())
        except FileExistsError:
            pass
        with storage._Writer(self.root):
            if not (self.root/'budget.json').exists():
                storage._publish(self.root/'budget.json',storage._bytes(
                    {'version':1,'ceiling':ceiling,'operations':{}}))
            self._load()

    def _load(self):
        value=storage._json(self.root/'budget.json')
        if (set(value)!={'version','ceiling','operations'} or value['version']!=1
            or value['ceiling']!=self.ceiling or type(value['operations']) is not dict):
            raise BudgetError()
        for op in value['operations'].values():
            if (type(op) is not dict or set(op)!={'reserved','status','charged'}
                or type(op['reserved']) is not int or op['reserved']<1
                or op['status'] not in {'reserved','observed','unknown'}
                or op['status']=='reserved' and op['charged'] is not None
                or op['status']!='reserved' and (type(op['charged']) is not int or op['charged']<0)):
                raise BudgetError()
        return value

    def _save(self,value):
        temporary=self.root/('.tmp-'+uuid.uuid4().hex)
        try:
            with temporary.open('xb') as stream:
                stream.write(storage._bytes(value)); stream.flush(); os.fsync(stream.fileno())
            os.replace(str(temporary),str(self.root/'budget.json'))
        finally:
            if temporary.exists(): temporary.unlink()

    def _snapshot(self,value):
        reserved=sum(o['reserved'] for o in value['operations'].values() if o['status']=='reserved')
        charged=sum(o['charged'] for o in value['operations'].values() if o['status']!='reserved')
        return {'ceiling':self.ceiling,'charged':charged,'reserved':reserved,
                'remaining':max(0,self.ceiling-charged-reserved),
                'breached':charged+reserved>self.ceiling,
                'ledger_allocatable':max(0,self.ceiling-charged-reserved),
                'conservative_charged':sum(o['charged'] for o in value['operations'].values() if o['status']=='unknown'),
                'observed_tokens':sum(o['charged'] for o in value['operations'].values() if o['status']=='observed'),
                'unknown_operations':sum(o['status']=='unknown' for o in value['operations'].values()),
                'observed_operations':sum(o['status']=='observed' for o in value['operations'].values()),
                'coverage':'partial_or_unknown' if any(o['status']!='observed' for o in value['operations'].values()) else 'ledger_observations_only',
                'credits':None,'basis':'persistent_ledger_not_provider_remaining_quota'}

    def snapshot(self):
        # Writers atomically replace a complete validated JSON file. A snapshot
        # reads either committed version and never acquires a spending lock.
        return self._snapshot(self._load())

    def reserve(self, operation_id, tokens):
        if (type(operation_id) is not str or not re.fullmatch('[A-Za-z0-9._:-]{1,128}',operation_id)
            or type(tokens) is not int or tokens<1): raise BudgetError()
        with storage._Writer(self.root):
            value=self._load()
            if operation_id in value['operations'] or tokens>self._snapshot(value)['remaining']:
                raise BudgetError()
            value['operations'][operation_id]={'reserved':tokens,'status':'reserved','charged':None}
            self._save(value)
            return {'operation_id':operation_id,'reserved_tokens':tokens,**self._snapshot(value)}

    def settle(self, operation_id, observed_tokens=None):
        if observed_tokens is not None and (type(observed_tokens) is not int or observed_tokens<0):
            raise BudgetError()
        with storage._Writer(self.root):
            value=self._load()
            if operation_id not in value['operations']: raise BudgetError()
            op=value['operations'][operation_id]
            status='unknown' if observed_tokens is None else 'observed'
            charged=op['reserved'] if observed_tokens is None else observed_tokens
            if op['status']!='reserved' and (op['status']!=status or op['charged']!=charged):
                raise BudgetError()
            op.update(status=status,charged=charged)
            self._save(value)
            return self._snapshot(value)
