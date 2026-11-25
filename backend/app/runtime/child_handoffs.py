"""A recipient delegation is usable only within its issuing execution tree."""

from sqlalchemy import select

from app.services.e2ag import evaluate_work_permission

from .child_authorizations import row as child_row
from .child_authorizations import validate_binding
from .child_handoff_schema import check, handoffs, require
from .database import RuntimeErrorCode
from .work_auth_schema import work
from .work_policy import permission


async def bind(c, store, reference, proof, source, parent_ref, permissions):
    await require(c)
    binding = proof["parent_execution"]
    validate_binding(binding)
    parent, attempt, _ = await store._owned(c, *binding["owner"], authorize=True)
    if (
        source is None
        or source["principal"] != parent["input"].get("principal")
        or parent_ref != parent["input"].get("delegation_ref")
        or attempt["session_id"] != binding["session_id"]
        or source["profile_id"] != proof["profile_id"]
    ):
        raise RuntimeErrorCode("CHILD_RECIPIENT_BINDING_MISMATCH")
    authority = (
        (
            await c.execute(
                select(work).where(
                    work.c.scope == store.scope,
                    work.c.id == parent["input"]["work_authorization_ref"],
                )
            )
        )
        .mappings()
        .one()
    )
    requested = permission(dict(proof, policy_id=authority["policy_id"]), "spawn")
    for p in [requested, *permissions]:
        if not evaluate_work_permission(authority["permissions"], p).permits_execution:
            raise RuntimeErrorCode("CHILD_PERMISSION_EXPANSION")
    from .delegations import check_work

    await check_work(c, store.db, parent, authority, requested)
    await store._owned(c, *binding["owner"], authorize=True)
    await c.execute(
        handoffs.insert().values(
            delegation_ref=reference,
            scope=store.scope,
            parent_run_id=parent["id"],
            binding=dict(
                proof, parent_authorization_epoch=parent["authorization_epoch"]
            ),
        )
    )


async def required(c, scope, reference):
    await require(c)
    row = (
        (
            await c.execute(
                select(handoffs).where(
                    handoffs.c.scope == scope, handoffs.c.delegation_ref == reference
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeErrorCode("CHILD_HANDOFF_REQUIRED")
    return row["binding"]


async def validate_tree(c, run, chain):
    if not await check(c):
        return
    records = (
        (
            await c.execute(
                select(handoffs).where(
                    handoffs.c.scope == run["scope"],
                    handoffs.c.delegation_ref.in_([link["id"] for link in chain]),
                )
            )
        )
        .mappings()
        .all()
    )
    for record in records:
        current = run["id"]
        proof = record["binding"]
        expected = proof["parent_execution"]
        for _ in range(3):
            relation = await child_row(c, run["scope"], current)
            if relation is None:
                raise RuntimeErrorCode("CHILD_PARENT_REQUIRED")
            if relation["parent_run_id"] == record["parent_run_id"]:
                if (
                    relation["parent_attempt_id"] != expected["owner"][1]
                    or relation["parent_epoch"] != expected["owner"][4]
                    or relation["parent_session_id"] != expected["session_id"]
                    or relation["parent_authorization_epoch"]
                    != proof["parent_authorization_epoch"]
                ):
                    raise RuntimeErrorCode("CHILD_RECIPIENT_BINDING_MISMATCH")
                break
            current = relation["parent_run_id"]
        else:
            raise RuntimeErrorCode("CHILD_PARENT_REQUIRED")
