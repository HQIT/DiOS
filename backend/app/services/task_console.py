"""Single trusted operator service over the durable runtime. No executor in HTTP."""
import asyncio
import base64
import json
import math
from pathlib import Path
from sqlalchemy import select, and_, or_
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import RuntimeErrorCode
from app.runtime.schema import runs, attempts, artifacts, events
from app.runtime.store import RuntimeStore
from app.services.task_inputs import KINDS, inputs
from app.runtime.worker import read_profile
from app.runtime.work_policy import security_profile


class TaskConsole:
    def __init__(self, db, scope, profiles_dir, artifacts_root, *, policy=None):
        self.db, self.scope = db, scope
        self.policy=policy
        self.tools=None
        self.store = RuntimeStore(db,scope)
        self.profiles_dir = Path(profiles_dir)
        self.provider = LocalArtifacts(Path(artifacts_root))

    def catalog(self):
        profiles=[]
        for path in sorted(self.profiles_dir.glob('*.json')):
            if path.stat().st_size>65536: raise RuntimeErrorCode('INVALID_PROFILE_CATALOG')
            raw=json.loads(path.read_text(encoding='utf-8'))
            display=raw.get('console',{})
            if display.get('enabled') is not True: continue
            profile_id,spec=read_profile(path)
            if any(x['id']==profile_id for x in profiles):raise RuntimeErrorCode('DUPLICATE_PROFILE_ID')
            if display.get('input_kind') not in KINDS:continue
            registered_inputs=inputs(display)
            # Explicit operator activation; API never accepts commands, paths or secrets.
            if spec.mounts and any(not m.read_only for m in spec.mounts):
                raise RuntimeErrorCode('PROFILE_REQUIRES_READ_ONLY_MOUNTS')
            mode,_=security_profile(path)
            profiles.append(dict(id=profile_id,name=str(display.get('name',profile_id))[:120],
                description=str(display.get('description',''))[:500],
                implementation=str(display.get('implementation','unknown'))[:120],
                synthetic=display.get('synthetic') is True,profile_digest=spec.fingerprint(),
                input_kind=display['input_kind'],input_ids=[x['id'] for x in registered_inputs],
                governance=mode,policy_configured=self.policy is not None if mode=='e2ag' else True))
        return profiles

    def samples(self):
        rows={}
        for path in sorted(self.profiles_dir.glob('*.json')):
            raw=json.loads(path.read_text(encoding='utf-8'));display=raw.get('console',{})
            if display.get('enabled') is not True:continue
            for item in inputs(display):
                if item['id'] in rows and rows[item['id']]!=item:raise RuntimeErrorCode('INVALID_INPUT_CATALOG')
                rows[item['id']]=item
        return list(rows.values())

    async def submit(self, profile_id, sample_id, goal, key):
        profiles=await asyncio.to_thread(self.catalog)
        profile=next((x for x in profiles if x['id']==profile_id),None)
        if not profile:raise RuntimeErrorCode('PROFILE_NOT_AVAILABLE')
        value=next((x for x in await asyncio.to_thread(self.samples) if x['id']==sample_id and sample_id in profile['input_ids']),None)
        if value is None:raise RuntimeErrorCode('SAMPLE_NOT_FOUND')
        if profile['input_kind']=='knowledge-evidence' and profile['governance']!='e2ag':raise RuntimeErrorCode('WORK_POLICY_REQUIRED')
        descriptor=None;refs=[]
        if profile['governance']=='e2ag':
            if self.policy is None:raise RuntimeErrorCode('WORK_POLICY_REQUIRED')
            def resolve_registration():
                for path in self.profiles_dir.glob('*.json'):
                    raw=json.loads(path.read_text(encoding='utf-8'))
                    if raw.get('id')!=profile_id or raw.get('console',{}).get('enabled') is not True:continue
                    pid,spec=read_profile(path)
                    if pid==profile_id:
                        mode,requests=security_profile(path)
                        if mode!='e2ag' or spec.fingerprint()!=profile['profile_digest']:
                            raise RuntimeErrorCode('PROFILE_CHANGED')
                        return requests
                raise RuntimeErrorCode('PROFILE_NOT_AVAILABLE')
            refs=await asyncio.to_thread(resolve_registration)
            descriptor=await asyncio.to_thread(self.policy.admission,profile_id,profile['profile_digest'],refs)
        run=await self.store.submit(key,dict(runtime_profile=profile_id,profile_digest=profile['profile_digest'],
            task_kind=profile['input_kind'],payload=dict(goal=goal,sample=value),
            secret_refs=refs,display=dict(name=profile['name'],implementation=profile['implementation'],synthetic=profile['synthetic'])),authorization=descriptor)
        return await self.detail(run['id'])

    @staticmethod
    def summary(run):
        value=run['input'];payload=value.get('payload',{})
        return {**{k:run[k] for k in ('id','state','version','attempt_id','created_at','updated_at','cancel_requested')},
            'goal':payload.get('goal',''), 'sample_id':payload.get('sample',{}).get('id'),
            'task_kind':value.get('task_kind'),'profile_id':value.get('runtime_profile'), 'display':value.get('display',{})}

    async def list(self, *, limit=30, cursor=None):
        if not 1<=limit<=100:raise RuntimeErrorCode('INVALID_LIMIT')
        query=select(runs).where(runs.c.scope==self.scope,runs.c.input['task_kind'].as_string().in_(KINDS))
        if cursor:
            try:
                stamp,rid=json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if type(stamp) not in (int,float) or not math.isfinite(stamp) or not isinstance(rid,str):raise ValueError()
            except Exception:raise RuntimeErrorCode('INVALID_CURSOR') from None
            query=query.where(or_(runs.c.created_at<stamp,and_(runs.c.created_at==stamp,runs.c.id<rid)))
        async with self.db.read(self.scope) as c:
            rows=(await c.execute(query.order_by(runs.c.created_at.desc(),runs.c.id.desc()).limit(limit+1))).mappings().all()
            now=await self.db.now(c)
        page=rows[:limit];next_cursor=None
        if len(rows)>limit:
            next_cursor=base64.urlsafe_b64encode(json.dumps([page[-1]['created_at'],page[-1]['id']]).encode()).decode()
        return dict(items=[self.summary(x) for x in page],next_cursor=next_cursor,observed_at=now)

    async def detail(self, run_id):
        async with self.db.read(self.scope) as c:
            run=await self.store._run(c,run_id)
            if run['input'].get('task_kind') not in KINDS:raise RuntimeErrorCode('NOT_FOUND')
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
                'confirmed_running':bool(fresh and not terminal and obs.get('alive') and obs.get('pid') and obs.get('process_instance')),
                'output_excerpt':a['remote'].get('progress',{}).get('text','')})
        from app.runtime.work_authorizations import view
        authorization=await view(self.store,run_id)
        return dict(self.summary(run),observed_at=now,attempts=views,authorization=authorization,recovery=recovery,
            sample=run['input']['payload']['sample'],
            artifacts=[{**{k:b[k] for k in ('id','attempt_id','digest','size','created_at')},'kind':kinds.get(b['id'],'raw')}
                for b in blobs],
            events=[dict(id=e['id'],version=e['version'],type=e['type'],state=e['payload']['state'],created_at=e['created_at']) for e in recent],
            attempts_has_more=bool(rows and rows[-1]['epoch']>1),artifacts_at_limit=len(blobs)==100)

    async def artifact(self, run_id, artifact_id):
        async with self.db.read(self.scope) as c:
            run=await self.store._run(c,run_id)
            if run['input'].get('task_kind') not in KINDS:raise RuntimeErrorCode('NOT_FOUND')
            b=(await c.execute(select(artifacts).where(artifacts.c.id==artifact_id,artifacts.c.run_id==run_id))).mappings().first()
        if not b:raise RuntimeErrorCode('NOT_FOUND')
        if b['provider']!=self.provider.name:raise RuntimeErrorCode('ARTIFACT_PROVIDER_UNAVAILABLE')
        blob=await asyncio.to_thread(self.provider.verify,b['storage_key'])
        if (blob.digest,blob.size)!=(b['digest'],b['size']):raise RuntimeErrorCode('ARTIFACT_DIGEST_MISMATCH')
        return dict(b)

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
        return dict(id=artifact_id,kind=kind,text=text,markdown=markdown,size=blob['size'],digest=blob['digest'])
