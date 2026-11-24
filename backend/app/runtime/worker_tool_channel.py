"""Per-attempt Unix socket: no operator credential or caller-selected execution identity."""
from .work_policy import policy_matches
from .principals import admit
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import tempfile
from sqlalchemy import select
from app.services.e2ag import Decision, evaluate_work_permission
from .adapter import Mount
from .database import RuntimeErrorCode
from .store import encoded, digest, bounded, identifier
from .work_auth_schema import work
from .work_authorizations import audit
from .tool_schema import invocations, require

POLICY_ENV='DIOS_TOOL_CHANNEL_POLICY'
SOCKET_ENV='DIOS_TOOL_SOCKET'
MOUNT_PATH='/run/dios-tools'
FRAME_LIMIT=65536


def profile_policy(value):
    if not isinstance(value,dict) or set(value)-{'tools','acp_names','client_read_tool','worker_secret_refs'}:raise RuntimeErrorCode('TOOL_CHANNEL_PROFILE_INVALID')
    tools=value.get('tools')
    if not isinstance(tools,dict) or not 1<=len(tools)<=16:raise RuntimeErrorCode('TOOL_CHANNEL_PROFILE_INVALID')
    for name,tool_id in tools.items():
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',name):raise RuntimeErrorCode('TOOL_CHANNEL_PROFILE_INVALID')
        identifier(tool_id)
    names=value.get('acp_names',{})
    if not isinstance(names,dict) or len(names)>32:raise RuntimeErrorCode('TOOL_CHANNEL_PROFILE_INVALID')
    for name,alias in names.items():
        identifier(name)
        if alias not in tools:raise RuntimeErrorCode('TOOL_CHANNEL_PROFILE_INVALID')
    if value.get('client_read_tool') is not None and value['client_read_tool'] not in tools:
        raise RuntimeErrorCode('TOOL_CHANNEL_PROFILE_INVALID')
    refs=value.get('worker_secret_refs',[])
    if not isinstance(refs,list) or len(refs)>32 or any(not isinstance(x,str) for x in refs) or len(set(refs))!=len(refs):
        raise RuntimeErrorCode('TOOL_CHANNEL_PROFILE_INVALID')
    for ref in refs:identifier(ref)
    bounded(value)
    return value


