"""Server-owned retry policy; serialized circuit probes and E2AG-preserving attempts."""
import json
from sqlalchemy import select,func
from .database import RuntimeErrorCode
from .fault_schema import bindings,circuits,plans
from .schema import runs,attempts

POLICY_ENV='DIOS_RECOVERY_POLICY'
# Wrappers normalize provider errors; stderr text is evidence, never executable policy.
CATEGORIES={
    'PROVIDER_NETWORK_ERROR':'network','PROVIDER_TIMEOUT':'network','PROVIDER_RATE_LIMITED':'rate_limit',
    'PROVIDER_QUOTA_EXHAUSTED':'quota','SESSION_CREATE_FAILED':'session',
    'PROCESS_EXITED':'process','RESULT_FORMAT_INVALID':'format','RESULT_INCOMPLETE':'format',
    'EVIDENCE_INSUFFICIENT':'evidence','RESULT_EXIT_CONFLICT':'evidence','PROCESS_DEADLINE_EXCEEDED':'deadline',
    'AGENT_EXECUTION_REJECTED':'unknown','REVIEW_BRIDGE_FAILED':'unknown',
    'ACP_TOOL_ACTIVITY_UNVERIFIED':'evidence','ACP_TOOL_ACTIVITY_CHANGED':'evidence',
    'TOOL_APPROVAL_DENIED':'authorization',
    'ACP_QUERY_TIMEOUT':'deadline','ACP_PROCESS_EXITED':'process',
    'ACP_TRANSPORT_FAILED':'control_connection','ACP_AUTH_FAILED':'configuration',
    'ACP_REQUEST_FAILED':'unknown','ACP_FRAME_INVALID':'format','ACP_FRAME_TOO_LARGE':'format',
    'WORKER_GATEWAY_OBSERVATION_UNAVAILABLE':'control_connection','WORK_PREFLIGHT_UNAVAILABLE':'configuration',
}
TRANSIENT={'network','rate_limit','quota','session','process','secret_service'}
SHARED={'network','rate_limit','quota','session','secret_service'}

def policy(value):
    if value is None:return None
    keys={'resource','replay_safe','max_attempts','base_delay','max_delay','quota_delay'}
    if not isinstance(value,dict) or not keys.issubset(value) or set(value)-keys-{'accept_complete_on_error'}:raise RuntimeErrorCode('RECOVERY_POLICY_INVALID')
    from .store import identifier
    identifier(value['resource'])
    if type(value['replay_safe']) is not bool:raise RuntimeErrorCode('RECOVERY_POLICY_INVALID')
    bounds={'max_attempts':(1,10),'base_delay':(1,300),'max_delay':(1,3600),'quota_delay':(1,86400)}
    for k,(lo,hi) in bounds.items():
        if type(value[k]) is not int or not lo<=value[k]<=hi:raise RuntimeErrorCode('RECOVERY_POLICY_INVALID')
    if value['max_delay']<value['base_delay']:raise RuntimeErrorCode('RECOVERY_POLICY_INVALID')
    if type(value.get('accept_complete_on_error',False)) is not bool:raise RuntimeErrorCode('RECOVERY_POLICY_INVALID')
    return dict(value,accept_complete_on_error=value.get('accept_complete_on_error',False))

def from_spec(spec):return policy(json.loads(spec.environment[POLICY_ENV])) if POLICY_ENV in spec.environment else None
def category(code):
    if code=='ACP_PREFLIGHT_NETWORK_FAILED':return 'network'
    if code.startswith('ACP_PREFLIGHT_'):return 'configuration'
    if code=='SECRET_PROVIDER_UNAVAILABLE':return 'secret_service'
    if code.startswith('COMPATIBILITY_'):return 'configuration'
    if code.startswith(('AUTH_','WORK_AUTHORIZATION','EXECUTION_GRANT','WORK_POLICY','TOOL_','SECRET_','PACKAGE_ADMISSION','PACKAGE_NOT_ADMITTED')):return 'authorization'
    return CATEGORIES.get(code,'unknown')
def delay(p,count,kind):
    base=p['quota_delay'] if kind=='quota' else p['base_delay']
    maximum=max(p['max_delay'],base)
    return min(maximum,base*2**min(count-1,10))
async def circuit(c,scope,resource):
    return (await c.execute(select(circuits).where(circuits.c.scope==scope,circuits.c.resource==resource))).mappings().first()
async def put_plan(c,row):
    old=(await c.execute(select(plans.c.run_id).where(plans.c.run_id==row['run_id']))).first()
    if old:await c.execute(plans.update().where(plans.c.run_id==row['run_id']).values(**row))
    else:await c.execute(plans.insert().values(**row))

def unlaunched(a):
    return not (a['remote'] or a['process_instance'] or a['process_id'] or a['session_id'])

