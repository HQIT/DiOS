from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api.os.connectors import create_connector, list_connector_types
from app.connectors import registry
from app.connectors.contracts import CAPABILITY_POLL, ConnectorManifest, EventTypeDecl
from app.connectors.events import make_event
from app.connectors.ownership import LocalPollOwnership
from app.db.database import Base
from app.models.schemas import ConnectorCreate
from app.models.tables import Connector, EventLog
from app.services.connector_poller import ConnectorPoller
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def eventually(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.01)


class ConnectorPollerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root}/events.db")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        registry.reset_for_tests()
        registry.all_manifests()
        self.pollers = []

    async def asyncTearDown(self):
        for poller in self.pollers:
            await poller.stop()
        registry.reset_for_tests()
        await self.engine.dispose()
        self.temp.cleanup()

    def poller(self, **kwargs):
        poller = ConnectorPoller(
            sessions=self.sessions,
            scan_interval=0.01,
            ownership=LocalPollOwnership(self.root / "locks"),
            **kwargs,
        )
        self.pollers.append(poller)
        return poller

    async def instance(self, adapter, *, name="test_poll", interval=60, enabled=True):
        registry.register(
            ConnectorManifest(
                type=name,
                label="Polling test",
                capabilities=(CAPABILITY_POLL,),
                poll_adapter=adapter,
                poll_interval_seconds=interval,
                event_types=(EventTypeDecl("email.received"),),
                accepted_source_patterns=("imap/*",),
            )
        )
        async with self.sessions() as db:
            return await create_connector(
                ConnectorCreate(type=name, name=name, enabled=enabled, config={}), db
            )

    async def test_new_type_runs_through_registry_crud_e2ag_and_dedup(self):
        class Adapter:
            async def poll(_, instance, emit):
                event = make_event(
                    source=f"imap/{instance.id}",
                    event_type="email.received",
                    data={
                        "from": "sender",
                        "subject": "test",
                        "body_preview": "hello",
                        "message_id": "one",
                    },
                )
                await emit(event)
                await emit(event)

        await self.instance(Adapter())
        catalog = await list_connector_types()
        self.assertEqual(
            60,
            next(x for x in catalog if x["type"] == "test_poll")["poll"][
                "interval_seconds"
            ],
        )
        json.dumps(catalog)
        poller = self.poller()
        await poller.start()

        async def delivered():
            async with self.sessions() as db:
                self.logs = (await db.execute(select(EventLog))).scalars().all()
                return len(self.logs) == 1 and len(self.logs[0].audit_chain) >= 3

        await eventually(delivered)
        await poller.stop()
        self.assertEqual("enforce", self.logs[0].contract_decision["mode"])
        self.assertTrue(self.logs[0].trace_id)

    async def test_slow_and_failed_instances_do_not_block_other_instances(self):
        started, release, healthy = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Slow:
            async def poll(_, instance, emit):
                started.set()
                await release.wait()

        class Bad:
            async def poll(_, instance, emit):
                raise OSError("sensitive-server-reply")

        class Good:
            async def poll(_, instance, emit):
                healthy.set()

        await self.instance(Slow(), name="slow")
        await self.instance(Bad(), name="bad")
        await self.instance(Good(), name="good")
        poller = self.poller()
        try:
            with self.assertLogs(
                "app.services.connector_poller", level="WARNING"
            ) as logs:
                await poller.start()
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(healthy.wait(), 2)
                await asyncio.sleep(0.05)
            self.assertNotIn("sensitive-server-reply", "".join(logs.output))
        finally:
            release.set()

    async def test_two_runtimes_start_idempotently_and_hold_cooldown_ownership(self):
        adapter = SimpleNamespace(poll=AsyncMock())
        await self.instance(adapter)
        first, second = self.poller(), self.poller()
        await first.start()
        await first.start()
        await second.start()

        async def called():
            return adapter.poll.await_count == 1

        await eventually(called)
        await asyncio.sleep(0.1)
        self.assertEqual(1, adapter.poll.await_count)

    async def test_drain_keeps_ownership_until_active_adapter_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class Adapter:
            async def poll(_, instance, emit):
                entered.set()
                await release.wait()

        row = await self.instance(Adapter())
        poller = self.poller()
        await poller.start()
        await asyncio.wait_for(entered.wait(), 2)
        stopping = asyncio.create_task(poller.stop())
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(stopping.done())
            async with LocalPollOwnership(self.root / "locks").hold(row.id) as owned:
                self.assertFalse(owned)
        finally:
            release.set()
            await stopping
        async with LocalPollOwnership(self.root / "locks").hold(row.id) as owned:
            self.assertTrue(owned)

    async def test_disabled_instances_and_non_poll_types_are_not_called(self):
        adapter = SimpleNamespace(poll=AsyncMock())
        await self.instance(adapter, enabled=False)
        async with self.sessions() as db:
            db.add(Connector(type="generic", name="webhook", enabled=True, config={}))
            await db.commit()
        poller = self.poller()
        await poller.start()
        await asyncio.sleep(0.08)
        adapter.poll.assert_not_awaited()

    async def test_disable_and_reenable_during_running_round_reloads_configuration(
        self,
    ):
        observed = []

        class Adapter:
            async def poll(_, instance, emit):
                observed.append(instance.config)

        row = await self.instance(Adapter(), interval=0.03)
        poller = self.poller()
        await poller.start()

        async def called():
            return bool(observed)

        await eventually(called)
        async with self.sessions() as db:
            current = await db.get(Connector, row.id)
            current.enabled = False
            await db.commit()
        await asyncio.sleep(0.1)
        count = len(observed)
        await asyncio.sleep(0.06)
        self.assertEqual(count, len(observed))
        async with self.sessions() as db:
            current = await db.get(Connector, row.id)
            current.enabled, current.config = True, {"revision": 2}
            await db.commit()

        async def changed():
            return {"revision": 2} in observed

        await eventually(changed)

    async def test_failed_round_automatically_retries_after_interval(self):
        adapter = SimpleNamespace(poll=AsyncMock())
        adapter.poll.side_effect = [OSError("offline"), None, None]
        await self.instance(adapter, interval=0.05)
        poller = self.poller()
        await poller.start()

        async def retried():
            return adapter.poll.await_count >= 2

        await eventually(retried)

    async def test_unpersisted_duplicate_is_not_acknowledged(self):
        event = make_event(source="imap/test", event_type="email.received", data={})
        with (
            patch(
                "app.services.connector_poller.dispatch_event",
                new=AsyncMock(return_value=(None, "Duplicate of event pending-claim")),
            ),
            self.assertRaisesRegex(RuntimeError, "POLL_EVENT_NOT_PERSISTED"),
        ):
            await self.poller()._emit(event)

    async def test_e2ag_denial_remains_durable_without_dispatch(self):
        event = make_event(
            source="unregistered/source", event_type="agent.root", data={}
        )
        with patch(
            "app.services.event_dispatcher.a2a_service.send_message", new=AsyncMock()
        ) as send:
            await self.poller()._emit(event)
            send.assert_not_awaited()
        async with self.sessions() as db:
            log = await db.scalar(select(EventLog))
            self.assertEqual("denied", log.status)
            self.assertEqual("deny", log.contract_decision["decision"])
            self.assertTrue(log.audit_chain)

    async def test_native_process_lock_released_after_process_death(self):
        code = """
import asyncio, sys
from pathlib import Path
from types import SimpleNamespace
from app.connectors.ownership import LocalPollOwnership
async def main():
    async with LocalPollOwnership(Path(sys.argv[1])).hold('instance') as held:
        print('owned' if held else 'busy', flush=True)
        await asyncio.Event().wait()
asyncio.run(main())
"""
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            str(self.root / "locks"),
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            self.assertEqual(
                b"owned\n", await asyncio.wait_for(child.stdout.readline(), 3)
            )
            async with LocalPollOwnership(self.root / "locks").hold("instance") as held:
                self.assertFalse(held)
        finally:
            child.kill()
            await child.wait()
        async with LocalPollOwnership(self.root / "locks").hold("instance") as held:
            self.assertTrue(held)

    async def test_invalid_poll_contracts_are_rejected(self):
        for kwargs in (
            {},
            {
                "poll_adapter": SimpleNamespace(poll=AsyncMock()),
                "poll_interval_seconds": 0,
            },
            {
                "poll_adapter": SimpleNamespace(poll=AsyncMock()),
                "poll_interval_seconds": float("nan"),
            },
        ):
            with self.assertRaises(ValueError):
                registry.register(
                    ConnectorManifest(
                        type="bad",
                        label="Bad",
                        capabilities=(CAPABILITY_POLL,),
                        **kwargs,
                    )
                )


if __name__ == "__main__":
    unittest.main()
