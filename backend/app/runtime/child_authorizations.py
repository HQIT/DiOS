"""Child admission and parent-close controls in the owning Runtime transaction."""
from sqlalchemy import select, func
from .child_schema import children, check, require
from .database import RuntimeErrorCode, Database
from .schema import runs
from .store import RuntimeStore, TERMINAL, identifier
from .work_auth_schema import work
from .work_policy import permission
from app.services.e2ag import evaluate_work_permission


def validate_binding(binding):
    if not isinstance(binding, dict) or set(binding) != {'owner', 'session_id', 'max_children', 'max_depth'}:
        raise RuntimeErrorCode('CHILD_BINDING_INVALID')
    owner = binding['owner']
    if not isinstance(owner, (list, tuple)) or len(owner) != 5 or type(owner[4]) is not int or owner[4] < 1:
        raise RuntimeErrorCode('CHILD_BINDING_INVALID')
    for value in [*owner[:4], binding['session_id']]: identifier(value)
    if type(binding['max_children']) is not int or not 1 <= binding['max_children'] <= 32:
        raise RuntimeErrorCode('CHILD_LIMIT_INVALID')
    if type(binding['max_depth']) is not int or not 1 <= binding['max_depth'] <= 3:
        raise RuntimeErrorCode('CHILD_DEPTH_INVALID')


async def row(c, scope, run_id):
    return (await c.execute(select(children).where(children.c.scope == scope, children.c.run_id == run_id))).mappings().first()


async def bind(c, store, run, descriptor, binding):
    """No queue entry, authorization or budget reservation survives a failed check."""
    await require(c)
    if descriptor is None: raise RuntimeErrorCode('WORK_AUTHORIZATION_REQUIRED')
    parent, attempt, now = await store._owned(c, *binding['owner'], authorize=True)
    if attempt['session_id'] != binding['session_id']: raise RuntimeErrorCode('SESSION_CHANGED')
    reference = parent['input'].get('work_authorization_ref')
    if not reference: raise RuntimeErrorCode('WORK_AUTHORIZATION_REQUIRED')
    authority = (await c.execute(select(work).where(work.c.id == reference, work.c.scope == store.scope))).mappings().one()
    if authority['subject'] != descriptor['subject']:
        raise RuntimeErrorCode('CHILD_IDENTITY_MISMATCH')
    # Identity changes require an independently authenticated recipient and an
    # immediate delegation signed by this exact parent source. No caller actor claim.
    if (authority['actor'] != descriptor['actor'] or
            parent['input'].get('principal') != run['input'].get('principal')):
        await recipient(c, store.scope, parent, run, descriptor)
    spawn = permission(descriptor, 'spawn')
    if not evaluate_work_permission(authority['permissions'], spawn).permits_execution:
        raise RuntimeErrorCode('CHILD_SPAWN_DENIED')
    effective = [p for p in descriptor['permissions'].get('caller', [])
        if evaluate_work_permission(descriptor['permissions'], p).permits_execution]
    if any(not evaluate_work_permission(authority['permissions'], p).permits_execution for p in effective):
        raise RuntimeErrorCode('CHILD_PERMISSION_EXPANSION')
    from .delegations import check_work, lineage
    await check_work(c, store.db, parent, authority, spawn)
    parent_ref = parent['input'].get('delegation_ref')
    if parent_ref:
        child_ref = descriptor.get('delegation_ref')
        if not child_ref: raise RuntimeErrorCode('DELEGATION_ANCESTOR_REQUIRED')
        chain = await lineage(c, store.scope, child_ref)
        if parent_ref not in {x['id'] for x in chain}: raise RuntimeErrorCode('DELEGATION_ANCESTOR_REQUIRED')
    ancestor = await row(c, store.scope, parent['id'])
    depth = ancestor['depth'] + 1 if ancestor else 1
    root = ancestor['root_run_id'] if ancestor else parent['id']
    prior = (await c.execute(select(func.min(children.c.root_limit), func.min(children.c.depth_limit))
        .where(children.c.scope == store.scope, children.c.root_run_id == root))).one()
    root_limit = min(binding['max_children'], ancestor['root_limit'] if ancestor else 32, prior[0] or 32)
    depth_limit = min(binding['max_depth'], ancestor['depth_limit'] if ancestor else 3, prior[1] or 3)
    if depth > depth_limit: raise RuntimeErrorCode('CHILD_DEPTH_EXCEEDED')
    count = (await c.execute(select(func.count()).select_from(children)
        .where(children.c.scope == store.scope, children.c.root_run_id == root))).scalar_one()
    if count >= root_limit: raise RuntimeErrorCode('CHILD_LIMIT_EXCEEDED')
    await store._owned(c, *binding['owner'], authorize=True)
    await c.execute(children.insert().values(run_id=run['id'], scope=store.scope,
        parent_run_id=parent['id'], parent_attempt_id=attempt['id'], parent_session_id=binding['session_id'],
        parent_epoch=attempt['epoch'], parent_authorization_epoch=parent['authorization_epoch'],
        root_run_id=root, depth=depth, root_limit=root_limit, depth_limit=depth_limit,
        request_key=run['idempotency_key'], created_at=now))
    return dict(descriptor, work_expires_at=min(authority['expires_at'], descriptor.get('work_expires_at', authority['expires_at'])))