async def ready(c,store,p,profile,now):
    """Promote bounded due retries; every new claim still requires a fresh E2AG grant."""
    if not p:return True
    rows=(await c.execute(select(runs).join(plans,plans.c.run_id==runs.c.id).where(runs.c.scope==store.scope,
        runs.c.state=='retry_wait',plans.c.state=='waiting',plans.c.retry_at<=now,
        plans.c.resource==p['resource'],runs.c.input['runtime_profile'].as_string()==profile)
        .order_by(plans.c.retry_at,runs.c.id).limit(32))).mappings().all()
    for row in rows:
        run=dict(row);a=(await c.execute(select(attempts).where(attempts.c.id==run['attempt_id']))).mappings().one()
        try:
            reason=(await c.execute(select(plans.c.reason).where(plans.c.run_id==run['id']))).scalar_one()
            preflight=reason=='RETRY_UNLAUNCHED' and a['state']=='failed' and unlaunched(a)
            if not p['replay_safe'] and not preflight:raise RuntimeErrorCode('REPLAY_DISABLED')
            from .work_authorizations import checked_work
            if await checked_work(c,run,a,now,'execute') is None:raise RuntimeErrorCode('WORK_AUTHORIZATION_REQUIRED')
            from .tool_invocations import require_settled
            await require_settled(c,store.scope,run['id'])
        except RuntimeErrorCode as exc:
            await c.execute(plans.update().where(plans.c.run_id==run['id']).values(state='blocked',reason=exc.code,updated_at=now))
            await store._event(c,run,'retry.blocked',{'code':exc.code},now,state='cancelled' if run['cancel_requested'] else 'failed')
            continue
        await c.execute(plans.update().where(plans.c.run_id==run['id']).values(state='released',updated_at=now))
        await store._event(c,run,'retry.ready',{},now,state='queued')
    state=await circuit(c,store.scope,p['resource'])
    return not state or state['state']=='closed' or (state['state']=='open' and state['retry_at']<=now)

async def claimed(c,scope,a,p,now):
    if not p:return
    await c.execute(bindings.insert().values(attempt_id=a['id'],scope=scope,resource=p['resource'],policy=p))
    state=await circuit(c,scope,p['resource'])
    if not state:
        await c.execute(circuits.insert().values(scope=scope,resource=p['resource'],state='closed',failures=0,retry_at=0,updated_at=now))
    elif state['state']=='open':
        await c.execute(circuits.update().where(circuits.c.scope==scope,circuits.c.resource==p['resource'])
            .values(state='half_open',probe_attempt=a['id'],updated_at=now))

async def outcome(c,scope,attempt_id,success,now,code=None):
    b=(await c.execute(select(bindings).where(bindings.c.attempt_id==attempt_id,bindings.c.scope==scope))).mappings().first()
    if not b:return
    state=await circuit(c,scope,b['resource'])
    if not state:return
    where=(circuits.c.scope==scope,circuits.c.resource==b['resource'])
    if success:
        # A late completion from a pre-outage attempt cannot reopen the account floodgate.
        if state['state']=='half_open' and state['probe_attempt']==attempt_id:
            await c.execute(circuits.update().where(*where).values(state='closed',failures=0,retry_at=0,probe_attempt=None,reason=None,updated_at=now))
    elif category(code or '') in SHARED or state['probe_attempt']==attempt_id:
        count=state['failures']+1
        # Keep another live/unknown probe fenced until its own result is reconciled.
        own=state['probe_attempt'] in (None,attempt_id)
        await c.execute(circuits.update().where(*where).values(state='open' if own else 'half_open',
            failures=count,retry_at=max(state['retry_at'],now+delay(b['policy'],count,category(code or ''))),
            probe_attempt=None if own else state['probe_attempt'],reason=code,updated_at=now))

async def failed(c,store,run,a,code,now,*,retry_unlaunched=False,retry_after=0):
    kind=category(code);b=(await c.execute(select(bindings).where(bindings.c.attempt_id==a['id']))).mappings().first()
    await outcome(c,store.scope,a['id'],False,now,code)
    if not b:return 'failed',{'code':code,'category':kind}
    p=b['policy'];reason='NON_RETRYABLE';retry_at=None
    count=(await c.execute(select(func.count()).select_from(attempts).where(attempts.c.run_id==run['id']))).scalar_one()
    if kind in TRANSIENT:
        try:
            preflight=retry_unlaunched and a['state']=='claimed' and unlaunched(a)
            if not p['replay_safe'] and not preflight:raise RuntimeErrorCode('REPLAY_DISABLED')
            if count>=p['max_attempts']:raise RuntimeErrorCode('RETRY_BUDGET_EXHAUSTED')
            from .work_authorizations import checked_work
            if await checked_work(c,run,a,now,'execute') is None:raise RuntimeErrorCode('WORK_AUTHORIZATION_REQUIRED')
            from .tool_invocations import require_settled
            await require_settled(c,store.scope,run['id'])
            retry_at=now+max(delay(p,count,kind),retry_after)
            reason='RETRY_UNLAUNCHED' if preflight else None
        except RuntimeErrorCode as exc:reason=exc.code
    await put_plan(c,dict(run_id=run['id'],scope=store.scope,attempt_id=a['id'],resource=p['resource'],
        state='waiting' if retry_at else 'blocked',category=kind,code=code,retry_at=retry_at,reason=reason,updated_at=now))
    if retry_at:
        from .work_auth_schema import grants
        await c.execute(grants.update().where(grants.c.attempt_id==a['id']).values(state='closed',version=grants.c.version+1))
    return ('retry_wait' if retry_at else 'failed'),dict(code=code,category=kind,retry_at=retry_at,retry_blocked=None if retry_at else reason)

async def view(c,scope,run_id):
    row=(await c.execute(select(plans).where(plans.c.scope==scope,plans.c.run_id==run_id))).mappings().first()
    if not row:return None
    state=await circuit(c,scope,row['resource'])
    return dict(row,circuit=dict(state) if state else None)

async def bound_policy(c,scope,attempt_id):
    return (await c.execute(select(bindings.c.policy).where(bindings.c.scope==scope,bindings.c.attempt_id==attempt_id))).scalar_one_or_none()
