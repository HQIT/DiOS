"""ASGI/AnyIO cancellation must release database connections, locks and writer slots."""
import asyncio
import logging
import os
from pathlib import Path
import tempfile
import threading
import unittest
import anyio
from sqlalchemy import text
from app.runtime.database import Database


class Errors(logging.Handler):
    def __init__(self):super().__init__(logging.ERROR);self.records=[]
    def emit(self,record):self.records.append(record.getMessage())


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=Database(getattr(self,'database_url',None) or 'sqlite+aiosqlite:///'+str(Path(self.temp.name)/'state.sqlite'))
        await self.db.migrate()
        async with self.db.transaction('test') as c:await c.execute(text('CREATE TABLE cancel_probe (value INTEGER)'))
        self.errors=Errors();logging.getLogger('sqlalchemy.pool').addHandler(self.errors)
    async def asyncTearDown(self):
        async with self.db.transaction('test') as c:await c.execute(text('DROP TABLE cancel_probe'))
        await self.db.close();logging.getLogger('sqlalchemy.pool').removeHandler(self.errors);self.temp.cleanup()
    async def writable(self):
        async with asyncio.timeout(3):
            async with self.db.transaction('test') as c:
                await c.execute(text('INSERT INTO cancel_probe VALUES (1)'))
        self.assertEqual(self.db.engine.pool.checkedout(),0)
        self.assertEqual(self.errors.records,[])

    async def test_cancelled_read_releases_connection_and_lock(self):
        with anyio.CancelScope() as scope:
            async with self.db.read('test') as c:
                await c.execute(text('SELECT * FROM cancel_probe'))
                scope.cancel();await anyio.sleep(0)
            self.fail('cancellation swallowed')
        self.assertTrue(scope.cancelled_caught)
        await self.writable()

    async def test_cancelled_write_rolls_back_and_releases_slot(self):
        with anyio.CancelScope() as scope:
            async with self.db.transaction('test') as c:
                await c.execute(text('INSERT INTO cancel_probe VALUES (99)'))
                scope.cancel();await anyio.sleep(0)
            self.fail('cancellation swallowed')
        self.assertTrue(scope.cancelled_caught)
        async with self.db.read('test') as c:self.assertEqual((await c.execute(text('SELECT count(*) FROM cancel_probe'))).scalar_one(),0)
        await self.writable()

    async def test_cancelled_driver_operation_finishes_before_invalidation(self):
        entered=threading.Event();release=threading.Event()
        def wait():
            entered.set()
            if not release.wait(3):raise RuntimeError('fixture release timeout')
            return 1
        async with self.db.engine.connect() as c:
            await c.run_sync(lambda sync:sync.connection.create_function('qualification_wait',0,wait))
        async def read():
            async with self.db.read('test') as c:await c.execute(text('SELECT qualification_wait()'))
        async def release_later():
            await asyncio.sleep(.05);release.set()
        releaser=None
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(read)
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                releaser=asyncio.create_task(release_later())
                group.cancel_scope.cancel()
            if releaser:await releaser
        finally:release.set()
        await self.writable()

@unittest.skipUnless(os.environ.get('DIOS_TEST_POSTGRES_URL'),'isolated PostgreSQL required')
class PostgresCancellationTests(unittest.IsolatedAsyncioTestCase):
    database_url=os.environ.get('DIOS_TEST_POSTGRES_URL')
    asyncSetUp=CancellationTests.asyncSetUp
    asyncTearDown=CancellationTests.asyncTearDown
    writable=CancellationTests.writable
    test_cancelled_read_releases_connection_and_lock=CancellationTests.test_cancelled_read_releases_connection_and_lock
    test_cancelled_write_rolls_back_and_releases_slot=CancellationTests.test_cancelled_write_rolls_back_and_releases_slot

    async def test_cancelled_postgres_query_releases_connection(self):
        entered=asyncio.Event()
        async def read():
            async with self.db.read('test') as c:
                entered.set()
                await c.execute(text('SELECT pg_sleep(0.3)'))
        async with anyio.create_task_group() as group:
            group.start_soon(read)
            await entered.wait();await asyncio.sleep(.03)
            group.cancel_scope.cancel()
        await self.writable()
