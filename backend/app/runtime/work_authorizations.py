"""Durable E2AG work authorization for the trusted local control service.

Grants are database-owned authorization records, not transferable bearer tokens.
Remote machine authentication and sender-bound token issuance remain #33 work.
"""
from .work_policy import policy_matches
from .principals import admit
import asyncio
from uuid import uuid4
from sqlalchemy import select
from .database import RuntimeErrorCode
from .schema import attempts, runs
from .work_auth_schema import work, grants, decisions, require
from .work_policy import permission
from app.services.e2ag import Decision, evaluate_work_permission


async def audit(c,scope,run_id,attempt_id,action,decision,now):
    await c.execute(decisions.insert().values(id=str(uuid4()),scope=scope,run_id=run_id,
        attempt_id=attempt_id,action=action,decision=decision.as_dict(),created_at=now))


async def create(c,scope,run,descriptor,now):
    await require(c)
    if (descriptor['profile_id']!=run['input'].get('runtime_profile')
            or descriptor['profile_digest']!=run['input'].get('profile_digest')
            or descriptor['secret_refs']!=run['input'].get('secret_refs',[])):
        raise RuntimeErrorCode('WORK_AUTHORIZATION_BINDING_MISMATCH')
    if type(descriptor['work_ttl_seconds']) is not int or not 60<=descriptor['work_ttl_seconds']<=86400:
        raise RuntimeErrorCode('WORK_DEADLINE_INVALID')
    for action in ('execute','accept_result'):
        decision=evaluate_work_permission(descriptor['permissions'],permission(descriptor,action))
        if not decision.permits_execution:raise RuntimeErrorCode(decision.reason_codes[0])
    await c.execute(work.insert().values(id=run['input']['work_authorization_ref'],scope=scope,run_id=run['id'],
        **{key:descriptor[key] for key in ('subject','actor','policy_id','policy_digest','profile_id','profile_digest','permissions','secret_refs')},
        state='active',version=1,authorization_epoch=run['authorization_epoch'],created_at=now,
        expires_at=now+descriptor['work_ttl_seconds']))
    await audit(c,scope,run['id'],None,'admit',Decision('work-admission','allow',('WORK_AUTHORIZATION_CREATED',)),now)


async def checked_work(c,run,attempt,now,action):
    reference=run['input'].get('work_authorization_ref')
    if not reference:return None
    await require(c)
    row=(await c.execute(select(work).where(work.c.id==reference,work.c.scope==run['scope'],work.c.run_id==run['id']))).mappings().first()
    if not row:raise RuntimeErrorCode('WORK_AUTHORIZATION_NOT_FOUND')
    if (row['state']!='active' or run['revoked'] or run['cancel_requested']
            or row['authorization_epoch']!=run['authorization_epoch']
            or attempt['authorization_epoch']!=run['authorization_epoch']):raise RuntimeErrorCode('AUTH_REVOKED')
    if row['expires_at']<=now:raise RuntimeErrorCode('WORK_AUTHORIZATION_EXPIRED')
    if (row['profile_id']!=run['input'].get('runtime_profile') or row['profile_digest']!=run['input'].get('profile_digest')
            or row['policy_digest']!=run['input'].get('work_policy_digest')
            or row['secret_refs']!=run['input'].get('secret_refs',[])):raise RuntimeErrorCode('WORK_AUTHORIZATION_BINDING_MISMATCH')
    decision=evaluate_work_permission(row['permissions'],permission(row,action))
    if not decision.permits_execution:raise RuntimeErrorCode(decision.reason_codes[0])
    return dict(row)


async def require_current(c,run,attempt,now,action='execute',*,session_id=None):
    row=await checked_work(c,run,attempt,now,action)
    if row is None:return
    grant=(await c.execute(select(grants).where(grants.c.attempt_id==attempt['id']))).mappings().first()
    if not grant or grant['state']!='active':raise RuntimeErrorCode('EXECUTION_GRANT_REQUIRED')
    if action=='execute' and grant['expires_at']<=now:raise RuntimeErrorCode('EXECUTION_GRANT_EXPIRED')
    expected=dict(authorization_id=row['id'],run_id=run['id'],worker_id=attempt['worker_id'],
        worker_instance=attempt['worker_instance'],lease_epoch=attempt['epoch'],authorization_epoch=attempt['authorization_epoch'])
    if any(grant[k]!=v for k,v in expected.items()):raise RuntimeErrorCode('EXECUTION_GRANT_BINDING_MISMATCH')
    sid=session_id or attempt['session_id']
    if sid and grant['session_id']!=sid and (action=='execute' or grant['session_id'] is not None):
        raise RuntimeErrorCode('EXECUTION_GRANT_SESSION_MISMATCH')


