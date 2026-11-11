"""Shared deterministic state machine; persistence adapters own atomic application."""
import hashlib,json
from typing import Protocol

def raw(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def hashed(value):return hashlib.sha256(raw(value).encode()).hexdigest()
def initial():
    return dict(version=0,epoch=1,authorization_epoch=1,revoked=False,head='0'*64,
                action='snapshot.read',resource='snapshot:1',approval_consumed=False)

class CommitStore(Protocol):
    async def create(self, run_id: str): ...
    async def apply(self, run_id: str, request: dict) -> dict: ...
    async def state(self, run_id: str) -> dict: ...
    async def entries(self, run_id: str) -> list[dict]: ...
    async def close(self): ...

def transition(state,request,existing):
    fingerprint=hashed(request)
    if existing:
        return None,None,{'status':'replay' if existing['request_digest']==fingerprint else 'conflict','version':existing['version']}
    kind=request['kind']
    if kind=='intent':
        if state['revoked']:return None,None,{'status':'deny','reason':'REVOKED'}
        if request['epoch']!=state['epoch']:return None,None,{'status':'deny','reason':'STALE_OWNER'}
        if request['authorization_epoch']!=state['authorization_epoch']:return None,None,{'status':'deny','reason':'STALE_AUTHORIZATION'}
        if (request['action'],request['resource'])!=(state['action'],state['resource']):
            return None,None,{'status':'deny','reason':'SCOPE_DENIED'}
        if request.get('approval') and state['approval_consumed']:
            return None,None,{'status':'deny','reason':'APPROVAL_CONSUMED'}
    updated=dict(state)
    if kind=='intent' and request.get('approval'):updated['approval_consumed']=True
    if kind=='revoke':updated.update(revoked=True,authorization_epoch=state['authorization_epoch']+1)
    if kind=='epoch':updated['epoch']+=1
    if kind not in {'intent','revoke','epoch'}:raise ValueError('unsupported transition')
    updated['version']+=1
    entry={'run_id':request['run_id'],'id':request['id'],'kind':kind,'version':updated['version'],
           'previous_hash':state['head'],'request_digest':fingerprint,'request':request,
           'outcome':'unknown' if kind=='intent' else 'committed'}
    entry['hash']=hashed(entry);updated['head']=entry['hash']
    return updated,entry,{'status':'committed','version':updated['version']}

def action(run_id,invocation_id,**overrides):
    value=dict(run_id=run_id,id=invocation_id,kind='intent',epoch=1,authorization_epoch=1,
               action='snapshot.read',resource='snapshot:1',parameters={'path':'src/example.py'},approval=False)
    value.update(overrides);return value

def verify(state,entries):
    previous='0'*64
    epoch,auth_epoch,revoked,consumed=1,1,False,False
    for sequence,e in enumerate(sorted(entries,key=lambda x:x['version']),1):
        assert e['version']==sequence and e['previous_hash']==previous
        h=e['hash'];assert hashed({k:v for k,v in e.items() if k!='hash'})==h
        assert e['request_digest']==hashed(e['request'])
        assert e['id']==e['request']['id'] and e['run_id']==e['request']['run_id']
        if e['kind']=='intent':
            q=e['request']
            assert not revoked and q['epoch']==epoch and q['authorization_epoch']==auth_epoch
            assert q['action']=='snapshot.read' and q['resource']=='snapshot:1'
            if q.get('approval'):
                assert not consumed
                consumed=True
            assert e['outcome']=='unknown'
        elif e['kind']=='revoke':revoked=True;auth_epoch+=1
        elif e['kind']=='epoch':epoch+=1
        else:raise AssertionError('unknown evidence type')
        previous=h
    assert len(entries)==state['version'] and previous==state['head']
