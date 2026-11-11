import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from sqlalchemy import select, text
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.store import RuntimeStore, digest
from app.runtime.artifacts import LocalArtifacts
from app.runtime.schema import runs, attempts, events, versions, artifacts, imports

class PersistenceScenarios:
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.url = os.environ['DIOS_TEST_POSTGRES_URL'] if self.postgres else 'sqlite+aiosqlite:///'+self.temp.name+'/state.db'
        self.db = Database(self.url)
        await self.db.migrate()
        self.store = RuntimeStore(self.db, str(uuid4()))
        self.provider = LocalArtifacts(Path(self.temp.name)/'objects')

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def worker(self, slots=1):
        wid, instance = str(uuid4()), str(uuid4())
        await self.store.register_worker(wid, instance, 'test-host', os.getpid(),slots)
        return wid,instance

    async def owned(self):
        r = await self.store.submit('one',{'snapshot':'sha:example'})
        w = await self.worker()
        a = await self.store.claim(*w)
        return r,a,(r['id'],a['id'],*w,a['epoch'])

    async def validating(self):
        r,a,b = await self.owned()
        for n,phase in enumerate(['preparing','creating_session','waiting_model','validating'],1):
            await self.store.report(*b,n,phase,session_id='session-'+a['id'] if n>=3 else None)
        return r,a,b

    async def test_migration_is_idempotent_and_restart_preserves_run(self):
        r = await self.store.submit('restart',{'中文':'保留'})
        self.assertFalse((await self.db.migrate())['changed'])
        await self.db.close()
        self.db = Database(self.url)
        self.store = RuntimeStore(self.db,self.store.scope)
        self.assertEqual((await self.store.get(r['id']))['input'],{'中文':'保留'})

    async def test_future_schema_and_checksum_rejected_without_changes(self):
        for values,code in [({'version':999},'SCHEMA_INCOMPATIBLE'),({'checksum':'tampered'},'MIGRATION_CHECKSUM_MISMATCH')]:
            with self.assertRaisesRegex(RuntimeErrorCode,code):
                async with self.db.transaction(self.store.scope) as c:
                    await c.execute(versions.update().values(**values))
                    await self.db.check_schema(c)
            self.assertFalse((await self.db.migrate())['changed'])

    async def test_concurrent_submission_has_one_identity_and_conflicts_reject(self):
        values = await asyncio.gather(*(self.store.submit('same',{'x':1}) for _ in range(8)))
        self.assertEqual(len({x['id'] for x in values}),1)
        self.assertEqual(len(await self.store.pending_events('observer')),1)
        with self.assertRaisesRegex(RuntimeErrorCode,'IDEMPOTENCY_CONFLICT'):
            await self.store.submit('same',{'x':2})

    async def test_transaction_rolls_back_state_and_outbox_together(self):
        r = await self.store.submit('atomic',{})
        with self.assertRaisesRegex(RuntimeError,'injected'):
            async with self.db.transaction(self.store.scope) as c:
                await self.store._event(c,r,'test',{},await self.db.now(c),state='failed')
                raise RuntimeError('injected crash before commit')
        self.assertEqual((await self.store.get(r['id']))['state'],'queued')
        self.assertEqual(len(await self.store.pending_events('observer')),1)

    async def test_competing_claims_and_capacity(self):
        for n in range(8):await self.store.submit(str(n),{})
        ws = [await self.worker() for _ in range(8)]
        claimed = await asyncio.gather(*(self.store.claim(*w) for w in ws))
        self.assertEqual(len({a['run_id'] for a in claimed}),8)
        self.assertTrue(all(a['epoch']==1 for a in claimed))
        self.assertIsNone(await self.store.claim(*ws[0]))

    async def test_expired_owner_cannot_write_and_query_does_not_requeue(self):
        r,a,b = await self.owned()
        async with self.db.transaction(self.store.scope) as c:
            await c.execute(attempts.update().where(attempts.c.id==a['id']).values(lease_until=0))
        self.assertEqual((await self.store.get(r['id']))['state'],'executing')
        with self.assertRaisesRegex(RuntimeErrorCode,'STALE_OWNER'):await self.store.heartbeat(*b)
        self.assertEqual(await self.store.expire(),1)
        current = await self.store.get(r['id'])
        self.assertEqual(current['state'],'reconciliation_required')
        self.assertIsNone(await self.store.claim(*(await self.worker())))
        await self.store.reconcile_absent(r['id'],current['version'],'fixture-process-exit-and-no-effect')
        current = await self.store.get(r['id'])
        await self.store.retry(r['id'],current['version'])
        newer = await self.store.claim(*(await self.worker()))
        self.assertGreater(newer['epoch'],a['epoch'])
        with self.assertRaisesRegex(RuntimeErrorCode,'STALE_OWNER'):await self.store.report(*b,1,'preparing')

    async def test_report_dedup_conflict_and_out_of_order(self):
        r,a,b = await self.owned()
        self.assertTrue(await self.store.report(*b,1,'preparing'))
        self.assertFalse(await self.store.report(*b,1,'preparing'))
        with self.assertRaisesRegex(RuntimeErrorCode,'REPORT_CONFLICT'):
            await self.store.report(*b,1,'preparing',output=True)
        await self.store.report(*b,2,'creating_session',remote={'endpoint_ref':'ep-1','protocol_version':None,'task_id':'remote-1','cursor':'cursor-1','credential_ref':'secret-ref-1'})
        self.assertFalse(await self.store.report(*b,1,'preparing'))
        got = await self.store.get(r['id'])
        self.assertEqual(got['attempts'][0]['remote']['cursor'],'cursor-1')
        self.assertIsNone(got['attempts'][0]['remote']['protocol_version'])

    async def test_phase_regression_and_session_change_reject(self):
        r,a,b = await self.validating()
        with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_TRANSITION'):await self.store.report(*b,5,'preparing')
        with self.assertRaisesRegex(RuntimeErrorCode,'SESSION_CHANGED'):await self.store.report(*b,5,'validating',session_id='different')

    async def test_revocation_fences_subsequent_work(self):
        r,a,b = await self.owned()
        current = await self.store.get(r['id'])
        await self.store.revoke(r['id'],current['version'])
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.store.report(*b,1,'preparing')
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.store.heartbeat(*b)

    async def test_drain_persists_and_active_attempt_completes(self):
        r,a,b = await self.validating()
        await self.store.drain(a['worker_id'])
        await self.store.register_worker(a['worker_id'],a['worker_instance'],'test-host',os.getpid())
        await self.store.submit('next',{})
        self.assertIsNone(await self.store.claim(a['worker_id'],a['worker_instance']))
        blob = self.provider.put([b'{"complete":true}'])
        await self.store.complete(*b,self.provider,blob.key,lambda chunks: json.loads(b''.join(chunks))['complete'])
        self.assertEqual((await self.store.get(r['id']))['state'],'succeeded')

    async def test_invalid_and_missing_artifact_never_succeed(self):
        r,a,b = await self.validating()
        blob = self.provider.put([b'partial'])
        with self.assertRaisesRegex(RuntimeErrorCode,'EVIDENCE_INSUFFICIENT'):
            await self.store.complete(*b,self.provider,blob.key,lambda chunks:False)
        with self.assertRaises(FileNotFoundError):
            await self.store.complete(*b,self.provider,'0'*64,lambda chunks:True)
        self.assertEqual((await self.store.get(r['id']))['state'],'validating')
        self.assertGreater(len(await self.store.pending_events('progress')),0)

    async def test_cancel_request_is_not_confirmation(self):
        r,a,b = await self.owned()
        current = await self.store.get(r['id'])
        pending = await self.store.cancel(r['id'],current['version'])
        self.assertEqual(pending['state'],'executing')
        await self.store.confirm_cancel(*b,'fixture-termination-confirmed')
        self.assertEqual((await self.store.get(r['id']))['state'],'cancelled')

    async def test_pending_cancel_and_scope_isolation(self):
        r = await self.store.submit('pending',{})
        other = RuntimeStore(self.db,'other-'+self.store.scope)
        with self.assertRaisesRegex(RuntimeErrorCode,'NOT_FOUND'):await other.get(r['id'])
        cancelled = await self.store.cancel(r['id'],r['version'])
        self.assertEqual(cancelled['state'],'cancelled')
        self.assertIsNone(await self.store.claim(*(await self.worker())))

    async def test_outbox_retry_ack_and_stale_projection(self):
        r,a,b = await self.owned()
        pending = sorted(await self.store.pending_events('progress'),key=lambda e:e['version'])
        self.assertEqual(len(await self.store.pending_events('progress')),2)
        self.assertTrue(await self.store.apply_projection('dashboard',pending[-1]))
        self.assertFalse(await self.store.apply_projection('dashboard',pending[0]))
        self.assertFalse(await self.store.apply_projection('dashboard',pending[-1]))
        for event in pending:
            await self.store.acknowledge('progress',event['id'],event['digest'])
            await self.store.acknowledge('progress',event['id'],event['digest'])
        self.assertEqual(await self.store.pending_events('progress'),[])
        self.assertEqual(len(await self.store.pending_events('independent')),2)
        bad = dict(pending[-1],payload={'run_id':r['id'],'version':pending[-1]['version'],'state':'forged'})
        with self.assertRaisesRegex(RuntimeErrorCode,'EVENT_MISMATCH'):await self.store.apply_projection('dashboard',bad)
        bad['digest']=digest(bad['payload'])
        with self.assertRaisesRegex(RuntimeErrorCode,'EVENT_CONFLICT'):await self.store.apply_projection('dashboard',bad)

    async def test_import_preserves_unknowns_links_and_no_execution(self):
        record = {'task':'old-task','attempt':'old-attempt','session':'old-session','artifact':'old-blob','status':'success','protocol_version':None,'capabilities':None}
        first = await self.store.import_history('legacy-host','attempt','old-attempt',record)
        second = await self.store.import_history('legacy-host','attempt','old-attempt',record)
        self.assertEqual(first,second)
        self.assertEqual(first['record'],record)
        self.assertEqual(first['acceptance'],'legacy_unverified')
        self.assertEqual(await self.store.pending_events('observer'),[])
        with self.assertRaisesRegex(RuntimeErrorCode,'IMPORT_CONFLICT'):
            await self.store.import_history('legacy-host','attempt','old-attempt',{'status':'changed'})

    async def test_size_bounds_do_not_block_progress(self):
        r,a,b = await self.owned()
        with self.assertRaisesRegex(RuntimeErrorCode,'METADATA_TOO_LARGE'):
            await self.store.report(*b,1,'preparing',remote={'detail':'x'*20000})
        await self.store.report(*b,1,'preparing')
        self.assertEqual(len(await self.store.pending_events('progress')),3)

    async def test_changed_bytes_between_verification_and_validation_reject(self):
        r,a,b = await self.validating()
        base = self.provider
        blob = base.put([b'original'])
        class ChangingProvider:
            name = base.name
            def verify(self,key):return base.verify(key)
            def read(self,key):yield b'changed-after-verification'
        with self.assertRaisesRegex(RuntimeErrorCode,'ARTIFACT_DIGEST_MISMATCH'):
            await self.store.complete(*b,ChangingProvider(),blob.key,lambda chunks:True)
        self.assertEqual((await self.store.get(r['id']))['state'],'validating')