async def recipient(c, scope, parent, run, descriptor):
    from .delegations import lineage
    from .delegation_identity_schema import issuers
    source = parent['input'].get('principal')
    target = run['input'].get('principal')
    parent_ref = parent['input'].get('delegation_ref')
    reference = descriptor.get('delegation_ref')
    if not source or not target or not parent_ref or not reference:
        raise RuntimeErrorCode('CHILD_IDENTITY_MISMATCH')
    from .child_handoffs import required
    proof=await required(c,scope,reference)
    expected=run['input']['parent_execution']
    if (proof['parent_execution']['owner']!=expected['owner']
            or proof['parent_execution']['session_id']!=expected['session_id']
            or proof['parent_authorization_epoch']!=parent['authorization_epoch']
            or proof['profile_id']!=descriptor['profile_id']
            or proof['profile_digest']!=descriptor['profile_digest']):
        raise RuntimeErrorCode('CHILD_RECIPIENT_BINDING_MISMATCH')
    chain = await lineage(c, scope, reference)
    leaf = chain[-1]
    issuer = (await c.execute(select(issuers).where(
        issuers.c.scope == scope, issuers.c.delegation_ref == reference
    ))).mappings().first()
    if (leaf['parent_ref'] != parent_ref or leaf['subject_ref'] != descriptor['subject']
            or leaf['actor_ref'] != descriptor['actor'] or issuer is None
            or issuer['principal'] != source or issuer['profile_id'] != descriptor['profile_id']
            or target['subject'] != descriptor['subject'] or target['actor'] != descriptor['actor']):
        raise RuntimeErrorCode('CHILD_RECIPIENT_BINDING_MISMATCH')


async def authorize_result(c, store, parent, child):
    if parent['input'].get('principal') == child['input'].get('principal'):
        return
    rows = (await c.execute(select(work).where(work.c.scope == store.scope,
        work.c.id.in_([parent['input']['work_authorization_ref'], child['input']['work_authorization_ref']])
    ))).mappings().all()
    by_id = {r['id']: r for r in rows}
    authority = by_id[parent['input']['work_authorization_ref']]
    target = by_id[child['input']['work_authorization_ref']]
    requested = permission(target, 'read_result')
    if not evaluate_work_permission(authority['permissions'], requested).permits_execution:
        raise RuntimeErrorCode('CHILD_RESULT_NOT_GRANTED')
    from .delegations import check_work
    await check_work(c, store.db, parent, authority, requested)


async def validate_parent(c, run, now):
    if not run['input'].get('parent_execution'): return
    await require(c)
    binding = await row(c, run['scope'], run['id'])
    if binding is None or not 1 <= binding['depth'] <= 3: raise RuntimeErrorCode('CHILD_BINDING_INVALID')
    parent = (await c.execute(select(runs).where(runs.c.id == binding['parent_run_id'], runs.c.scope == run['scope']))).mappings().first()
    prior = await row(c, run['scope'], binding['parent_run_id'])
    if parent is None or binding['depth'] != (prior['depth'] + 1 if prior else 1):
        raise RuntimeErrorCode('CHILD_BINDING_INVALID')
    owner = run['input']['parent_execution']
    if (owner['owner'][0:2] != [binding['parent_run_id'], binding['parent_attempt_id']]
            or owner['owner'][4] != binding['parent_epoch'] or owner['session_id'] != binding['parent_session_id']):
        raise RuntimeErrorCode('CHILD_BINDING_INVALID')
    if parent['authorization_epoch'] != binding['parent_authorization_epoch']:
        raise RuntimeErrorCode('PARENT_AUTHORIZATION_INVALID')
    try:
        _, attempt, _ = await RuntimeStore(Database, run['scope'])._owned(c, *owner['owner'], authorize=True)
    except RuntimeErrorCode as exc:
        if exc.code in {'STALE_OWNER','AUTH_REVOKED','WORK_AUTHORIZATION_EXPIRED','EXECUTION_GRANT_EXPIRED','EXECUTION_GRANT_REQUIRED'}:
            raise RuntimeErrorCode('PARENT_AUTHORIZATION_INVALID') from None
        raise
    if attempt['session_id'] != binding['parent_session_id']: raise RuntimeErrorCode('SESSION_CHANGED')


async def cancel_children(c, store, parent_id, now):
    if not await check(c): return
    related = (await c.execute(select(runs).join(children, children.c.run_id == runs.c.id)
        .where(children.c.scope == store.scope, runs.c.scope == store.scope,
            children.c.parent_run_id == parent_id, ~runs.c.state.in_(TERMINAL)))).mappings().all()
    for child in related:
        if child['cancel_requested']: continue
        state = 'cancelled' if child['state'] in {'queued', 'retry_wait'} else child['state']
        await store._event(c, dict(child), 'run.parent_cancel_requested', {'parent_run_id':parent_id},
            now, state=state, cancel_requested=True)


async def view(c, scope, run_id, access=None):
    if not await check(c): return None
    parent = await row(c, scope, run_id)
    if parent is not None and access is not None:
        ids={parent['parent_run_id'],parent['root_run_id']}
        visible=set((await c.execute(select(runs.c.id).where(runs.c.scope==scope,runs.c.id.in_(ids),access))).scalars())
        if visible!=ids:parent=None
    related = (await c.execute(select(children.c.run_id, children.c.parent_attempt_id, runs.c.state, runs.c.cancel_requested)
        .join(runs, runs.c.id == children.c.run_id).where(children.c.scope == scope,
            runs.c.scope == scope, children.c.parent_run_id == run_id, access if access is not None else True)
        .order_by(children.c.created_at, children.c.run_id).limit(32))).mappings().all()
    if parent is None and not related: return None
    return {'parent': {k:parent[k] for k in ('parent_run_id','parent_attempt_id','parent_session_id','parent_epoch','root_run_id','depth','root_limit','depth_limit')} if parent else None,
        'children':[dict(x) for x in related], 'parent_close_policy':'request_cancel'}
