"""Single trusted operator service over the durable runtime. No executor in HTTP."""
import asyncio
import base64
import copy
import json
import math
from pathlib import Path

from sqlalchemy import and_, or_, select

from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import RuntimeErrorCode
from app.runtime.principals import admit
from app.runtime.schema import artifacts, attempts, events, runs
from app.runtime.store import RuntimeStore, digest
from app.runtime.work_policy import security_profile
from app.runtime.worker import read_profile
from app.services.task_inputs import (
    CONSOLE_ORIGIN,
    LEGACY_INPUT_KINDS,
    dynamic_input,
    is_console_input,
    registration,
)


class TaskConsole:
    def __init__(self, db, scope, profiles_dir, artifacts_root, *, policy=None):
        self.db, self.scope = db, scope
        self.policy=policy
        self.tools=None
        self.principal=None
        self.access_enforced=False
        self.store = RuntimeStore(db,scope)
        self.profiles_dir = Path(profiles_dir)
        self.provider = LocalArtifacts(Path(artifacts_root))

    def identity_provider(self):
        return self.policy.identity_provider() if self.policy and hasattr(self.policy,'identity_provider') else None

    def for_principal(self,principal):
        value=copy.copy(self);value.principal=principal;value.access_enforced=True
        return value

    def access_row(self):
        if self.principal is None:return None
        provider=self.identity_provider()
        if provider is None:raise RuntimeErrorCode('AUTH_PRINCIPAL_REVOKED')
        return provider.current(self.principal)

    def profile_allowed(self,profile_id,action='run.submit'):
        row=self.access_row()
        return row is None or action in row['profiles'].get(profile_id,{}).get('actions',[])

    def check_access(self,run,action):
        if not self.access_enforced:return
        owner=run['input'].get('principal')
        if self.principal is None:
            if owner is not None:raise RuntimeErrorCode('NOT_FOUND')
            return
        row=self.access_row()
        own=owner is not None and all(owner[k]==self.principal[k] for k in ('issuer','id','scope','subject','actor'))
        actions=row['profiles'].get(run['input'].get('runtime_profile'),{}).get('actions',[]) if own else row.get('objects',{}).get(run['id'],[])
        if action not in actions:raise RuntimeErrorCode('NOT_FOUND')

    async def authorize(self,run_id,action):
        async with self.db.read(self.scope) as c:run=await self.store._run(c,run_id)
        await asyncio.to_thread(self.check_access,run,action)
        return run

    def access_filter(self):
        if not self.access_enforced:return None
        if self.principal is None:return runs.c.input['principal'].as_string().is_(None)
        row=self.access_row();owner=runs.c.input['principal']
        profiles=[pid for pid,g in row['profiles'].items() if 'run.read' in g['actions']]
        shared=[rid for rid,actions in row.get('objects',{}).items() if 'run.read' in actions]
        own=and_(owner['issuer'].as_string()==self.principal['issuer'],owner['id'].as_string()==self.principal['id'],
            owner['subject'].as_string()==self.principal['subject'],owner['actor'].as_string()==self.principal['actor'],
            owner['scope'].as_string()==self.scope,runs.c.input['runtime_profile'].as_string().in_(profiles))
        return or_(own,runs.c.id.in_(shared))

    def _registrations(self):
        profiles=[]
        for path in sorted(self.profiles_dir.glob('*.json')):
            with path.open('rb') as stream: data=stream.read(65537)
            if len(data)>65536: raise RuntimeErrorCode('INVALID_PROFILE_CATALOG')
            raw=json.loads(data)
            display=raw.get('console',{})
            if display.get('enabled') is not True: continue
            profile_id,spec=read_profile(path,value=raw)
            if not self.profile_allowed(profile_id):continue
            if any(x['view']['id']==profile_id for x in profiles):raise RuntimeErrorCode('DUPLICATE_PROFILE_ID')
            registered=registration(display)
            registered_inputs=registered['inputs']
            # Explicit operator activation; API never accepts commands, paths or secrets.
            if spec.mounts and any(not m.read_only for m in spec.mounts):
                raise RuntimeErrorCode('PROFILE_REQUIRES_READ_ONLY_MOUNTS')
            mode,refs=security_profile(path,raw=raw)
            profiles.append(dict(inputs=registered_inputs,refs=refs,registration_digest=registered['digest'],
                requires_e2ag=registered['requires_e2ag'],contract=registered,view=dict(id=profile_id,name=str(display.get('name',profile_id))[:120],
                description=str(display.get('description',''))[:500],
                implementation=str(display.get('implementation','unknown'))[:120],
                synthetic=display.get('synthetic') is True,profile_digest=spec.fingerprint(),
                input_schema=registered['schema'] if registered['dynamic_inputs'] else None,
                dynamic_inputs=registered['dynamic_inputs'],input_kind=display['input_kind'],input_ids=[x['id'] for x in registered_inputs],
                governance=mode,policy_configured=self.policy is not None if mode=='e2ag' else True)))
        return profiles

    def catalog(self):
        return [item['view'] for item in self._registrations()]

    def samples(self):
        rows={}
        for registered in self._registrations():
            for item in registered['inputs']:
                if item['id'] in rows and rows[item['id']]!=item:raise RuntimeErrorCode('INVALID_INPUT_CATALOG')
                rows[item['id']]=item
        return list(rows.values())

    async def prepare_submission(self, profile_id, sample_id, goal, *, input_payload=None):
        registered=next((x for x in await asyncio.to_thread(self._registrations)
                         if x['view']['id']==profile_id),None)
        if registered is None:raise RuntimeErrorCode('PROFILE_NOT_AVAILABLE')
        profile=registered['view']
        if self.principal is not None and not await asyncio.to_thread(self.profile_allowed,profile_id,'run.read'):
            raise RuntimeErrorCode('AUTH_ACTION_DENIED')
        if (sample_id is None)==(input_payload is None):raise RuntimeErrorCode('INVALID_WORK_INPUT')
        if input_payload is not None:
            value=dynamic_input(registered['contract'],input_payload)
        else:
            value=next((x for x in registered['inputs'] if x['id']==sample_id),None)
            if value is None:raise RuntimeErrorCode('SAMPLE_NOT_FOUND')
        if registered['requires_e2ag'] and profile['governance']!='e2ag':
            raise RuntimeErrorCode('WORK_POLICY_REQUIRED')
        if self.principal is not None and profile['governance']!='e2ag':raise RuntimeErrorCode('WORK_POLICY_REQUIRED')
        # The client request identifies a receipt; the first accepted source owns
        # Work. OAuth refresh must never replace that Work or extend its deadline.
        owner = ({k:self.principal[k] for k in ('issuer','id','scope','subject','actor')}
                 if self.principal is not None else None)
        submission_digest=digest(dict(owner=owner,profile_id=profile_id,sample_id=sample_id,
            input_payload=input_payload,goal=goal,registered_input=value,
            registration_digest=registered['registration_digest'],profile_digest=profile['profile_digest'],
            secret_refs=registered['refs']))
        return dict(registered=registered, profile=profile, value=value, submission_digest=submission_digest)

    @staticmethod
    def schedule_binding(prepared, descriptor):
        if descriptor is None or prepared['profile']['governance'] != 'e2ag':
            raise RuntimeErrorCode('WORK_POLICY_REQUIRED')
        return dict(source='service-policy', submission_digest=prepared['submission_digest'],
            profile_digest=prepared['profile']['profile_digest'],
            **{key:descriptor[key] for key in ('subject','actor','policy_id','policy_digest')})

    async def submit(self, profile_id, sample_id, goal, key, *, input_payload=None,
                     expected_schedule_binding=None, admission_guard=None):
        prepared=await self.prepare_submission(profile_id,sample_id,goal,input_payload=input_payload)
        registered,profile,value=(prepared[k] for k in ('registered','profile','value'))
        submission_digest=prepared['submission_digest']
        if self.principal is not None:key='principal-'+digest([self.principal['issuer'],self.principal['id'],key])
        receipt=await self.store.submission_receipt(key,submission_digest)
        if receipt is not None:return await self.detail(receipt['id'])
        descriptor=None;refs=registered['refs']
        if profile['governance']=='e2ag':
            if self.policy is None:raise RuntimeErrorCode('WORK_POLICY_REQUIRED')
            descriptor=await asyncio.to_thread(admit,self.policy,profile_id,profile['profile_digest'],refs,self.principal)
        if expected_schedule_binding is not None:
            if self.principal is not None or self.schedule_binding(prepared,descriptor)!=expected_schedule_binding:
                raise RuntimeErrorCode('SCHEDULE_BINDING_CHANGED')
        run=await self.store.submit(key,dict(origin=CONSOLE_ORIGIN,
            **({'principal':self.principal} if self.principal is not None else {}),
            input_registration=dict(digest=registered['registration_digest'],
                **({'result_schema':registered['contract']['result_schema']} if registered['contract']['result_schema'] is not None else {})),
            runtime_profile=profile_id,profile_digest=profile['profile_digest'],
            task_kind=profile['input_kind'],payload=dict(goal=goal,sample=value),
            secret_refs=refs,display=dict(name=profile['name'],implementation=profile['implementation'],synthetic=profile['synthetic'])),authorization=descriptor,submission_digest=submission_digest,admission_guard=admission_guard)
        return await self.detail(run['id'])

    @staticmethod
    def summary(run):
        value=run['input'];payload=value.get('payload',{})
        return {**{k:run[k] for k in ('id','state','version','attempt_id','created_at','updated_at','cancel_requested')},
            'goal':payload.get('goal',''), 'sample_id':payload.get('sample',{}).get('id'),
            'task_kind':value.get('task_kind'),'profile_id':value.get('runtime_profile'), 'display':value.get('display',{})}

    async def list(self, *, limit=30, cursor=None):
        if not 1<=limit<=100:raise RuntimeErrorCode('INVALID_LIMIT')
        query=select(runs).where(runs.c.scope==self.scope,or_(
            runs.c.input['origin'].as_string()==CONSOLE_ORIGIN,
            and_(runs.c.input['origin'].as_string().is_(None),
                 runs.c.input['task_kind'].as_string().in_(LEGACY_INPUT_KINDS))))
        if cursor:
            try:
                stamp,rid=json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if type(stamp) not in (int,float) or not math.isfinite(stamp) or not isinstance(rid,str):raise ValueError()
            except Exception:raise RuntimeErrorCode('INVALID_CURSOR') from None
            query=query.where(or_(runs.c.created_at<stamp,and_(runs.c.created_at==stamp,runs.c.id<rid)))
        async with self.db.read(self.scope) as c:
            access=await asyncio.to_thread(self.access_filter)
            if access is not None:query=query.where(access)
            rows=(await c.execute(query.order_by(runs.c.created_at.desc(),runs.c.id.desc()).limit(limit+1))).mappings().all()
            now=await self.db.now(c)
        page=rows[:limit];next_cursor=None
        if len(rows)>limit:
            next_cursor=base64.urlsafe_b64encode(json.dumps([page[-1]['created_at'],page[-1]['id']]).encode()).decode()
        return dict(items=[self.summary(x) for x in page],next_cursor=next_cursor,observed_at=now)

    async def detail(self, run_id):
        async with self.db.read(self.scope) as c:
            run=await self.store._run(c,run_id)
            if not is_console_input(run['input']):raise RuntimeErrorCode('NOT_FOUND')
            await asyncio.to_thread(self.check_access,run,'run.read')
            now=await self.db.now(c)
            rows=(await c.execute(select(attempts).where(attempts.c.run_id==run_id)
                .order_by(attempts.c.epoch.desc()).limit(20))).mappings().all()
            blobs=(await c.execute(select(artifacts).where(artifacts.c.run_id==run_id)
                .order_by(artifacts.c.created_at.desc(),artifacts.c.id).limit(100))).mappings().all()
            recent=(await c.execute(select(events).where(events.c.run_id==run_id)
                .order_by(events.c.version.desc()).limit(30))).mappings().all()
            # Kind comes from persisted event links, never by guessing JSON contents.
            kind_events=(await c.execute(select(events.c.payload,events.c.type).where(events.c.run_id==run_id,
                events.c.type.in_(['run.completed','attempt.output_retained','attempt.exit_reconciled'])))).all()
            from app.runtime.faults import view as recovery_view
            recovery=await recovery_view(c,self.scope,run_id)
        kinds={}
        for payload,kind in kind_events:
            detail=payload['detail']
            if kind=='run.completed':kinds[detail['artifact_id']]='result'
            elif kind=='attempt.output_retained':kinds[detail['artifact_id']]=detail['kind']
            else:
                for name,aid in detail.get('artifacts',{}).items():kinds[aid]=name
        views=[]
        for a in rows:
            obs=a['remote'].get('runtime',{})
            terminal=a['finished_at'] is not None
            fresh=terminal or (a['state']!='result_unknown' and a['lease_until']>now and obs.get('observed_at',0)>=now-15)
            views.append({**{k:a[k] for k in ('id','epoch','state','session_id','worker_id','process_id',
                'started_at','finished_at','last_output_at','error')},
                'freshness':'final' if terminal else ('fresh' if fresh else 'stale'),
                'runtime_state':obs.get('state'),
                'confirmed_running':bool(fresh and not terminal and obs.get('state')!='paused' and obs.get('alive') and obs.get('pid') and obs.get('process_instance')),
                'output_excerpt':a['remote'].get('progress',{}).get('text','')})
        from app.runtime.work_authorizations import view
        authorization=await view(self.store,run_id)
        access=[]
        for action in ('run.read','run.cancel','run.revoke','artifact.read','tool.read','tool.approve'):
            try:await asyncio.to_thread(self.check_access,run,action);access.append(action)
            except RuntimeErrorCode as exc:
                if exc.code!='NOT_FOUND':raise
        return dict(self.summary(run),access=access,observed_at=now,attempts=views,authorization=authorization,recovery=recovery,
            sample=run['input']['payload']['sample'],
            artifacts=[{**{k:b[k] for k in ('id','attempt_id','digest','size','created_at')},'kind':kinds.get(b['id'],'raw')}
                for b in blobs],
            events=[dict(id=e['id'],version=e['version'],type=e['type'],state=e['payload']['state'],created_at=e['created_at']) for e in recent],
            attempts_has_more=bool(rows and rows[-1]['epoch']>1),artifacts_at_limit=len(blobs)==100)

    async def artifact(self, run_id, artifact_id):
        async with self.db.read(self.scope) as c:
            run=await self.store._run(c,run_id)
            if not is_console_input(run['input']):raise RuntimeErrorCode('NOT_FOUND')
            await asyncio.to_thread(self.check_access,run,'run.read')
            await asyncio.to_thread(self.check_access,run,'artifact.read')
            b=(await c.execute(select(artifacts).where(artifacts.c.id==artifact_id,artifacts.c.run_id==run_id))).mappings().first()
        if not b:raise RuntimeErrorCode('NOT_FOUND')
        if b['provider']!=self.provider.name:raise RuntimeErrorCode('ARTIFACT_PROVIDER_UNAVAILABLE')
        blob=await asyncio.to_thread(self.provider.verify,b['storage_key'])
        if (blob.digest,blob.size)!=(b['digest'],b['size']):raise RuntimeErrorCode('ARTIFACT_DIGEST_MISMATCH')
        return dict(b)

    async def stream_artifact(self,run_id,blob):
        run=await self.authorize(run_id,'artifact.read')
        stream=iter(self.provider.read(blob['storage_key']))
        try:
            while True:
                chunk=await asyncio.to_thread(next,stream,None)
                if chunk is None:break
                await asyncio.to_thread(self.check_access,run,'run.read')
                await asyncio.to_thread(self.check_access,run,'artifact.read')
                yield chunk
        finally:
            if hasattr(stream,'close'):await asyncio.to_thread(stream.close)

    async def preview(self, run_id, artifact_id):
        blob=await self.artifact(run_id,artifact_id)
        if blob['size']>256*1024:raise RuntimeErrorCode('PREVIEW_TOO_LARGE')
        data=await asyncio.to_thread(lambda:b''.join(self.provider.read(blob['storage_key'])))
        try:text=data.decode('utf-8')
        except UnicodeError:raise RuntimeErrorCode('PREVIEW_NOT_TEXT') from None
        detail=await self.detail(run_id)
        kind=next((a['kind'] for a in detail['artifacts'] if a['id']==artifact_id),'raw')
        markdown=None
        if kind=='result':
            try:value=json.loads(text)
            except ValueError:value={}
            if isinstance(value,dict) and isinstance(value.get('markdown'),str):markdown=value['markdown']
        await self.authorize(run_id,'artifact.read')
        return dict(id=artifact_id,kind=kind,text=text,markdown=markdown,size=blob['size'],digest=blob['digest'])
