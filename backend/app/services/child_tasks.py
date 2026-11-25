"""Attempt-bound task channel over existing admission, policy and durable queue."""
import asyncio
import json
import time
from pathlib import Path
from app.runtime.database import RuntimeErrorCode
from app.runtime.store import digest, identifier
from app.runtime.worker import read_profile
from app.runtime.child_schema import require
from app.runtime.child_authorizations import row
from .task_console import TaskConsole

SUBMIT='dios_task_submit'
GET='dios_task_get'


def tool_schemas(aliases):
    return [dict(name=SUBMIT,description='Submit a governed child task. Preserve the returned run ID and observe it before finishing the parent.',
        inputSchema=dict(type='object',properties=dict(profile={'type':'string','enum':aliases},goal={'type':'string','minLength':1,'maxLength':2000},
            sample_id={'type':'string','minLength':1,'maxLength':160},input_payload={'type':'object'}),
            required=['profile','goal'],additionalProperties=False,oneOf=[{'required':['sample_id']},{'required':['input_payload']}])),
        dict(name=GET,description='Wait up to 20 seconds (default) for a child to finish, then return its state and verified result. If still active, call again with the same run ID. A wait timeout is not task failure and does not resubmit it.',
            inputSchema=dict(type='object',properties={'run_id':{'type':'string','minLength':1,'maxLength':160},
                'wait_seconds':{'type':'integer','minimum':0,'maximum':30,'default':20}},required=['run_id'],additionalProperties=False))]


def activity_arguments(alias,params):
    if alias==SUBMIT:
        return {key:params[key] for key in ('profile','goal','sample_id','input_payload') if params.get(key) is not None}
    return {'run_id':params.get('run_id'),'wait_seconds':params.get('wait_seconds',20)}


def configuration(value):
    if not isinstance(value,dict) or set(value)!={'profiles','max_children','max_depth'}:
        raise RuntimeErrorCode('CHILD_CHANNEL_PROFILE_INVALID')
    if type(value['max_children']) is not int or not 1<=value['max_children']<=32:
        raise RuntimeErrorCode('CHILD_LIMIT_INVALID')
    if type(value['max_depth']) is not int or not 1<=value['max_depth']<=3:
        raise RuntimeErrorCode('CHILD_DEPTH_INVALID')
    profiles=value['profiles']
    if not isinstance(profiles,dict) or not 1<=len(profiles)<=8:raise RuntimeErrorCode('CHILD_CHANNEL_PROFILE_INVALID')
    for alias,entry in profiles.items():
        identifier(alias)
        if not isinstance(entry,dict) or set(entry)!={'path','profile_id','profile_digest'}:raise RuntimeErrorCode('CHILD_CHANNEL_PROFILE_INVALID')
        if not isinstance(entry['path'],str) or not Path(entry['path']).is_absolute():raise RuntimeErrorCode('CHILD_CHANNEL_PROFILE_INVALID')
        identifier(entry['profile_id'])
        if not isinstance(entry['profile_digest'],str) or len(entry['profile_digest'])!=64 or any(c not in '0123456789abcdef' for c in entry['profile_digest']):
            raise RuntimeErrorCode('CHILD_CHANNEL_PROFILE_INVALID')
    return value


