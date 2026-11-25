"""Authenticated control plane. One bounded request/transaction; no caller-selected code."""
import asyncio
import base64
import time
from contextlib import asynccontextmanager
from sqlalchemy import select,func
from .remote_schema import peers,receipts,uploads,chunks,require
from .schema import runs,attempts,workers
from .store import RuntimeStore,digest,encoded,identifier,ACTIVE_ATTEMPTS,TERMINAL
from .database import RuntimeErrorCode
from .worker_identity import PROTOCOL,document
from .work_authorizations import WorkAuthorizationVerifier,audit
from .authorization import authorize_work
from .adapter import Observation
from .bridge import JsonlBridge
from .lifecycle import admission
from app.services.e2ag import Decision


class TransactionView:
    """Compose trusted repository operations in the ingress transaction without nested locks."""
    def __init__(self,db,c,scope):self.db,self.c,self.scope=db,c,scope
    @asynccontextmanager
    async def transaction(self,scope,**kwargs):
        if scope!=self.scope:raise RuntimeErrorCode('WORKER_IDENTITY_SCOPE_DENIED')
        yield self.c
    read=transaction
    async def now(self,c):return await self.db.now(c)


class RemoteControl:
    def __init__(self,store,identities,profiles,policy,provider):
        self.store,self.identities,self.profiles,self.policy,self.provider=store,identities,profiles,policy,provider

    async def handle(self,certificate,expiry,e):
        fields={'protocol','audience','scope','profile','profile_digest','worker_id','instance','request_id','message'}
        if not isinstance(e,dict) or set(e)!=fields or e['protocol']!=PROTOCOL:raise RuntimeErrorCode('WORKER_GATEWAY_FRAME_INVALID')
        if e['scope']!=self.store.scope or e['audience']!=self.identities.audience:raise RuntimeErrorCode('WORKER_IDENTITY_SCOPE_DENIED')
        for k in ('worker_id','instance','request_id','profile'):identifier(e[k])
        if max(len(e[k]) for k in ('worker_id','instance','request_id'))>64:raise RuntimeErrorCode('INVALID_IDENTIFIER')
        message=e['message']
        if not isinstance(message,dict) or set(message)!={'method','params'} or not isinstance(message['params'],dict):raise RuntimeErrorCode('WORKER_GATEWAY_FRAME_INVALID')
        method=message['method'];p=message['params']
        if not isinstance(method,str) or not method.startswith('runtime.'):raise RuntimeErrorCode('WORKER_GATEWAY_FRAME_INVALID')
        method=method[8:]
        entry=(await asyncio.to_thread(document,self.profiles)).get('profiles',{}).get(e['profile'])
        if not isinstance(entry,dict) or entry.get('enabled') is not True:raise RuntimeErrorCode('WORKER_PROFILE_DENIED')
        from .worker import read_profile
        from .work_policy import security_profile
        profile,spec=await asyncio.to_thread(read_profile,entry['path'])
        mode,_=await asyncio.to_thread(security_profile,entry['path'])
        if mode!='e2ag' or profile!=e['profile'] or spec.fingerprint()!=e['profile_digest']:raise RuntimeErrorCode('PROFILE_CHANGED')
        async with self.store.db.transaction(self.store.scope) as c:
            await require(c);now=await self.store.db.now(c)
            if expiry<=max(now,time.time()):raise RuntimeErrorCode('WORKER_CERTIFICATE_EXPIRED')
            machine=await asyncio.to_thread(self.identities.machine,certificate,e['scope'],profile,e['worker_id'],now=now)
            await admission(c,e['scope'],ownership=True)
            bound=RuntimeStore(TransactionView(self.store.db,c,e['scope']),e['scope'])
            peer=(await c.execute(select(peers).where(peers.c.id==e['worker_id']))).mappings().first()
            expected=dict(scope=e['scope'],instance=e['instance'],certificate=certificate,machine_id=machine['worker_id'],profile=profile)
            if peer and any(peer[k]!=v for k,v in expected.items()):raise RuntimeErrorCode('WORKER_IDENTITY_BINDING_CHANGED')
            if not peer and method!='register':raise RuntimeErrorCode('WORKER_REGISTRATION_REQUIRED')
            # The key is scoped to an authenticated process; bodies cannot be changed on retry.
            rid=digest([e['scope'],certificate,e['worker_id'],e['instance'],e['request_id']]);hashed=digest(message)
            durable=method in {'claim','complete','finish_failed','confirm_cancel','reject_unlaunched','resolve_exit'}
            if durable:
                old=(await c.execute(select(receipts).where(receipts.c.id==rid))).mappings().first()
                if old:
                    if old['digest']!=hashed:raise RuntimeErrorCode('IDEMPOTENCY_CONFLICT')
                    return old['response']
            failure=None
            try:
                async with c.begin_nested():
                    value=await self.dispatch(c,bound,machine,e,method,p,now)
                    if durable:
                        await c.execute(receipts.insert().values(id=rid,scope=e['scope'],digest=hashed,response=value,created_at=now))
            except RuntimeErrorCode as exc:
                failure=exc
                # Preserve denial evidence after rolling back a partial claim or result write.
                target=None;aid=None
                if method=='claim':
                    target=(await c.execute(select(runs.c.id).where(runs.c.scope==bound.scope,runs.c.state=='queued',
                        runs.c.input['runtime_profile'].as_string()==profile).order_by(runs.c.created_at,runs.c.id).limit(1))).scalar_one_or_none()
                elif isinstance(p.get('binding'),list):
                    try:
                        denied_run,denied_attempt=await self.binding(c,bound,e,p['binding'],recovery=True)
                        target,aid=denied_run['id'],denied_attempt['id']
                    except RuntimeErrorCode:pass
                if target:
                    await audit(c,bound.scope,target,aid,'remote-control',Decision('remote-control','deny',(exc.code,),
                        evidence={'certificate_sha256':certificate,'worker_id':e['worker_id'],'method':method}),now)
        if failure:raise failure
        return value

    async def binding(self,c,store,e,binding,*,recovery=False):
        if not isinstance(binding,list) or len(binding)!=5 or type(binding[4]) is not int:raise RuntimeErrorCode('INVALID_BINDING')
        for x in binding[:4]:identifier(x)
        run=await store._run(c,binding[0])
        a=(await c.execute(select(attempts).where(attempts.c.id==binding[1],attempts.c.run_id==run['id']))).mappings().first()
        if not a or (a['worker_id'],a['worker_instance'],a['epoch'])!=tuple(binding[2:]):raise RuntimeErrorCode('STALE_OWNER')
        peer=(await c.execute(select(peers).where(peers.c.id==a['worker_id'],peers.c.scope==store.scope))).mappings().first()
        caller=(await c.execute(select(peers).where(peers.c.id==e['worker_id']))).mappings().one()
        if not peer or any(peer[k]!=caller[k] for k in ('certificate','machine_id','profile')):raise RuntimeErrorCode('WORKER_IDENTITY_SCOPE_DENIED')
        if not recovery and (binding[2],binding[3])!=(e['worker_id'],e['instance']):raise RuntimeErrorCode('STALE_OWNER')
        if run['attempt_id']!=a['id'] or run['input'].get('runtime_profile')!=e['profile'] or run['input'].get('profile_digest')!=e['profile_digest']:
            raise RuntimeErrorCode('STALE_OWNER')
        return run,dict(a)

    async def dispatch(self,c,s,m,e,method,p,now):
        allowed={
            'register':{'host','pid'},'drain':set(),'claim':set(),'get':{'binding'},'recoverable':{'cursor','limit'},
            'authorize':{'binding','action','session_id'},'heartbeat':{'binding'},'report':{'binding','report'},
            'observe':{'binding','observation','adapter','boot_id'},'reject_unlaunched':{'binding','code'},
            'mark_unknown':{'binding','code'},'finish_failed':{'binding','code'},'confirm_cancel':{'binding','evidence_ref'},
            'retain':{'binding','key','kind'},'complete':{'binding','key'},
            'resolve_exit':{'binding','version','receipt','session_id','result_key','failure'},
            'upload.begin':{'binding','key','size'},'upload.chunk':{'binding','key','offset','data'},
            'upload.finish':{'binding','key'},'artifact.read':{'binding','key','offset'}}
        if method not in allowed or set(p)!=allowed[method]:raise RuntimeErrorCode('REMOTE_METHOD_DENIED')
        if method=='register':
            if type(p['pid']) is not int:raise RuntimeErrorCode('INVALID_WORKER')
            await s.register_worker(e['worker_id'],e['instance'],p['host'],p['pid'])
            old=(await c.execute(select(peers.c.id).where(peers.c.id==e['worker_id']))).first()
            if not old:await c.execute(peers.insert().values(id=e['worker_id'],scope=s.scope,instance=e['instance'],
                certificate=m['certificate_sha256'],machine_id=m['worker_id'],profile=e['profile'],created_at=now))
            return True
        if method=='drain':await s.drain(e['worker_id']);return True
        if method=='claim':
            # Restarted processes cannot bypass an unresolved execution slot.
            capacity=m.get('slots',1)
            if type(capacity) is not int or not 1<=capacity<=32:raise RuntimeErrorCode('INVALID_WORKER')
            busy=(await c.execute(select(func.count()).select_from(attempts.join(peers,peers.c.id==attempts.c.worker_id))
                .where(peers.c.scope==s.scope,peers.c.certificate==m['certificate_sha256'],peers.c.machine_id==m['worker_id'],
                attempts.c.state.in_(ACTIVE_ATTEMPTS|{'result_unknown'})))).scalar_one()
            if busy>=capacity:return None
            from .worker import read_profile
            from .faults import from_spec
            entry=(await asyncio.to_thread(document,self.profiles))['profiles'][e['profile']]
            _,spec=await asyncio.to_thread(read_profile,entry['path'])
            if spec.fingerprint()!=e['profile_digest']:raise RuntimeErrorCode('PROFILE_CHANGED')
            a=await s.claim(e['worker_id'],e['instance'],profile_id=e['profile'],recovery_policy=from_spec(spec))
            if a:
                run=await s.get(a['run_id'])
                if run['input'].get('profile_digest')!=e['profile_digest']:raise RuntimeErrorCode('PROFILE_CHANGED')
                await authorize_work(s,run,a,'execute',verifier=WorkAuthorizationVerifier(s,self.policy))
            return a
        if method=='recoverable':
            if type(p['limit']) is not int or not 1<=p['limit']<=100:raise RuntimeErrorCode('INVALID_LIMIT')
            await s.expire()
            q=select(attempts).join(runs,runs.c.attempt_id==attempts.c.id).join(peers,peers.c.id==attempts.c.worker_id).where(
                runs.c.scope==s.scope,runs.c.state=='reconciliation_required',peers.c.certificate==m['certificate_sha256'],
                peers.c.machine_id==m['worker_id'],peers.c.profile==e['profile'])
            if p['cursor']:q=q.where(runs.c.id>p['cursor'])
            rows=(await c.execute(q.order_by(runs.c.id).limit(p['limit']))).mappings().all()
            return [{'run_id':a['run_id'],'binding':[a['run_id'],a['id'],a['worker_id'],a['worker_instance'],a['epoch']]} for a in rows]
        b=p['binding'];run,a=await self.binding(c,s,e,b,recovery=method in {'get','authorize','resolve_exit','upload.begin','upload.chunk','upload.finish','artifact.read'})
        if method=='get':
            value=await s.get(run['id']);value['attempts']=[x for x in value['attempts'] if x['id']==a['id']];return value
        if method in {'heartbeat','report'} or (method=='authorize' and p['action']=='execute'):
            from .credential_schema import credentials
            family=digest([s.scope,self.identities.audience,m['certificate_sha256'],b])
            state=(await c.execute(select(credentials.c.state).where(credentials.c.id==family))).scalar_one_or_none()
            if state=='revoked':raise RuntimeErrorCode('WORKER_CREDENTIAL_REVOKED')
        if method=='authorize':
            if p['action']=='execute' and (a['worker_id'],a['worker_instance'])!=(e['worker_id'],e['instance']):raise RuntimeErrorCode('STALE_OWNER')
            await authorize_work(s,run,a,p['action'],session_id=p['session_id'],verifier=WorkAuthorizationVerifier(s,self.policy));return True
        if method=='heartbeat':return await s.heartbeat(*b)
        if method=='report':
            report=p['report']
            if not isinstance(report,dict) or set(report)-{'sequence','phase','session_id','process_id','process_instance','remote','output','output_observed_at','authorization_epoch','output_excerpt','usage'}:
                raise RuntimeErrorCode('INVALID_REPORT')
            return await s.report(*b,**report)
        if method=='observe':
            if p['adapter']!='local-docker-v1':raise RuntimeErrorCode('RUNTIME_BINDING_NOT_FOUND')
            obs=Observation(**p['observation'])
            if obs.handle!=a['id']:raise RuntimeErrorCode('RUNTIME_BINDING_NOT_FOUND')
            await s.observe_process(b,obs,adapter=p['adapter'],boot_id=p['boot_id']);return True
        if method in {'reject_unlaunched','mark_unknown','finish_failed'}:
            await getattr(s,method)(b,p['code']);return True
        if method=='confirm_cancel':await s.confirm_cancel(*b,p['evidence_ref']);return True
        if method.startswith('upload.') or method=='artifact.read':return await self.upload(c,s,a,p,method,now)
        if method=='retain':
            await self.blob(c,s,a,p['key']);return await s.retain_output(b,self.provider,p['key'],p['kind'])
        if method=='complete':
            await self.blob(c,s,a,p['key'])
            await authorize_work(s,run,a,'accept_result',session_id=a['session_id'],verifier=WorkAuthorizationVerifier(s,self.policy))
            result=await s.complete(*b,self.provider,p['key'],JsonlBridge.validate)
        elif method=='resolve_exit':
            for key in ('stdout','stderr'):await self.blob(c,s,a,p['receipt'][key])
            if p['result_key']:
                await self.blob(c,s,a,p['result_key'])
                await authorize_work(s,run,a,'accept_result',session_id=p['session_id'],verifier=WorkAuthorizationVerifier(s,self.policy))
            result=await s.resolve_exit(dict(run,version=p['version']),a,p['receipt'],self.provider,
                session_id=p['session_id'],result_key=p['result_key'],failure=p['failure'])
        else:raise RuntimeErrorCode('REMOTE_METHOD_DENIED')
        await audit(c,s.scope,run['id'],a['id'],'remote-result',Decision('worker-identity','allow',('REMOTE_RESULT_ACCEPTED',),
            evidence={'certificate_sha256':m['certificate_sha256'],'original_worker':a['worker_id'],'original_epoch':a['epoch']}),now)
        return result

    async def blob(self,c,s,a,key):
        row=(await c.execute(select(uploads).where(uploads.c.scope==s.scope,uploads.c.attempt_id==a['id'],uploads.c.key==key,uploads.c.complete==True))).mappings().first()
        if not row:raise RuntimeErrorCode('ARTIFACT_ACCESS_DENIED')
        return row

    async def upload(self,c,s,a,p,method,now):
        key=p['key'];self.provider.path(key)
        uid=digest([s.scope,a['id'],key]);row=(await c.execute(select(uploads).where(uploads.c.id==uid))).mappings().first()
        if method=='upload.begin':
            size=p['size']
            if type(size) is not int or not 0<=size<=8*1024*1024:raise RuntimeErrorCode('ARTIFACT_TOO_LARGE')
            if row:
                if row['size']!=size:raise RuntimeErrorCode('UPLOAD_CONFLICT')
            else:
                count,total=(await c.execute(select(func.count(),func.coalesce(func.sum(uploads.c.size),0)).where(uploads.c.scope==s.scope,uploads.c.attempt_id==a['id']))).one()
                if count>=16 or total+size>32*1024*1024:raise RuntimeErrorCode('UPLOAD_QUOTA_EXCEEDED')
                row=dict(id=uid,scope=s.scope,attempt_id=a['id'],key=key,size=size,received=0,complete=False,created_at=now)
                await c.execute(uploads.insert().values(**row))
            return {'received':row['received'],'complete':row['complete']}
        if not row:raise RuntimeErrorCode('UPLOAD_NOT_FOUND')
        if method=='upload.chunk':
            offset=p['offset']
            if not isinstance(p['data'],str) or len(p['data'])>87384:raise RuntimeErrorCode('UPLOAD_CHUNK_INVALID')
            try:data=base64.b64decode(p['data'],validate=True)
            except (ValueError,TypeError):raise RuntimeErrorCode('UPLOAD_CHUNK_INVALID') from None
            if type(offset) is not int or offset<0 or not 0<len(data)<=65536 or offset+len(data)>row['size']:raise RuntimeErrorCode('UPLOAD_CHUNK_INVALID')
            old=(await c.execute(select(chunks).where(chunks.c.upload_id==uid,chunks.c.offset==offset))).mappings().first()
            import hashlib
            hashed=hashlib.sha256(data).hexdigest()
            if old:
                if old['key']!=hashed or old['size']!=len(data):raise RuntimeErrorCode('UPLOAD_CONFLICT')
            else:
                if row['complete'] or offset!=row['received']:raise RuntimeErrorCode('UPLOAD_OFFSET_CONFLICT')
                blob=await asyncio.to_thread(self.provider.put,[data])
                await c.execute(chunks.insert().values(upload_id=uid,offset=offset,key=blob.key,size=blob.size))
                await c.execute(uploads.update().where(uploads.c.id==uid).values(received=offset+len(data)))
            return {'received':max(row['received'],offset+len(data))}
        if method=='upload.finish':
            if not row['complete']:
                if row['received']!=row['size']:raise RuntimeErrorCode('UPLOAD_INCOMPLETE')
                parts=(await c.execute(select(chunks).where(chunks.c.upload_id==uid).order_by(chunks.c.offset))).mappings().all()
                def join():
                    for part in parts:yield from self.provider.read(part['key'])
                blob=await asyncio.to_thread(self.provider.put,join(),expected_digest=key)
                if blob.size!=row['size']:raise RuntimeErrorCode('UPLOAD_CONFLICT')
                await c.execute(uploads.update().where(uploads.c.id==uid).values(complete=True))
            return {'key':key,'size':row['size']}
        if not row['complete']:raise RuntimeErrorCode('ARTIFACT_ACCESS_DENIED')
        offset=p['offset']
        if type(offset) is not int or not 0<=offset<=row['size']:raise RuntimeErrorCode('UPLOAD_OFFSET_CONFLICT')
        def read():
            with self.provider.path(key).open('rb') as f:f.seek(offset);return f.read(65536)
        return {'data':base64.b64encode(await asyncio.to_thread(read)).decode(),'size':row['size']}
