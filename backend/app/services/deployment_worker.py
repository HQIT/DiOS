"""Bounded, repeatable deployment reconciliation; execution stays in Workers."""

import asyncio
import logging

from sqlalchemy import select

from app.runtime.deployment_schema import deployments, require
from app.runtime.deployments import Deployments

logger = logging.getLogger(__name__)


class DeploymentLoop:
    def __init__(self, console):
        self.manager = Deployments(console.db, console.scope, console.policy)
        self.task = None
        self.cursor = ""

    async def tick(self):
        async with self.manager.db.read(self.manager.scope) as c:
            await require(c)
            ids = (
                (
                    await c.execute(
                        select(deployments.c.id)
                        .where(
                            deployments.c.scope == self.manager.scope,
                            deployments.c.state.in_(["pending", "draining"]),
                            deployments.c.id > self.cursor,
                        )
                        .order_by(deployments.c.id)
                        .limit(32)
                    )
                )
                .scalars()
                .all()
            )
        self.cursor = ids[-1] if len(ids) == 32 else ""
        for deployment_id in ids:
            await self.manager.reconcile(deployment_id)

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Deployment reconciliation failed")
            await asyncio.sleep(2)

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
