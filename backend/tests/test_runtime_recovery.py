import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from sqlalchemy import text
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.store import RuntimeStore
from app.runtime.lifecycle import Lifecycle
from app.runtime.artifacts import LocalArtifacts
from app.runtime.history import HistoryQueries
from app.runtime.recovery import backup_sqlite, verify_backup, restore_sqlite


class DrainScenarios:
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.url=os.environ['DIOS_TEST_POSTGRES_URL'] if self.postgres else 'sqlite+aiosqlite:///'+self.temp.name+'/db.sqlite'
        self.db=Database(self.url);await self.db.migrate()
        self.scope=str(uuid4());self.store=RuntimeStore(self.db,self.scope);self.lifecycle=Lifecycle(self.db,self.scope)
        self.provider=LocalArtifacts(Path(self.temp.name)/'objects')
        self.worker,self.instance=str(uuid4()),str(uuid4())
        await self.store.register_worker(self.worker,self.instance,'fixture',os.getpid())

    async def asyncTearDown(self):await self.db.close();self.temp.cleanup()

    async def active(self):
        run=await self.store.submit('active',{})
        attempt=await self.store.claim(self.worker,self.instance)
        return run,attempt,(run['id'],attempt['id'],self.worker,self.instance,attempt['epoch'])

    async def finish(self,binding):
        for seq,phase in enumerate(['preparing','creating_session','waiting_model','validating'],1):
            await self.store.report(*binding,seq,phase,session_id='session' if seq>=3 else None)
        blob=self.provider.put(['完整结果'.encode()])
        return await self.store.complete(*binding,self.provider,blob.key,lambda chunks:b''.join(chunks)=='完整结果'.encode())

    async def test_drain_allows_natural_completion_and_preserves_queued_identity(self):
        run,a,binding=await self.active()
        queued=await self.store.submit('queued',{})
        start=await self.lifecycle.begin_drain()
        self.assertEqual(start['active_attempts'],1);self.assertFalse(start['ready_for_cutover'])
        self.assertEqual((await self.store.submit('queued',{}))['id'],queued['id'])
        with self.assertRaisesRegex(RuntimeErrorCode,'RUNTIME_DRAINING'):await self.store.submit('new',{})
        newcomer=str(uuid4());await self.store.register_worker(newcomer,newcomer,'other',os.getpid())
        self.assertIsNone(await self.store.claim(newcomer,newcomer))
        await self.store.heartbeat(*binding)
        artifact=await self.finish(binding)
        final=await self.lifecycle.wait_drained(1)
        self.assertTrue(final['ready_for_cutover']);self.assertEqual(final['queued_runs'],1)
        view=await HistoryQueries(self.db,self.scope).run(run['id'])
        self.assertEqual(view['artifacts']['items'][0]['id'],artifact)
        resumed=await self.lifecycle.resume(final['revision'])
        self.assertEqual(resumed['mode'],'running')
        self.assertEqual((await self.store.claim(newcomer,newcomer))['run_id'],queued['id'])

    async def test_timeout_and_unknown_result_do_not_reassign(self):
        run,a,binding=await self.active();await self.lifecycle.begin_drain()
        with self.assertRaisesRegex(RuntimeErrorCode,'DRAIN_(OBSERVATION_)?TIMEOUT'):await self.lifecycle.wait_drained(.05)
        self.assertEqual((await self.store.get(run['id']))['state'],'executing')
        async with self.db.transaction(self.scope) as c:
            await c.execute(text('UPDATE runtime_attempts SET lease_until=0 WHERE id=:id'),{'id':a['id']})
        await self.store.expire()
        state=await self.lifecycle.status()
        self.assertEqual(state['reconciliation_required'],1);self.assertFalse(state['ready_for_cutover'])
        self.assertEqual((await self.store.get(run['id']))['state'],'reconciliation_required')

    async def test_slow_observation_does_not_kill_or_reassign(self):
        run,a,binding=await self.active();await self.lifecycle.begin_drain()
        async def slow():await asyncio.sleep(30)
        with patch.object(self.lifecycle,'status',slow):
            with self.assertRaisesRegex(RuntimeErrorCode,'DRAIN_OBSERVATION_TIMEOUT'):
                await self.lifecycle.wait_drained(.01)
        self.assertEqual((await self.lifecycle.status())['mode'],'draining')
        self.assertEqual((await self.store.get(run['id']))['attempt_id'],a['id'])
        await self.store.heartbeat(*binding)

    async def test_restart_scope_isolation_and_stale_resume(self):
        first=await self.lifecycle.begin_drain()
        self.assertEqual((await self.lifecycle.begin_drain())['revision'],first['revision'])
        await self.db.close();self.db=Database(self.url)
        self.lifecycle=Lifecycle(self.db,self.scope);self.store=RuntimeStore(self.db,self.scope)
        self.assertEqual((await self.lifecycle.status())['mode'],'draining')
        other=RuntimeStore(self.db,str(uuid4()))
        self.assertEqual((await other.submit('unrelated',{}))['state'],'queued')
        with self.assertRaisesRegex(RuntimeErrorCode,'VERSION_CONFLICT'):await self.lifecycle.resume(first['revision']+1)
        await self.lifecycle.resume(first['revision'])
        second=await self.lifecycle.begin_drain()
        with self.assertRaisesRegex(RuntimeErrorCode,'VERSION_CONFLICT'):await self.lifecycle.resume(first['revision'])
        self.assertGreater(second['revision'],first['revision'])

    async def test_racing_claim_and_drain_never_hide_an_accepted_attempt(self):
        await self.store.submit('racing',{})
        claim,drained=await asyncio.gather(self.store.claim(self.worker,self.instance),self.lifecycle.begin_drain())
        state=await self.lifecycle.status()
        self.assertEqual(state['active_attempts'],int(claim is not None))
        self.assertEqual(state['ready_for_cutover'],claim is None)
        self.assertIsNone(await self.store.claim(self.worker,self.instance))