class WorkerToolChannel:
    def __init__(self,gate,binding,policy,uid):
        self.gate,self.store,self.binding=gate,gate.store,binding
        self.policy=profile_policy(policy);self.uid=uid
        self.session_id=None;self.server=None;self.directory=None;self.connections=set();self.closing=False
        self.registered={};self.busy=False;self.request_lock=asyncio.Lock()

    async def _owner(self, *, wait=False):
        from .identity_wait import identity_wait
        # Pure identity/owner check only. The action below is entered once.
        return await identity_wait(self._owner_once) if wait else await self._owner_once()

    async def _owner_once(self):
        async with self.store.db.read(self.store.scope) as c:
            run,a,now=await self.store._owned(c,*self.binding,authorize=True)
            if not run['input'].get('work_authorization_ref'):raise RuntimeErrorCode('WORK_AUTHORIZATION_REQUIRED')
            if self.session_id is not None and a['session_id']!=self.session_id:raise RuntimeErrorCode('SESSION_CHANGED')
        current=await asyncio.to_thread(admit,self.gate.policy,run['input']['runtime_profile'],
            run['input']['profile_digest'],run['input'].get('secret_refs',[]),run['input'].get('principal'))
        if not policy_matches(current,run['input']['work_policy_digest']):raise RuntimeErrorCode('WORK_POLICY_CHANGED')
        return run,a

    async def initialize(self):
        run,_=await self._owner()
        current=await asyncio.to_thread(admit,self.gate.policy,run['input']['runtime_profile'],
            run['input']['profile_digest'],run['input'].get('secret_refs',[]),run['input'].get('principal'))
        if not policy_matches(current,run['input']['work_policy_digest']):raise RuntimeErrorCode('WORK_POLICY_CHANGED')
        for alias,tool_id in self.policy['tools'].items():
            reg=await asyncio.to_thread(self.gate.catalog.registration,tool_id)
            decision=evaluate_work_permission(current['permissions'],reg['permission'])
            if not decision.permits_execution:raise RuntimeErrorCode(decision.reason_codes[0])
            if alias==self.policy.get('client_read_tool') and reg['permission']['action']!='filesystem.read':
                raise RuntimeErrorCode('ACP_READ_TOOL_PERMISSION_INVALID')
            self.registered[alias]=reg

    async def open(self):
        await self.initialize()
        return await self.listen()

    async def listen(self):
        if os.name!='posix' or not hasattr(socket,'SO_PEERCRED'):raise RuntimeErrorCode('TOOL_CHANNEL_PLATFORM_UNSUPPORTED')
        # A short private host parent also keeps AF_UNIX paths below Linux's length limit.
        parent=Path('/tmp')/('dios-tools-'+str(os.getuid()))
        parent.mkdir(mode=0o700,exist_ok=True)
        info=parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
            raise RuntimeErrorCode('TOOL_CHANNEL_DIRECTORY_INVALID')
        self.directory=Path(tempfile.mkdtemp(prefix='attempt-',dir=parent))
        os.chmod(self.directory,0o755)
        try:
            self.server=await asyncio.start_unix_server(self._connection,path=str(self.directory/'channel.sock'),limit=FRAME_LIMIT+1)
            os.chmod(self.directory/'channel.sock',0o666)
        except BaseException:
            await self.close();raise
        return Mount(str(self.directory),MOUNT_PATH,True)

    async def bind_session(self,session_id):
        if self.session_id is not None and self.session_id!=session_id:raise RuntimeErrorCode('SESSION_CHANGED')
        _,a=await self._owner()
        if a['session_id']!=session_id:raise RuntimeErrorCode('TOOL_SESSION_NOT_READY')
        self.session_id=session_id

    async def _deny(self,code):
        async with self.store.db.transaction(self.store.scope) as c:
            now=await self.store.db.now(c)
            await audit(c,self.store.scope,self.binding[0],self.binding[1],'tool-control',
                Decision('worker-tool-control','deny',(code,)),now)

    async def _connection(self,reader,writer):
        if len(self.connections)>=8:
            writer.close();return
        task=asyncio.current_task();self.connections.add(task)
        try:
            peer=writer.get_extra_info('socket')
            _,uid,_=struct.unpack('3i',peer.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid!=self.uid:raise RuntimeErrorCode('TOOL_CHANNEL_PEER_DENIED')
            async with asyncio.timeout(100):
                data=await reader.readline()
                if not data.endswith(b'\n') or len(data)>FRAME_LIMIT:raise RuntimeErrorCode('TOOL_CHANNEL_FRAME_INVALID')
                message=json.loads(data.decode('utf-8'))
                value=await self.handle(message)
                result={'ok':True,'value':value}
                if len(encoded(result))>2*1024*1024:raise RuntimeErrorCode('TOOL_RESULT_TOO_LARGE')
                writer.write(encoded(result)+b'\n');await writer.drain()
        except asyncio.CancelledError:raise
        except Exception as exc:
            code=exc.code if isinstance(exc,RuntimeErrorCode) else 'TOOL_CHANNEL_UNAVAILABLE'
            try:
                await self._deny(code)
            except Exception:pass
            try:
                writer.write(encoded({'ok':False,'error':code})+b'\n');await writer.drain()
            except Exception:pass
        finally:
            self.connections.discard(task);writer.close()
            try:await writer.wait_closed()
            except (OSError,ConnectionError):pass

    async def handle(self,message):
        if self.closing:raise RuntimeErrorCode('TOOL_CHANNEL_CLOSED')
        if not isinstance(message,dict):raise RuntimeErrorCode('TOOL_CHANNEL_FRAME_INVALID')
        method=message.get('method');params=message.get('params',{})
        allowed={'describe':set(),'ready':{'session_id'},'request':{'tool','arguments','key'},
            'advance':{'invocation_id'},'verify_activity':{'activity'},'verify_required':{'requirements'},'permit_proxy':{'name','arguments'},'deny_native':{'method'}}
        if set(message)!={'method','params'} or method not in allowed or not isinstance(params,dict) or set(params)!=allowed[method]:
            raise RuntimeErrorCode('TOOL_CHANNEL_METHOD_DENIED')
        run,a=await self._owner(wait=True)
        if method=='describe':
            return {'tools':[{'name':alias,'description':'E2AG governed tool: '+alias,'inputSchema':reg['input_schema']}
                for alias,reg in self.registered.items()], 'client_read_tool':self.policy.get('client_read_tool')}
        if self.session_id is None:raise RuntimeErrorCode('TOOL_SESSION_NOT_READY')
        if method=='ready':
            if params['session_id']!=self.session_id:raise RuntimeErrorCode('SESSION_CHANGED')
            return {'session_id':self.session_id}
        if method=='permit_proxy':
            from jsonschema import Draft202012Validator
            alias=self.policy.get('acp_names',{}).get(params['name']) if isinstance(params['name'],str) else None
            if alias not in self.registered:raise RuntimeErrorCode('TOOL_NOT_GRANTED')
            reg=self.registered[alias]
            if not isinstance(params['arguments'],dict) or not Draft202012Validator(reg['input_schema']).is_valid(params['arguments']):
                raise RuntimeErrorCode('INVALID_TOOL_ARGUMENTS')
            # Permission to enter our MCP proxy only. Actual action still passes
            # request/approval/dispatch and cannot be authorized by the Agent.
            async with self.store.db.transaction(self.store.scope) as c:
                now=await self.store.db.now(c)
                await audit(c,self.store.scope,self.binding[0],self.binding[1],'proxy-forward',
                    Decision('worker-tool-control','allow',('ACP_PROXY_FORWARD_ALLOWED',),
                        evidence={'tool_id':reg['id'],'arguments_digest':digest(params['arguments'])}),now)
            return {'forward_to_gate':True}
        if method=='deny_native':
            if params['method'] not in {'session/request_permission','fs/write_text_file','terminal','unsupported'}:
                raise RuntimeErrorCode('TOOL_CHANNEL_METHOD_DENIED')
            await self._deny('ACP_NATIVE_OPERATION_DENIED')
            return {'denied':True}
        if method=='verify_required':return await self.verify_required(params['requirements'])
        if method=='verify_activity':return await self.verify_activity(params['activity'])
        if method=='request':
            alias=params['tool'];key=params['key']
            if not isinstance(alias,str) or alias not in self.registered:raise RuntimeErrorCode('TOOL_NOT_GRANTED')
            identifier(key)
            reg=self.registered[alias]
            fresh=await asyncio.to_thread(self.gate.catalog.registration,reg['id'])
            if fresh['digest']!=reg['digest']:raise RuntimeErrorCode('TOOL_REGISTRATION_CHANGED')
            async with self.request_lock:
                from sqlalchemy import func
                request_key='channel:'+digest(key)
                async with self.store.db.read(self.store.scope) as c:
                    await require(c)
                    base=(invocations.c.scope==self.store.scope,invocations.c.attempt_id==a['id'])
                    count=(await c.execute(select(func.count()).select_from(invocations).where(*base))).scalar_one()
                    existing=(await c.execute(select(invocations.c.id).where(*base,invocations.c.request_key==request_key))).first()
                if count>=128 and not existing:raise RuntimeErrorCode('TOOL_CALL_LIMIT_EXCEEDED')
                value=await self.gate.request(run['id'],a['id'],self.session_id,reg['id'],params['arguments'],request_key)
            return self.status(value)
        identifier(params['invocation_id'])
        item=await self.gate.get(run['id'],params['invocation_id'])
        await self._own_call(item)
        if item['expired'] and item['state'] in {'prepared','awaiting_approval'}:raise RuntimeErrorCode('TOOL_AUTHORIZATION_EXPIRED')
        if item['state']=='prepared':
            # One actual tool at a time per independent Worker. Heartbeats remain in its main loop.
            if self.busy:return self.status(item)
            self.busy=True
            try:item=await self.gate.dispatch(run['id'],item['id'],item['version'])
            finally:self.busy=False
        result=self.status(item)
        if item['state'] in {'succeeded','failed'} and item['result_key']:
            blob=await asyncio.to_thread(self.gate.provider.verify,item['result_key'])
            if blob.size>1048576:raise RuntimeErrorCode('TOOL_RESULT_TOO_LARGE')
            result['result']=await asyncio.to_thread(lambda:json.loads(b''.join(self.gate.provider.read(item['result_key']))))
        return result

    async def _own_call(self,item):
        if item['attempt_id']!=self.binding[1] or item['session_id']!=self.session_id:
            raise RuntimeErrorCode('TOOL_BINDING_CHANGED')
        async with self.store.db.read(self.store.scope) as c:
            row=await self.gate._row(c,self.binding[0],item['id'])
            if not row['request_key'].startswith('channel:'):raise RuntimeErrorCode('TOOL_BINDING_CHANGED')
            if row['tool_id'] not in self.policy['tools'].values():raise RuntimeErrorCode('TOOL_NOT_GRANTED')

    @staticmethod
    def status(item):
        return {k:item[k] for k in ('id','state','version','approval_state','expired','reason')}

    async def verify_required(self,requirements):
        if not isinstance(requirements,list) or not 1<=len(requirements)<=16:
            raise RuntimeErrorCode('TOOL_REQUIREMENTS_INVALID')
        async with self.store.db.read(self.store.scope) as c:
            await require(c)
            rows=(await c.execute(select(invocations).where(invocations.c.scope==self.store.scope,
                invocations.c.run_id==self.binding[0],invocations.c.attempt_id==self.binding[1],
                invocations.c.request_key.like('channel:%'),invocations.c.state=='succeeded').limit(129))).mappings().all()
        for item in requirements:
            if (not isinstance(item,dict) or set(item)!={'tool','arguments'} or
                    not isinstance(item['tool'],str) or item['tool'] not in self.registered or
                    not isinstance(item['arguments'],dict)):
                raise RuntimeErrorCode('TOOL_REQUIREMENTS_INVALID')
            tool=self.registered[item['tool']]['id']
            if not any(row['tool_id']==tool and row['arguments_digest']==digest(item['arguments']) and
                    row['binding']['session_id']==self.session_id for row in rows):
                raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
        return {'verified':True,'required_tool_count':len(requirements)}

    async def verify_activity(self,activity):
        if not isinstance(activity,list) or len(activity)>128:raise RuntimeErrorCode('ACP_TOOL_ACTIVITY_UNVERIFIED')
        async with self.store.db.read(self.store.scope) as c:
            await require(c)
            rows=(await c.execute(select(invocations).where(invocations.c.scope==self.store.scope,
                invocations.c.run_id==self.binding[0],invocations.c.attempt_id==self.binding[1],
                invocations.c.request_key.like('channel:%')).limit(129))).mappings().all()
        if len(rows)>128:raise RuntimeErrorCode('TOOL_CALL_LIMIT_EXCEEDED')
        actual=Counter((row['tool_id'],row['arguments_digest']) for row in rows
            if row['binding']['session_id']==self.session_id and row['state'] in {'succeeded','failed'})
        for event in activity:
            if not isinstance(event,dict) or set(event)!={'name','arguments'}:raise RuntimeErrorCode('ACP_TOOL_ACTIVITY_UNVERIFIED')
            alias=self.policy.get('acp_names',{}).get(event['name'])
            if alias not in self.registered:raise RuntimeErrorCode('ACP_TOOL_ACTIVITY_UNVERIFIED')
            key=(self.registered[alias]['id'],digest(event['arguments']))
            if actual[key]<1:raise RuntimeErrorCode('ACP_TOOL_ACTIVITY_UNVERIFIED')
            actual[key]-=1
        async with self.store.db.transaction(self.store.scope) as c:
            now=await self.store.db.now(c)
            await audit(c,self.store.scope,self.binding[0],self.binding[1],'tool-activity',
                Decision('worker-tool-control','allow',('ACP_TOOL_ACTIVITY_LINKED',),
                    evidence={'observed_tool_calls':len(activity),'invocation_ids':[row['id'] for row in rows]}),now)
        return {'verified':True,'observed_tool_calls':len(activity)}

    async def close(self):
        self.closing=True
        if self.server:self.server.close();await self.server.wait_closed()
        tasks=list(self.connections)
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if self.directory:
            (self.directory/'channel.sock').unlink(missing_ok=True)
            self.directory.rmdir()
            self.directory=None
