import asyncio
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from storage_runtime import pool_options


class QueuedStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + str(Path(self.directory.name)/'state.db'), **pool_options('queued'))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as db:
            await db.execute(text('CREATE TABLE counter (n INTEGER NOT NULL)'))
            await db.execute(text('INSERT INTO counter VALUES (0)'))

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.directory.cleanup()

    async def test_32_read_modify_write_transactions_keep_all_updates(self):
        async def change():
            async with self.sessions() as db:
                n = (await db.execute(text('SELECT n FROM counter'))).scalar_one()
                await asyncio.sleep(.001)
                await db.execute(text('UPDATE counter SET n=:n'), {'n':n+1})
                await db.commit()
        await asyncio.gather(*(change() for _ in range(32)))
        async with self.sessions() as db:
            self.assertEqual(32, (await db.execute(text('SELECT n FROM counter'))).scalar_one())

    async def test_cancelled_waiter_does_not_leak_queue_capacity(self):
        async with self.sessions() as holder:
            await holder.execute(text('SELECT n FROM counter'))
            async def wait():
                async with self.sessions() as db:
                    await db.execute(text('SELECT n FROM counter'))
            task = asyncio.create_task(wait())
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await holder.rollback()
        await asyncio.wait_for(wait(), timeout=2)

    async def test_rollback_releases_connection_and_discards_write(self):
        async with self.sessions() as db:
            await db.execute(text('UPDATE counter SET n=9'))
            await db.rollback()
        async with self.sessions() as db:
            self.assertEqual(0, (await db.execute(text('SELECT n FROM counter'))).scalar_one())

    async def test_committed_session_does_not_block_other_work_during_tool_wait(self):
        async with self.sessions() as first:
            await first.execute(text('UPDATE counter SET n=1'))
            await first.commit()
            self.assertFalse(first.in_transaction())
            async def other():
                async with self.sessions() as db:
                    await db.execute(text('UPDATE counter SET n=2'))
                    await db.commit()
            await asyncio.wait_for(other(), timeout=2)
            self.assertEqual(2, (await first.execute(text('SELECT n FROM counter'))).scalar_one())

    def test_mode_options_and_unknown_mode(self):
        self.assertEqual(0, pool_options('queued')['max_overflow'])
        self.assertEqual(1, pool_options('queued')['pool_size'])
        self.assertEqual(30, pool_options('queued')['pool_timeout'])
        self.assertEqual(10, pool_options('parallel')['max_overflow'])
        with self.assertRaises(ValueError):
            pool_options('unknown')


if __name__ == '__main__':
    unittest.main()
