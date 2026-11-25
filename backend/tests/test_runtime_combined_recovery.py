"""Combined durable-control faults with isolated SQLite and separate Worker objects."""
import json
import unittest
from unittest.mock import AsyncMock, patch

import test_runtime_remote_control as fixtures
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database
from app.runtime.remote_control import RemoteControl
from app.runtime.reporting import relay_reports
from app.runtime.schema import artifacts, attempts
from app.runtime.store import RuntimeStore
from app.runtime.worker import Worker
from sqlalchemy import select


class CombinedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RemoteControlTests.asyncSetUp
    client = fixtures.RemoteControlTests.client
    submit = fixtures.RemoteControlTests.submit
    write_machine = fixtures.RemoteControlTests.write_machine

    async def interrupted_upload(self, *, complete=True):
        rows = [{'type':'session','session_id':'fixture-session'}]
        rows += [{'type':'output','text':'中文🙂'*200} for _ in range(50)]
        if complete:
            rows.append({'type':'result','session_id':'fixture-session',
                         'result':{'summary':'合成结果','evidence':['fixture:source']}})
        data = b''.join((json.dumps(row,ensure_ascii=False)+'\n').encode() for row in rows)
        self.assertGreater(len(data),65536)
        self.adapter.data = data
        original = self.remote.transport.exchange
        disconnected = False

        async def exchange(envelope):
            nonlocal disconnected
            if disconnected:
                raise ConnectionError('isolated control outage')
            result = await original(envelope)
            if envelope['message']['method'] == 'runtime.upload.chunk':
                disconnected = True
                # The first chunk is durable, but its acknowledgement is lost.
                raise ConnectionError('lost durable chunk acknowledgement')
            return result

        self.remote.transport.exchange = exchange
        with self.assertRaises(ConnectionError):
            await self.worker.once()
        self.assertTrue(disconnected)
        prior = await self.f.store.get(self.run['id'])
        self.assertEqual(self.adapter.started,1)
        await self.f.db.close()
        self.f.db = Database(self.f.url)
        self.f.store = RuntimeStore(self.f.db,self.f.scope)
        self.server = RemoteControl(self.f.store,self.server.identities,self.profiles,self.policy,self.f.provider)
        async with self.f.db.transaction(self.f.scope) as connection:
            await connection.execute(attempts.update().where(attempts.c.id==prior['attempt_id']).values(lease_until=0))
        remote = self.client()
        provider = LocalArtifacts(self.client_provider.root)
        worker = Worker(remote,self.adapter,'test-profile',self.f.spec,provider,
                        authorization_verifier=remote.verifier,poll_interval=.05)
        return prior,remote,worker,data

    async def test_chunk_ack_loss_restart_observation_timeout_and_stale_reports(self):
        prior,remote,worker,data = await self.interrupted_upload()
        with patch.object(self.adapter,'observe',AsyncMock(side_effect=TimeoutError('query only'))), patch.object(self.adapter,'stop',wraps=self.adapter.stop) as stop:
            first = await worker.recover()
            stop.assert_not_called()
        self.assertEqual(first[0]['error'],'OBSERVATION_UNAVAILABLE')
        self.assertEqual(self.adapter.started,1)
        result = await worker.recover()
        self.assertEqual(result[0]['state'],'succeeded')
        final = await self.f.store.get(self.run['id'])
        self.assertEqual(final['attempt_id'],prior['attempt_id'])
        self.assertEqual(len(final['attempts']),1)
        self.assertEqual(final['attempts'][0]['session_id'],'fixture-session')
        async with self.f.db.read(self.f.scope) as connection:
            linked = (await connection.execute(select(artifacts).where(artifacts.c.run_id==self.run['id']))).mappings().all()
        self.assertEqual(len(linked),3)
        contents = [b''.join(self.f.provider.read(item['storage_key'])) for item in linked]
        self.assertIn(data,contents)
        attempt=prior['attempts'][0]
        binding=(prior['id'],attempt['id'],attempt['worker_id'],attempt['worker_instance'],attempt['epoch'])
        worker.spool.enqueue(self.f.scope,*binding,999,'streaming',session_id='fixture-session')
        await relay_reports(remote,worker.spool)
        self.assertEqual(worker.spool.status()['errors']['STALE_OWNER'],1)
        self.assertEqual((await self.f.store.get(self.run['id']))['version'],final['version'])
        self.assertEqual(await worker.recover(),[])
        self.assertEqual(self.adapter.started,1)

    async def test_revoked_work_after_chunk_loss_never_accepts_retained_result(self):
        prior,_,worker,_ = await self.interrupted_upload()
        current=await self.f.store.get(self.run['id'])
        await self.f.store.revoke(current['id'],current['version'])
        await worker.recover()
        final=await self.f.store.get(self.run['id'])
        self.assertEqual(final['state'],'failed')
        self.assertEqual(final['attempt_id'],prior['attempt_id'])
        self.assertEqual(final['attempts'][0]['error']['code'],'AUTH_REVOKED')
        async with self.f.db.read(self.f.scope) as connection:
            retained=(await connection.execute(select(artifacts.c.storage_key).where(artifacts.c.run_id==self.run['id']))).scalars().all()
        self.assertEqual(len(retained),2)  # stdout/stderr only; no accepted result object
        self.assertEqual(self.adapter.started,1)

    async def test_incomplete_output_after_chunk_loss_remains_failed(self):
        prior,_,worker,_ = await self.interrupted_upload(complete=False)
        result=await worker.recover()
        self.assertEqual(result[0]['state'],'failed')
        final=await self.f.store.get(self.run['id'])
        self.assertEqual(final['attempt_id'],prior['attempt_id'])
        self.assertEqual(final['attempts'][0]['error']['code'],'RESULT_INCOMPLETE')
        self.assertEqual(self.adapter.started,1)