class SQLitePersistence(PersistenceScenarios,unittest.IsolatedAsyncioTestCase):
    postgres = False

@unittest.skipUnless(os.environ.get('DIOS_TEST_POSTGRES_URL'),'isolated PostgreSQL test URL not set')
class PostgreSQLPersistence(PersistenceScenarios,unittest.IsolatedAsyncioTestCase):
    postgres = True

class ArtifactScenarios(unittest.TestCase):
    def test_binary_chunked_chinese_roundtrip_reuse_and_corruption(self):
        with tempfile.TemporaryDirectory() as d:
            provider=LocalArtifacts(Path(d));raw=json.dumps({'内容':'汉字与🙂'*10000},ensure_ascii=False).encode()
            blob=provider.put((raw[n:n+7] for n in range(0,len(raw),7)))
            self.assertEqual(blob,provider.put([raw]))
            self.assertEqual(b''.join(provider.read(blob.key)),raw)
            self.assertEqual(provider.verify(blob.key).digest,hashlib.sha256(raw).hexdigest())
            (Path(d)/blob.key).write_bytes(b'corrupt')
            with self.assertRaisesRegex(RuntimeErrorCode,'ARTIFACT_DIGEST_MISMATCH'):provider.verify(blob.key)

    def test_size_digest_path_and_interrupted_upload(self):
        with tempfile.TemporaryDirectory() as d:
            p=LocalArtifacts(Path(d),max_bytes=4)
            with self.assertRaisesRegex(RuntimeErrorCode,'ARTIFACT_TOO_LARGE'):p.put([b'12345'])
            with self.assertRaisesRegex(RuntimeErrorCode,'ARTIFACT_DIGEST_MISMATCH'):p.put([b'1'],expected_digest='0'*64)
            with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_ARTIFACT_KEY'):p.verify('../outside')
            def broken():
                yield b'1'
                raise OSError('network lost')
            with self.assertRaises(OSError):p.put(broken())
            self.assertEqual(list(Path(d).iterdir()),[])

if __name__=='__main__':unittest.main()