async def revoke(c,run,now):
    reference=run['input'].get('work_authorization_ref')
    if not reference:return
    await require(c)
    await c.execute(work.update().where(work.c.id==reference,work.c.run_id==run['id'],work.c.scope==run['scope'])
        .values(state='revoked',authorization_epoch=run['authorization_epoch']+1,version=work.c.version+1))
    await c.execute(grants.update().where(grants.c.authorization_id==reference).values(state='revoked',version=grants.c.version+1))
    await audit(c,run['scope'],run['id'],run['attempt_id'],'revoke',Decision('work-revocation','deny',('AUTH_REVOKED',)),now)


async def close(c,run):
    reference=run['input'].get('work_authorization_ref')
    if not reference:return
    await require(c)
    await c.execute(work.update().where(work.c.id==reference,work.c.scope==run['scope'],work.c.state=='active')
        .values(state='closed',version=work.c.version+1))
    await c.execute(grants.update().where(grants.c.authorization_id==reference,grants.c.state=='active')
        .values(state='closed',version=grants.c.version+1))


class WorkAuthorizationVerifier:
    def __init__(self,store,policy):self.store,self.policy=store,policy

    async def __call__(self,context,action):
        if context.scope!=self.store.scope:raise RuntimeErrorCode('WORKER_MISMATCH')
        if not context.authorization_ref:raise RuntimeErrorCode('WORK_AUTHORIZATION_REQUIRED')
        # Configuration I/O stays outside the transaction; authoritative state is checked again inside.
        async with self.store.db.read(self.store.scope) as c:
            await require(c)
            row=(await c.execute(select(work).where(work.c.id==context.authorization_ref,work.c.scope==self.store.scope))).mappings().first()
            if not row:raise RuntimeErrorCode('WORK_AUTHORIZATION_NOT_FOUND')
            captured=dict(row)
            principal=(await self.store._run(c,context.run_id))['input'].get('principal')
        policy_error=None
        try:
            current=await asyncio.to_thread(admit,self.policy,captured['profile_id'],captured['profile_digest'],captured['secret_refs'],principal)
            # Legacy grants remain valid only against the exact original whole-file digest.
            if not policy_matches(current,captured['policy_digest']):
                policy_error=RuntimeErrorCode('WORK_POLICY_CHANGED')
        except RuntimeErrorCode as exc:policy_error=exc
        except (OSError,ValueError):policy_error=RuntimeErrorCode('WORK_POLICY_UNAVAILABLE')
        failure=None
        async with self.store.db.transaction(self.store.scope) as c:
            await require(c)
            now=await self.store.db.now(c)
            try:
                run=await self.store._run(c,context.run_id)
                a=(await c.execute(select(attempts).where(attempts.c.id==context.attempt_id,attempts.c.run_id==run['id']))).mappings().first()
                if not a or (a['worker_id'],a['worker_instance'],a['epoch'],a['authorization_epoch'])!=(
                    context.worker_id,context.worker_instance,context.lease_epoch,context.authorization_epoch):
                    raise RuntimeErrorCode('STALE_OWNER')
                if run['attempt_id']!=a['id'] or run['input'].get('work_authorization_ref')!=context.authorization_ref:
                    raise RuntimeErrorCode('STALE_OWNER')
                if policy_error:raise policy_error
                row=await checked_work(c,run,a,now,action)
                if row is None:raise RuntimeErrorCode('WORK_AUTHORIZATION_REQUIRED')
                if a['session_id'] and a['session_id']!=context.session_id:raise RuntimeErrorCode('SESSION_CHANGED')
                if action=='execute':
                    await self.store._owned(c,run['id'],a['id'],a['worker_id'],a['worker_instance'],a['epoch'])
                    grant=(await c.execute(select(grants).where(grants.c.attempt_id==a['id']))).mappings().first()
                    binding=dict(authorization_id=row['id'],run_id=run['id'],worker_id=a['worker_id'],
                        worker_instance=a['worker_instance'],lease_epoch=a['epoch'],authorization_epoch=a['authorization_epoch'])
                    code=None
                    if grant is None:
                        await c.execute(grants.insert().values(attempt_id=a['id'],**binding,session_id=context.session_id,
                            state='active',version=1,expires_at=min(now+900,row['expires_at'])))
                        code='EXECUTION_GRANT_CREATED'
                    else:
                        if any(grant[k]!=v for k,v in binding.items()) or grant['state']!='active':raise RuntimeErrorCode('AUTH_REVOKED')
                        if grant['expires_at']<=now:raise RuntimeErrorCode('EXECUTION_GRANT_EXPIRED')
                        if grant['session_id'] and grant['session_id']!=context.session_id:raise RuntimeErrorCode('SESSION_CHANGED')
                        changes={}
                        if context.session_id and not grant['session_id']:changes['session_id']=context.session_id
                        renewed_until=min(now+900,row['expires_at'])
                        if grant['expires_at']-now<=300 and renewed_until>grant['expires_at']:
                            changes['expires_at']=renewed_until
                        if changes:
                            await c.execute(grants.update().where(grants.c.attempt_id==a['id'],grants.c.version==grant['version'])
                                .values(**changes,version=grant['version']+1))
                            code='EXECUTION_GRANT_RENEWED' if 'expires_at' in changes else 'EXECUTION_SESSION_BOUND'
                    if code:await audit(c,self.store.scope,run['id'],a['id'],action,Decision('work-grant','allow',(code,)),now)
                elif action=='accept_result':
                    from .lifecycle import admission
                    await admission(c,self.store.scope,ownership=True)
                    await require_current(c,run,a,now,'accept_result',session_id=context.session_id)
                    if a['state']=='result_unknown':
                        if run['state']!='reconciliation_required' or run['epoch']!=a['epoch']+1:raise RuntimeErrorCode('STALE_OWNER')
                    else:await self.store._owned(c,run['id'],a['id'],a['worker_id'],a['worker_instance'],a['epoch'])
                    await audit(c,self.store.scope,run['id'],a['id'],action,
                        Decision('work-result','allow',('RESULT_ACCEPTANCE_AUTHORIZED',)),now)
                else:raise RuntimeErrorCode('INVALID_AUTHORIZATION_ACTION')
            except RuntimeErrorCode as exc:
                failure=exc
                if exc.code in {'AUTH_PRINCIPAL_REVOKED','AUTH_PRINCIPAL_EXPIRED'}:
                    # Once observed, re-enabling an identity cannot resurrect its old grant.
                    await c.execute(work.update().where(work.c.id==context.authorization_ref,work.c.scope==self.store.scope,work.c.state=='active')
                        .values(state='revoked',version=work.c.version+1))
                    await c.execute(grants.update().where(grants.c.authorization_id==context.authorization_ref,grants.c.state=='active')
                        .values(state='revoked',version=grants.c.version+1))
                await audit(c,self.store.scope,context.run_id,context.attempt_id,action,
                    Decision('work-authorization','deny',(exc.code,)),now)
        if failure:raise failure
        return True


