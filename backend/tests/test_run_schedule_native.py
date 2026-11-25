"""Opt-in independent processes, durable receipts and Docker Worker qualification."""

import asyncio
import json
import os
import sys
import time
import unittest

import test_runtime_task_console as fixtures
from app.runtime.schedule_schema import schedules
from app.runtime.work_auth_schema import work
from app.runtime.worker import read_profile
from app.services.run_schedules import RunSchedules
from sqlalchemy import select
from work_auth_fixtures import policy_fixture

TICK = """
import asyncio, sys
from app.runtime.database import Database
from app.runtime.work_policy import FileWorkPolicy
from app.services.task_console import TaskConsole
from app.services.run_schedules import RunSchedules
async def main():
    db=Database(sys.argv[1])
    try:
        console=TaskConsole(db,'one',sys.argv[2],sys.argv[3],policy=FileWorkPolicy(sys.argv[4]))
        await RunSchedules(console).tick()
    finally: await db.close()
asyncio.run(main())
"""


@unittest.skipUnless(
    os.environ.get("DIOS_RUN_SCHEDULE_NATIVE") == "1",
    "explicit native Docker qualification required",
)
class NativeSchedules(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TaskConsoleTests.asyncSetUp(self)
        value = fixtures.sample("first-item")
        result = fixtures.review_result(
            value,
            {
                "summary": "边界检查",
                "findings": [
                    {
                        "title": "空列表下标越界",
                        "line_start": 2,
                        "line_end": 2,
                        "condition": "items 为空列表",
                        "evidence": "items[0]",
                        "impact": "抛出 IndexError",
                    }
                ],
            },
        )
        rows = [
            {"type": "session", "session_id": "native-schedule"},
            {"type": "output", "text": "中文证据🙂"},
            {"type": "result", "session_id": "native-schedule", "result": result},
        ]
        code = (
            "import json,time\nfor row in "
            + repr(rows)
            + ":\n print(json.dumps(row,ensure_ascii=False),flush=True)\n time.sleep(.1)"
        )
        self.profile["launch"] = {
            "image": os.environ.get(
                "DIOS_RUN_SCHEDULE_IMAGE", "dios-backend:cron-20261124"
            ),
            "command": ["python", "-u", "-c", code],
        }
        self.profile["governance"] = {"mode": "e2ag"}
        (self.profiles / "review.json").write_text(json.dumps(self.profile))
        _, spec = read_profile(self.profiles / "review.json")
        self.policy, _ = policy_fixture(self.root, "review", spec)
        self.console.policy = self.policy
        self.manager = RunSchedules(self.console)

    async def asyncTearDown(self):
        await fixtures.TaskConsoleTests.asyncTearDown(self)

    async def process(self, *args):
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(child.communicate(), 60)
        except BaseException:
            if child.returncode is None:
                child.kill()
                await child.wait()
            raise
        self.assertEqual(child.returncode, 0, err.decode())
        return out

    async def test_process_race_native_worker_restart_and_new_work_identity(self):
        row = await self.manager.create(
            {
                "profile_id": "review",
                "sample_id": "first-item",
                "input_payload": None,
                "goal": "native scheduled review",
            },
            "* * * * *",
            "native-timer",
        )
        async with self.db.transaction("one") as c:
            await c.execute(
                schedules.update()
                .where(schedules.c.id == row["id"])
                .values(next_due=time.time() - 1)
            )
        args = (
            "-c",
            TICK,
            "sqlite+aiosqlite:///" + str(self.root / "runtime.sqlite"),
            str(self.profiles),
            str(self.root / "objects"),
            str(self.root / "work-policy.json"),
        )
        await asyncio.gather(*(self.process(*args) for _ in range(3)))
        first_occurrence = (await self.manager.list())["items"][0]["active"]
        first = first_occurrence["run_id"]
        self.assertEqual(len((await self.console.list())["items"]), 1)
        await self.process(
            "-m",
            "app.runtime.cli",
            "--database-url",
            args[2],
            "--scope",
            "one",
            "worker",
            "--profile",
            str(self.profiles / "review.json"),
            "--artifacts",
            str(self.root / "objects"),
            "--policy-file",
            str(self.root / "work-policy.json"),
            "--once",
            "--timeout",
            "30",
        )
        detail = await self.console.detail(first)
        self.assertEqual(detail["state"], "succeeded")
        self.assertEqual(len(detail["attempts"]), 1)
        self.assertEqual(detail["attempts"][0]["output_excerpt"], "中文证据🙂")
        self.assertTrue(detail["artifacts"])
        # Another control process observes completion; it never starts a Worker itself.
        await self.process(*args)
        row = (await self.manager.list())["items"][0]
        history = (await self.manager.history(row["id"]))["items"]
        self.assertEqual(
            next(item for item in history if item["run_id"] == first)["state"], "succeeded"
        )
        # The next occurrence obtains a distinct bounded Work, not renewal of the old one.
        if row["active"]:
            # A real minute boundary may have arrived while the Docker Worker ran.
            self.assertGreater(row["active"]["first_due"], first_occurrence["last_due"])
        else:
            async with self.db.transaction("one") as c:
                await c.execute(
                    schedules.update()
                    .where(schedules.c.id == row["id"])
                    .values(next_due=time.time() - 1)
                )
            await self.process(*args)
        second = (await self.manager.list())["items"][0]["active"]["run_id"]
        self.assertNotEqual(first, second)
        async with self.db.read("one") as c:
            records = (await c.execute(select(work))).mappings().all()
        self.assertEqual(len(records), 2)
        self.assertEqual(len({x["id"] for x in records}), 2)
        pending = await self.console.detail(second)
        await self.console.store.cancel(second, pending["version"])


if __name__ == "__main__":
    unittest.main()
