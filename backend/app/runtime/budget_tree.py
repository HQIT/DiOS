"""Budget ancestry comes exclusively from admitted, scope-bound child relations."""

from sqlalchemy import select

from .child_schema import children
from .database import RuntimeErrorCode
from .schema import runs


def descendants(scope, seeds):
    """Include each seed once, even when matching roots overlap."""
    tree = seeds.cte(recursive=True)
    tree = tree.union(
        select(children.c.run_id.label("id"))
        .join(tree, children.c.parent_run_id == tree.c.id)
        .where(children.c.scope == scope)
    )
    return select(tree.c.id)


def subtree(scope, run_id):
    return descendants(
        scope, select(runs.c.id).where(runs.c.scope == scope, runs.c.id == run_id)
    )


async def ancestors(c, scope, run_id):
    result = []
    current = run_id
    for _ in range(4):
        parent = (
            await c.execute(
                select(children.c.parent_run_id).where(
                    children.c.scope == scope, children.c.run_id == current
                )
            )
        ).scalar_one_or_none()
        if parent is None:
            return result
        if parent == run_id or parent in result or len(result) == 3:
            raise RuntimeErrorCode("CHILD_BINDING_INVALID")
        result.append(parent)
        current = parent
    raise RuntimeErrorCode("CHILD_BINDING_INVALID")