async def view(store,run_id):
    async with store.db.read(store.scope) as c:
        run=await store._run(c,run_id)
        reference=run['input'].get('work_authorization_ref')
        if not reference:return {'mode':'trusted-local'}
        await require(c);now=await store.db.now(c)
        row=(await c.execute(select(work).where(work.c.id==reference,work.c.scope==store.scope,work.c.run_id==run_id))).mappings().first()
        if not row:raise RuntimeErrorCode('WORK_AUTHORIZATION_NOT_FOUND')
        grant=(await c.execute(select(grants).where(grants.c.attempt_id==run['attempt_id']))).mappings().first()
        recent=(await c.execute(select(decisions).where(decisions.c.scope==store.scope,decisions.c.run_id==run_id)
            .order_by(decisions.c.created_at.desc(),decisions.c.id).limit(20))).mappings().all()
        return dict(mode='e2ag',reference=reference,subject=row['subject'],actor=row['actor'],policy_id=row['policy_id'],
            state='expired' if row['state']=='active' and row['expires_at']<=now else row['state'],
            expires_at=row['expires_at'],authorization_epoch=row['authorization_epoch'],
            secret_refs=[{'ref':x['ref'],'version':x['version']} for x in row['secret_refs']],
            grant=({k:grant[k] for k in ('attempt_id','session_id','lease_epoch','version','state','expires_at')} | {'state':'expired' if grant['state']=='active' and grant['expires_at']<=now else grant['state']} if grant else None),
            decisions=[dict(id=d['id'],action=d['action'],created_at=d['created_at'],
                outcome=d['decision']['decision'],reason_codes=d['decision']['reason_codes']) for d in recent])