class SQLiteDrain(DrainScenarios,unittest.IsolatedAsyncioTestCase):postgres=False


@unittest.skipUnless(os.environ.get('DIOS_TEST_POSTGRES_URL'),'isolated PostgreSQL test URL not set')
class PostgresDrain(DrainScenarios,unittest.IsolatedAsyncioTestCase):postgres=True


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.url='sqlite+aiosqlite:///'+str(self.root/'source.sqlite')
        self.db=Database(self.url);await self.db.migrate()
        self.store=RuntimeStore(self.db,'test');self.provider=LocalArtifacts(self.root/'objects')
        self.bundle=self.root/'bundle';self.restored=self.root/'restored'

    async def asyncTearDown(self):await self.db.close();self.temp.cleanup()

    async def seed(self):
        run=await self.store.submit('done',{'中文':'保留'})
        await self.store.register_worker('w','instance','host',os.getpid())
        a=await self.store.claim('w','instance');binding=(run['id'],a['id'],'w','instance',a['epoch'])
        for seq,phase in enumerate(['preparing','creating_session','waiting_model','validating'],1):
            await self.store.report(*binding,seq,phase,session_id='remote-session' if seq>=3 else None)
        blob=self.provider.put(['中文产物'.encode()])
        artifact=await self.store.complete(*binding,self.provider,blob.key,lambda chunks:b''.join(chunks)=='中文产物'.encode())
        self.provider.put([b'unreferenced-object'])
        await self.store.import_history('old','task','old-task',{'status':'success'})
        await HistoryQueries(self.db,'test').link('old','task','old-task','has_attempt','old','attempt','unresolved')
        return run,artifact,blob

    async def test_backup_restore_preserves_links_events_blobs_and_holds_new_work(self):
        run,artifact,blob=await self.seed()
        queued=await self.store.submit('queued',{})
        manifest=await asyncio.to_thread(backup_sqlite,self.url,self.provider.root,self.bundle)
        self.assertEqual(set(manifest['objects']),{blob.key})
        before=await self.store.pending_events('audit')
        receipt=await restore_sqlite(self.bundle,self.restored)
        self.assertEqual(receipt['mode'],'restore_hold')
        db=Database('sqlite+aiosqlite:///'+str(self.restored/'state.sqlite'))
        try:
            store=RuntimeStore(db,'test');query=HistoryQueries(db,'test')
            self.assertEqual((await store.get(run['id']))['input'],{'中文':'保留'})
            self.assertEqual(await store.pending_events('audit'),before)
            self.assertEqual((await query.run(run['id']))['artifacts']['items'][0]['id'],artifact)
            self.assertEqual((await query.related('old','task','old-task'))['items'][0]['resolution'],'unresolved')
            self.assertEqual(b''.join(LocalArtifacts(self.restored/'objects').read(blob.key)),'中文产物'.encode())
            self.assertEqual((await store.submit('queued',{}))['id'],queued['id'])
            for scope in ('test','brand-new-scope'):
                with self.assertRaisesRegex(RuntimeErrorCode,'RESTORE_RECONCILIATION_REQUIRED'):
                    await RuntimeStore(db,scope).submit('new',{})
            with self.assertRaisesRegex(RuntimeErrorCode,'RESTORE_RECONCILIATION_REQUIRED'):await store.claim('w','instance')
            with self.assertRaisesRegex(RuntimeErrorCode,'RESTORE_RECONCILIATION_REQUIRED'):await Lifecycle(db,'test').resume(0)
            self.assertEqual((await Lifecycle(db,'test').status())['mode'],'restore_hold')
        finally:await db.close()
        self.assertEqual((await self.store.submit('original-still-open',{}))['state'],'queued')

    async def test_restore_rejects_old_live_owner(self):
        run=await self.store.submit('active',{})
        await self.store.register_worker('w','instance','host',os.getpid())
        a=await self.store.claim('w','instance')
        await asyncio.to_thread(backup_sqlite,self.url,self.provider.root,self.bundle)
        await restore_sqlite(self.bundle,self.restored)
        db=Database('sqlite+aiosqlite:///'+str(self.restored/'state.sqlite'))
        try:
            binding=(run['id'],a['id'],'w','instance',a['epoch'])
            with self.assertRaisesRegex(RuntimeErrorCode,'RESTORE_RECONCILIATION_REQUIRED'):
                await RuntimeStore(db,'test').heartbeat(*binding)
            with self.assertRaisesRegex(RuntimeErrorCode,'RESTORE_RECONCILIATION_REQUIRED'):
                await RuntimeStore(db,'test').report(*binding,1,'preparing')
            await self.store.heartbeat(*binding)
        finally:await db.close()

    async def test_wal_snapshot_is_consistent_with_uncommitted_writer(self):
        await self.db.close()
        c=sqlite3.connect(self.root/'source.sqlite',isolation_level=None)
        try:
            self.assertEqual(c.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
            self.db=Database(self.url);self.store=RuntimeStore(self.db,'test')
            run=await self.store.submit('snapshot',{})
            c.execute('BEGIN IMMEDIATE');c.execute("UPDATE runtime_runs SET state='failed' WHERE id=?",(run['id'],))
            await asyncio.to_thread(backup_sqlite,self.url,self.provider.root,self.bundle)
            c.rollback()
            snapshot=sqlite3.connect(self.bundle/'state.sqlite')
            try:self.assertEqual(snapshot.execute('SELECT state FROM runtime_runs WHERE id=?',(run['id'],)).fetchone()[0],'queued')
            finally:snapshot.close()
            self.assertEqual(verify_backup(self.bundle)['counts']['runtime_runs'],1)
        finally:c.close()

    async def test_corrupt_object_or_database_is_rejected_before_restore_publication(self):
        run,artifact,blob=await self.seed()
        await asyncio.to_thread(backup_sqlite,self.url,self.provider.root,self.bundle)
        (self.bundle/'objects'/blob.key).write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeErrorCode,'BACKUP_ARTIFACT_MISMATCH'):await restore_sqlite(self.bundle,self.restored)
        self.assertFalse(self.restored.exists())
        (self.bundle/'objects'/blob.key).write_bytes('中文产物'.encode())
        with (self.bundle/'state.sqlite').open('ab') as f:f.write(b'bad')
        with self.assertRaisesRegex(RuntimeErrorCode,'BACKUP_DATABASE_MISMATCH'):verify_backup(self.bundle)

    async def test_missing_object_incomplete_backup_and_existing_destination(self):
        run,artifact,blob=await self.seed()
        (self.provider.root/blob.key).unlink()
        with self.assertRaises(FileNotFoundError):await asyncio.to_thread(backup_sqlite,self.url,self.provider.root,self.bundle)
        self.assertFalse((self.bundle/'manifest.json').exists())
        with self.assertRaisesRegex(RuntimeErrorCode,'BACKUP_MANIFEST_INVALID'):verify_backup(self.bundle)
        with self.assertRaises(FileExistsError):await asyncio.to_thread(backup_sqlite,self.url,self.provider.root,self.bundle)
        self.provider.put(['中文产物'.encode()])
        second=self.root/'second';await asyncio.to_thread(backup_sqlite,self.url,self.provider.root,second)
        self.restored.mkdir();marker=self.restored/'keep';marker.write_text('existing')
        with self.assertRaises(FileExistsError):await restore_sqlite(second,self.restored)
        self.assertEqual(marker.read_text(),'existing')

    async def test_schema_failure_never_publishes_restore_database(self):
        await asyncio.to_thread(backup_sqlite,self.url,self.provider.root,self.bundle)
        async def fail(_):raise RuntimeError('upgrade failed')
        with patch.object(Database,'migrate',fail):
            with self.assertRaisesRegex(RuntimeError,'upgrade failed'):await restore_sqlite(self.bundle,self.restored)
        self.assertFalse((self.restored/'state.sqlite').exists())
        self.assertTrue((self.restored/'restore.pending.sqlite').exists())
        self.assertEqual((await self.store.submit('source-intact',{}))['state'],'queued')

    async def test_manifest_path_escape_and_unsupported_provider_rejected(self):
        await asyncio.to_thread(backup_sqlite,self.url,self.provider.root,self.bundle)
        path=self.bundle/'manifest.json';manifest=json.loads(path.read_text())
        manifest['objects']={'../outside':{'sha256':'../outside','size':0}}
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeErrorCode,'BACKUP_MANIFEST_INVALID'):verify_backup(self.bundle)
        with self.assertRaisesRegex(RuntimeErrorCode,'BACKUP_PROVIDER_UNSUPPORTED'):
            backup_sqlite('postgresql+asyncpg://localhost/test',self.provider.root,self.root/'pg')

    async def test_cli_backup_verify_restore_and_drain(self):
        root=Path(__file__).resolve().parents[2]
        async def call(*args):
            p=await asyncio.create_subprocess_exec(sys.executable,str(root/'cli/dios'),'runtime',
                '--database-url',self.url,'--scope','test',*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await p.communicate();self.assertEqual(p.returncode,0,err.decode()+out.decode())
            return json.loads(out)
        self.assertTrue((await call('upgrade','drain'))['ready_for_cutover'])
        self.assertEqual((await call('upgrade','status'))['mode'],'draining')
        self.assertTrue((await call('upgrade','wait','--timeout','1'))['ready_for_cutover'])
        manifest=await call('backup','--artifacts',str(self.provider.root),'--destination',str(self.bundle))
        self.assertEqual((await call('backup-verify',str(self.bundle)))['database'],manifest['database'])
        self.assertEqual((await call('restore',str(self.bundle),str(self.restored)))['mode'],'restore_hold')
        self.assertEqual((await call('upgrade','resume','--revision','1'))['mode'],'running')


if __name__=='__main__':unittest.main()
