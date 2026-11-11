import asyncio
import tempfile
import unittest
from sqlalchemy import text
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.store import RuntimeStore
from app.runtime.write_queue import WriteGate, WriteQueueError


class WriteGateTests(unittest.IsolatedAsyncioTestCase):
    async def queued(self, gate, count):
        for _ in range(100):
            if len(gate.waiters)==count:return
            await asyncio.sleep(0)
        self.fail('waiters did not arrive')

    async def test_fifo_and_full(self):
        gate=WriteGate(3);order=[]
        async def work(i):
            async with gate.slot():order.append(i)
        await gate.acquire()
        tasks=[]
        for i in range(3):
            tasks.append(asyncio.create_task(work(i)));await self.queued(gate,i+1)
        with self.assertRaisesRegex(WriteQueueError,'WRITE_QUEUE_FULL'):await gate.acquire()
        gate.release();await asyncio.gather(*tasks)
        self.assertEqual(order,[0,1,2]);self.assertFalse(gate.active)

    async def test_timeout_removes_waiter(self):
        gate=WriteGate(1,.01);await gate.acquire()
        with self.assertRaisesRegex(WriteQueueError,'WRITE_QUEUE_TIMEOUT'):await gate.acquire()
        self.assertEqual(len(gate.waiters),0);gate.release()
        async with gate.slot():pass

    async def test_cancel_queued_and_cancel_grant(self):
        for granted in [False,True]:
            gate=WriteGate();await gate.acquire()
            task=asyncio.create_task(gate.acquire());await self.queued(gate,1)
            if granted:gate.release()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            if not granted:gate.release()
            async with gate.slot():pass
            self.assertFalse(gate.active);self.assertEqual(len(gate.waiters),0)

    async def test_cancel_active_releases(self):
        gate=WriteGate();entered=asyncio.Event()
        async def work():
            async with gate.slot():entered.set();await asyncio.Event().wait()
        task=asyncio.create_task(work());await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        async with gate.slot():pass

    async def test_drain_completes_admitted_and_rejects_new(self):
        gate=WriteGate();await gate.acquire()
        task=asyncio.create_task(gate.acquire());await self.queued(gate,1)
        drain=asyncio.create_task(gate.drain());await asyncio.sleep(0)
        with self.assertRaisesRegex(WriteQueueError,'WRITE_DRAINING'):await gate.acquire()
        self.assertFalse(drain.done());gate.release();await task
        self.assertFalse(drain.done());gate.release();await drain


class RuntimeWriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=Database('sqlite+aiosqlite:///'+self.temp.name+'/state.db',write_queue_capacity=1,write_queue_timeout=.05)
        await self.db.migrate();self.store=RuntimeStore(self.db,'test')
    async def asyncTearDown(self):await self.db.close();self.temp.cleanup()

    async def test_query_snapshot_does_not_wait_for_writer(self):
        run=await self.store.submit('one',{'中文':'原值'})
        async with self.db.transaction('test') as c:
            await c.execute(text('UPDATE runtime_runs SET version=999 WHERE id=:id'),{'id':run['id']})
            value=await asyncio.wait_for(self.store.get(run['id']),1)
            self.assertEqual(value['version'],run['version'])
        self.assertEqual((await self.store.get(run['id']))['version'],999)

    async def test_read_is_enforced_and_connection_reusable(self):
        with self.assertRaisesRegex(Exception, "readonly"):
            async with self.db.read('test') as c:await c.execute(text('DELETE FROM runtime_runs'))
        await self.store.submit('after',{})
        self.assertEqual(len(await self.store.pending_events('audit')),1)

    async def test_queue_timeout_is_not_persisted(self):
        async with self.db.transaction('test'):
            with self.assertRaisesRegex(RuntimeErrorCode,'WRITE_QUEUE_TIMEOUT'):
                await self.store.submit('retry',{})
        run=await self.store.submit('retry',{})
        replay=await self.store.submit('retry',{})
        self.assertEqual(run['id'],replay['id'])
        self.assertEqual(len(await self.store.pending_events('audit')),1)
