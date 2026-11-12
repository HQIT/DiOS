"""E2AG work-authorization seam; trusted local compatibility is explicit.

The verifier receives references, never credentials. Delivery/reconciliation cannot
renew an execution grant. #34 supplies the policy-backed verifier and renewal gate.
"""
from dataclasses import dataclass
from .database import RuntimeErrorCode


@dataclass(frozen=True)
class WorkAuthorizationContext:
    scope: str
    run_id: str
    attempt_id: str
    worker_id: str
    worker_instance: str
    lease_epoch: int
    authorization_epoch: int
    session_id: str | None
    authorization_ref: str | None


async def authorize_work(store, run, attempt, action, *, session_id=None, verifier=None):
    if action not in {'execute', 'accept_result'}:
        raise RuntimeErrorCode('INVALID_AUTHORIZATION_ACTION')
    if run['revoked'] or run['cancel_requested'] or run['authorization_epoch'] != attempt['authorization_epoch']:
        raise RuntimeErrorCode('AUTH_REVOKED')
    ref = run['input'].get('work_authorization_ref')
    if ref is not None and (not isinstance(ref, str) or not ref or len(ref) > 160):
        raise RuntimeErrorCode('INVALID_WORK_AUTHORIZATION_REFERENCE')
    if verifier is None:
        if ref is not None: raise RuntimeErrorCode('WORK_AUTHORIZATION_VERIFIER_REQUIRED')
        return  # Existing explicitly trusted local profile.
    context = WorkAuthorizationContext(store.scope, run['id'], attempt['id'],
        attempt['worker_id'], attempt['worker_instance'], attempt['epoch'],
        attempt['authorization_epoch'], session_id, ref)
    if await verifier(context, action) is not True:
        raise RuntimeErrorCode('WORK_AUTHORIZATION_DENIED')