class ChildTasks:
    def __init__(self,channel):
        self.channel=channel;self.store=channel.store;self.gate=channel.gate
        self.config=configuration(channel.policy.get('children'))

    async def console(self,alias,parent):
        if not isinstance(alias,str) or alias not in self.config['profiles']:
            raise RuntimeErrorCode('CHILD_PROFILE_NOT_GRANTED')
        entry=self.config['profiles'][alias]
        path=Path(entry['path'])
        profile_id,spec=await asyncio.to_thread(read_profile,path)
        if profile_id!=entry['profile_id'] or spec.fingerprint()!=entry['profile_digest']:
            raise RuntimeErrorCode('PROFILE_CHANGED')
        console=TaskConsole(self.store.db,self.store.scope,path.parent,self.gate.provider.root,policy=self.gate.policy)
        if parent['input'].get('principal') is not None:console=console.for_principal(parent['input']['principal'])
        return console,entry

    async def submit(self,params,parent):
        identifier(params['key'])
        if not isinstance(params['goal'],str) or not 1<=len(params['goal'])<=2000:raise RuntimeErrorCode('INVALID_WORK_INPUT')
        console,entry=await self.console(params['profile'],parent)
        expected=dict(owner=list(self.channel.binding),session_id=self.channel.session_id,
            max_children=self.config['max_children'],max_depth=self.config['max_depth'])
        async def guard(c):
            if self.gate.identity_guard:await self.gate.identity_guard(c)
            _,current=await asyncio.to_thread(read_profile,entry['path'])
            if current.fingerprint()!=entry['profile_digest']:raise RuntimeErrorCode('PROFILE_CHANGED')
        key='child-'+digest([parent['id'],params['key']])
        result=await console.submit(entry['profile_id'],params['sample_id'],params['goal'],key,
            input_payload=params['input_payload'],parent_binding=expected,admission_guard=guard,
            delegation_ref=parent['input'].get('delegation_ref') if console.principal is not None else None)
        await self.audit(SUBMIT,params,result['id'])
        return self.summary(result)

    async def audit(self,alias,params,run_id):
        from app.runtime.work_authorizations import audit
        from app.services.e2ag import Decision
        async with self.store.db.transaction(self.store.scope) as c:
            await self.own(c,run_id)
            await audit(c,self.store.scope,self.channel.binding[0],self.channel.binding[1],'child-channel',
                Decision('child-task','allow',('CHILD_TASK_SUBMITTED' if alias==SUBMIT else 'CHILD_TASK_OBSERVED',),
                    evidence=dict(alias=alias,arguments_digest=digest(activity_arguments(alias,params)),
                        child_run_id=run_id,session_id=self.channel.session_id)),await self.store.db.now(c))

    @staticmethod
    def summary(result):
        return {k:result[k] for k in ('id','state','version','cancel_requested','attempt_id','family')}

    async def own(self,c,run_id):
        await require(c)
        relation=await row(c,self.store.scope,run_id)
        if (relation is None or relation['parent_run_id']!=self.channel.binding[0]
                or relation['parent_attempt_id']!=self.channel.binding[1]
                or relation['parent_epoch']!=self.channel.binding[4]
                or relation['parent_session_id']!=self.channel.session_id):
            raise RuntimeErrorCode('NOT_FOUND')
        if self.gate.identity_guard:await self.gate.identity_guard(c)
        await self.store._owned(c,*self.channel.binding,authorize=True)
        return await self.store._run(c,run_id)

    async def get(self,run_id,parent,wait_seconds=0):
        identifier(run_id)
        if type(wait_seconds) is not int or not 0<=wait_seconds<=30:raise RuntimeErrorCode('CHILD_WAIT_INVALID')
        deadline=time.monotonic()+wait_seconds
        while True:
            async with self.store.db.read(self.store.scope) as c:run=await self.own(c,run_id)
            if run['state'] in {'succeeded','failed','cancelled'} or time.monotonic()>=deadline:break
            await asyncio.sleep(min(.5,max(0,deadline-time.monotonic())))
        alias=next((key for key,value in self.config['profiles'].items() if value['profile_id']==run['input']['runtime_profile']),None)
        console,_=await self.console(alias,parent)
        result=await console.detail(run_id)
        value=self.summary(result)
        active=next((a for a in result['attempts'] if a['id']==result['attempt_id']),None)
        value['phase']=active['state'] if active else result['state']
        value['wait_exhausted']=bool(wait_seconds and result['state'] not in {'succeeded','failed','cancelled'})
        value['artifacts']=result['artifacts']
        if result['state']=='succeeded':
            artifact=next((a for a in result['artifacts'] if a['kind']=='result'),None)
            if artifact is not None and artifact['size']<=65536:
                blob=await console.artifact(run_id,artifact['id'])
                value['result']=await asyncio.to_thread(lambda:json.loads(b''.join(self.gate.provider.read(blob['storage_key']))))
        # Access and owner may change while object I/O or identity lookups wait.
        await self.channel._owner(wait=True)
        async with self.store.db.read(self.store.scope) as c:await self.own(c,run_id)
        await self.audit(GET,{'run_id':run_id,'wait_seconds':wait_seconds},run_id)
        return value
