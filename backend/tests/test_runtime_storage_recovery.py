"""Storage backoff keeps the daemon alive without swallowing unrelated failures."""
import errno
import unittest
from unittest.mock import AsyncMock, patch
import test_runtime_worker as fixtures


class StorageRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.WorkerTests()
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.worker=self.fixture.worker(fixtures.FakeAdapter())

    async def test_space_probe_blocks_new_claims_until_writable(self):
        worker=self.worker
        worker.recover=AsyncMock(return_value=[])
        calls=[]
        async def once():
            calls.append('claim')
            if len(calls)==1:raise OSError(errno.ENOSPC,'test full')
            worker.stopping=True
            return {'state':'idle'}
        worker.once=once
        original_put=worker.artifacts.put
        probes=[]
        def put(chunks):
            probes.append(True)
            if len(probes)==1:raise OSError(errno.EDQUOT,'test quota')
            return original_put(chunks)
        with patch.object(worker.artifacts,'put',side_effect=put), patch('app.runtime.worker.asyncio.sleep',new_callable=AsyncMock) as sleep:
            result=await worker.serve()
        self.assertEqual(result['state'],'drained')
        self.assertEqual(len(calls),2)
        self.assertEqual(len(probes),2)
        self.assertEqual(worker.recover.await_count,2)
        self.assertEqual([c.args[0] for c in sleep.await_args_list[:2]],[1,2])

    async def test_permission_error_is_not_retried_as_disk_full(self):
        self.worker.recover=AsyncMock(side_effect=PermissionError(errno.EACCES,'denied'))
        self.worker.once=AsyncMock()
        with self.assertRaises(PermissionError):await self.worker.serve()
        self.worker.once.assert_not_awaited()

    async def test_once_retains_actionable_storage_failure(self):
        self.worker.recover=AsyncMock(side_effect=OSError(errno.ENOSPC,'full'))
        with self.assertRaises(OSError) as caught:await self.worker.serve(once=True)
        self.assertEqual(caught.exception.errno,errno.ENOSPC)