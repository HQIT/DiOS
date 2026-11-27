"""Whole-tree slot reservations from pinned administrator profiles and claim events."""
import json
from sqlalchemy import select, exists
from .database import RuntimeErrorCode
from .schema import attempts, events, runs
from .child_schema import children
from .budget_tree import subtree


def from_spec(spec):
    """Bounded profile graph; child count is already a whole-tree admission limit."""
    from .worker import read_profile
    from .worker_tool_channel import POLICY_ENV
    from app.services.child_tasks import configuration
    raw = getattr(spec, "environment", {}).get(POLICY_ENV)
    config = json.loads(raw).get('children') if raw else None
    if config is None:
        return None
    configuration(config)
    profiles = {}
    visited = set()

    def visit(value, depth):
        if depth <= 0:
            return
        for entry in value['profiles'].values():
            identity, child = read_profile(entry['path'])
            fingerprint = child.fingerprint()
            if identity != entry['profile_id'] or fingerprint != entry['profile_digest']:
                raise RuntimeErrorCode('PROFILE_CHANGED')
            if identity in profiles and profiles[identity] != fingerprint:
                raise RuntimeErrorCode('CHILD_CAPACITY_PROFILE_CONFLICT')
            profiles[identity] = fingerprint
            if len(profiles) > 64:
                raise RuntimeErrorCode('CHILD_CAPACITY_PROFILE_LIMIT')
            key = (identity, fingerprint, depth)
            if key in visited:
                continue
            visited.add(key)
            policy = getattr(child, "environment", {}).get(POLICY_ENV)
            nested = json.loads(policy).get('children') if policy else None
            if nested:
                configuration(nested)
                visit(nested, min(depth - 1, nested['max_depth']))

    visit(config, config['max_depth'])
    return dict(max_children=config['max_children'], profiles=profiles)


def validate(plan):
    if plan is None:
        return
    if (not isinstance(plan, dict) or set(plan) != {'max_children', 'profiles'}
            or type(plan['max_children']) is not int or not 1 <= plan['max_children'] <= 32
            or not isinstance(plan['profiles'], dict) or not 1 <= len(plan['profiles']) <= 64):
        raise RuntimeErrorCode('CHILD_CAPACITY_PLAN_INVALID')
    from .store import identifier
    for profile, fingerprint in plan['profiles'].items():
        identifier(profile)
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint):
            raise RuntimeErrorCode('CHILD_CAPACITY_PLAN_INVALID')


def is_child(scope):
    return exists(select(children.c.run_id).where(children.c.scope == scope, children.c.run_id == runs.c.id))


async def reservations(c, scope):
    from .capacity import OCCUPIED
    records = (await c.execute(select(runs.c.id, attempts.c.id.label('attempt_id'), events.c.payload)
        .select_from(runs.join(attempts, attempts.c.run_id == runs.c.id).join(events, events.c.run_id == runs.c.id))
        .where(runs.c.scope == scope, attempts.c.state.in_(OCCUPIED), ~is_child(scope),
               events.c.type == 'attempt.claimed',
               events.c.payload['detail']['attempt_id'].as_string() == attempts.c.id))).mappings()
    result = []
    for row in records:
        plan = row['payload']['detail'].get('child_capacity')
        if plan is not None:
            validate(plan)
            result.append(dict(root=row['id'], attempt_id=row['attempt_id'], plan=plan))
    return result


async def reserved_occupancy(c, scope, selector, actual):
    """Actual occupied attempts plus unused slots; children never count twice."""
    from .capacity import matches, OCCUPIED
    from sqlalchemy import func
    extra, owners = 0, []
    for reservation in await reservations(c, scope):
        root, plan = reservation['root'], reservation['plan']
        root_matches = bool((await c.execute(select(runs.c.id).where(
            runs.c.id == root, runs.c.scope == scope, matches(selector, scope)))).first())
        child_matches = root_matches or (selector['kind'] == 'profiles' and bool(set(selector['profiles']) & set(plan['profiles'])))
        if not child_matches:
            continue
        target = int(root_matches) + plan['max_children']
        occupied = (await c.execute(select(func.count()).select_from(attempts.join(runs, attempts.c.run_id == runs.c.id))
            .where(runs.c.scope == scope, runs.c.id.in_(subtree(scope, root)), attempts.c.state.in_(OCCUPIED), matches(selector, scope)))).scalar_one()
        extra += max(0, target - occupied)
        owners.append(root)
    return actual + extra, owners


async def check_child(c, scope, root, run):
    """An altered nested profile cannot consume an unrelated pre-reserved slot."""
    found = next((r for r in await reservations(c, scope) if r['root'] == root), None)
    if found is None:
        return  # Old attempts retain their original admission and visible blocking behavior.
    plan = found['plan']
    if plan['profiles'].get(run['input'].get('runtime_profile')) != run['input'].get('profile_digest'):
        raise RuntimeErrorCode('CHILD_CAPACITY_PROFILE_CHANGED')
    from sqlalchemy import func
    count = (await c.execute(select(func.count()).select_from(children).where(
        children.c.scope == scope, children.c.root_run_id == root))).scalar_one()
    if count >= plan['max_children']:
        raise RuntimeErrorCode('CHILD_LIMIT_EXCEEDED')