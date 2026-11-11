import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from sqlalchemy import select, text, inspect

from app.runtime.database import Database, RuntimeErrorCode, SCHEMA_CHECKSUM
from app.runtime.schema import metadata, versions, runs
from app.runtime.history_schema import versions as history_versions, links, CHECKSUM
from app.runtime.history import HistoryQueries
from app.runtime.store import RuntimeStore
from app.runtime.artifacts import LocalArtifacts


class HistoryScenarios:
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        url = os.environ['DIOS_TEST_POSTGRES_URL'] if self.postgres else 'sqlite+aiosqlite:///'+self.temp.name+'/state.db'
        self.db = Database(url)
        await self.db.migrate()
        self.scope = str(uuid4())
        self.store = RuntimeStore(self.db, self.scope)
        self.query = HistoryQueries(self.db, self.scope)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def seed(self):
        for kind, source_id in [('task','t'), ('attempt','a1'), ('attempt','a2'), ('session','s'), ('artifact','b')]:
            await self.store.import_history('old', kind, source_id,
                {'state': 'success', 'protocol_version': None, 'capabilities': None, 'note': '中文历史'})
        for aid in ('a1','a2'):
            await self.query.link('old','task','t','has_attempt','old','attempt',aid)
            await self.query.link('old','attempt',aid,'uses_session','old','session','s')
            await self.query.link('old','attempt',aid,'produced_artifact','old','artifact','b')

    async def test_task_attempt_session_artifact_both_directions_preserve_sources(self):
        await self.seed()
        children = await self.query.related('old','task','t')
        self.assertEqual({e['peer']['source_id'] for e in children['items']}, {'a1','a2'})
        for kind, source_id in [('session','s'), ('artifact','b')]:
            parents = await self.query.related('old',kind,source_id,direction='incoming')
            self.assertEqual({e['peer']['source_id'] for e in parents['items']}, {'a1','a2'})
            self.assertTrue(all(e['resolution']=='resolved' for e in parents['items']))
        self.assertEqual(children['root']['acceptance'], 'legacy_unverified')
        self.assertEqual(await self.store.pending_events('observer'), [])
        async with self.db.read(self.scope) as c:
            self.assertEqual((await c.execute(select(runs.c.id).where(runs.c.scope==self.scope))).all(), [])

    async def test_raw_record_is_opt_in_immutable_and_not_in_query_pages(self):
        await self.seed()
        minimal = await self.query.imported('old','task','t')
        self.assertNotIn('record', minimal['record'])
        full = await self.query.imported('old','task','t',include_record=True)
        self.assertIsNone(full['record']['record']['protocol_version'])
        self.assertEqual(full['record']['record']['note'], '中文历史')
        with self.assertRaisesRegex(RuntimeErrorCode,'IMPORT_CONFLICT'):
            await self.store.import_history('old','task','t',{'state':'different'})
        self.assertEqual((await self.query.imported('old','task','t',include_record=True))['record'],full['record'])
        self.assertNotIn('中文历史',json.dumps(await self.query.list_imported(),ensure_ascii=False))

    async def test_dangling_link_resolves_after_later_import_without_rewriting_identity(self):
        await self.store.import_history('old','task','t',{})
        link = await self.query.link('old','task','t','has_attempt','second','attempt','later')
        unresolved = (await self.query.related('old','task','t'))['items'][0]
        self.assertEqual(unresolved['resolution'],'unresolved')
        self.assertIsNone(unresolved['peer']['target_id'])
        target = await self.store.import_history('second','attempt','later',{})
        resolved = (await self.query.related('old','task','t'))['items'][0]
        self.assertEqual(resolved['id'],link['id'])
        self.assertEqual(resolved['peer']['target_id'],target['target_id'])
        self.assertEqual(resolved['resolution'],'resolved')

    async def test_link_idempotency_and_invalid_kinds(self):
        await self.store.import_history('old','task','t',{})
        values = await asyncio.gather(*(self.query.link('old','task','t','has_attempt','old','attempt','a') for _ in range(4)))
        self.assertEqual(len({v['id'] for v in values}),1)
        self.assertEqual(len((await self.query.related('old','task','t'))['items']),1)
        with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_HISTORY_RELATION'):
            await self.query.link('old','task','t','has_attempt','old','session','s')
        with self.assertRaisesRegex(RuntimeErrorCode,'NOT_FOUND'):
            await self.query.link('old','task','missing','has_attempt','old','attempt','a')

    async def test_scope_and_source_isolation_even_with_reused_ids(self):
        await self.seed()
        other = RuntimeStore(self.db,str(uuid4()))
        await other.import_history('old','attempt','a1',{'different':'workspace'})
        foreign_query = HistoryQueries(self.db,other.scope)
        with self.assertRaisesRegex(RuntimeErrorCode,'NOT_FOUND'): await foreign_query.related('old','task','t')
        self.assertEqual(len((await foreign_query.list_imported())['items']),1)
        await self.store.import_history('second','attempt','a1',{'different':'source'})
        rows = (await self.query.related('old','task','t'))['items']
        self.assertTrue(all(r['peer']['source']=='old' for r in rows))
        self.assertEqual((await self.query.list_imported(source='missing'))['items'], [])

    async def test_keyset_pages_and_cursor_filter_binding(self):
        await self.seed()
        cursor=None;seen=[]
        while True:
            result=await self.query.list_imported(source='old',limit=2,cursor=cursor)
            seen.extend(r['target_id'] for r in result['items'])
            cursor=result['next_cursor']
            if not cursor:break
        self.assertEqual(len(seen),5);self.assertEqual(len(set(seen)),5)
        first=await self.query.related('old','task','t',limit=1)
        second=await self.query.related('old','task','t',limit=1,cursor=first['next_cursor'])
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        self.assertIsNone(second['next_cursor'])
        with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_CURSOR'):
            await self.query.related('old','task','t',direction='incoming',cursor=first['next_cursor'])
        listing=await self.query.list_imported(source='old',limit=1)
        with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_CURSOR'):
            await self.query.list_imported(source='second',cursor=listing['next_cursor'])
        with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_CURSOR'):
            await HistoryQueries(self.db,'foreign').list_imported(source='old',cursor=listing['next_cursor'])
        for bad in ('invalid','W10=','e30='):
            with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_CURSOR'): await self.query.list_imported(cursor=bad)
        with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_LIMIT'): await self.query.list_imported(limit=101)

    async def test_native_attempt_qualified_sessions_and_artifact_projection(self):
        run=await self.store.submit('run',{'private':'not-in-history'})
        worker, instance=str(uuid4()),str(uuid4())
        await self.store.register_worker(worker,instance,'host',os.getpid())
        async def start():
            a=await self.store.claim(worker,instance)
            binding=(run['id'],a['id'],worker,instance,a['epoch'])
            for seq, phase in enumerate(['preparing','creating_session','waiting_model','validating'],1):
                await self.store.report(*binding,seq,phase,session_id='same-external-handle' if seq>=3 else None)
            return a,binding
        first,_=await start()
        async with self.db.transaction(self.scope) as c:
            await c.execute(text('UPDATE runtime_attempts SET lease_until=0 WHERE id=:id'),{'id':first['id']})
        await self.store.expire()
        row=await self.store.get(run['id'])
        await self.store.reconcile_absent(run['id'],row['version'],'fixture-no-effect')
        row=await self.store.get(run['id'])
        await self.store.retry(run['id'],row['version'])
        second,binding=await start()
        provider=LocalArtifacts(Path(self.temp.name)/'objects')
        blob=provider.put(['完整产物'.encode()])
        artifact=await self.store.complete(*binding,provider,blob.key,lambda chunks:b''.join(chunks)=='完整产物'.encode())
        view=await self.query.run(run['id'],limit=1)
        next_view=await self.query.run(run['id'],limit=1,attempt_cursor=view['attempts']['next_cursor'])
        self.assertEqual(view['run']['state'],'succeeded')
        self.assertEqual(view['sessions'][0]['session_id'],next_view['sessions'][0]['session_id'])
        self.assertNotEqual(view['sessions'][0]['attempt_id'],next_view['sessions'][0]['attempt_id'])
        self.assertEqual(next_view['sessions'][0]['attempt_id'],second['id'])
        self.assertEqual(view['artifacts']['items'][0]['id'],artifact)
        self.assertEqual(view['artifacts']['items'][0]['attempt_id'],second['id'])
        self.assertNotIn('storage_key',view['artifacts']['items'][0])
        self.assertNotIn('not-in-history',json.dumps(view))
        with self.assertRaisesRegex(RuntimeErrorCode,'NOT_FOUND'):
            await HistoryQueries(self.db,'foreign').run(run['id'])
        with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_CURSOR'):
            await self.query.run(run['id'],artifact_cursor=view['attempts']['next_cursor'])


class SQLiteHistory(HistoryScenarios,unittest.IsolatedAsyncioTestCase):
    postgres=False


@unittest.skipUnless(os.environ.get('DIOS_TEST_POSTGRES_URL'),'isolated PostgreSQL test URL not set')
class PostgresHistory(HistoryScenarios,unittest.IsolatedAsyncioTestCase):
    postgres=True


class HistoryMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_v1_keeps_raw_ids_events_and_schema_checksum(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database('sqlite+aiosqlite:///'+d+'/v1.db')
            try:
                async with db.transaction('migration',check=False) as c:
                    await c.run_sync(metadata.create_all)
                    await c.execute(versions.insert().values(version=1,checksum=SCHEMA_CHECKSUM))
                store=RuntimeStore(db,'scope')
                original=await store.import_history('old','task','task',{'未知':None})
                run=await store.submit('key',{})
                query=HistoryQueries(db,'scope')
                with self.assertRaisesRegex(RuntimeErrorCode,'HISTORY_MIGRATION_REQUIRED'):
                    await query.related('old','task','task')
                self.assertTrue((await db.migrate())['changed'])
                self.assertFalse((await db.migrate())['changed'])
                self.assertEqual((await query.imported('old','task','task',include_record=True))['record']['target_id'],original['target_id'])
                self.assertEqual((await store.get(run['id']))['version'],run['version'])
                async with db.read('scope') as c:
                    self.assertEqual((await c.execute(select(versions.c.checksum))).scalar_one(),SCHEMA_CHECKSUM)
            finally:await db.close()

    async def test_component_version_and_partial_upgrade_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database('sqlite+aiosqlite:///'+d+'/state.db')
            try:
                await db.migrate()
                for field,value,code in [('version',99,'HISTORY_SCHEMA_INCOMPATIBLE'),('checksum','bad','HISTORY_CHECKSUM_MISMATCH')]:
                    async with db.transaction('scope') as c:await c.execute(history_versions.update().values(**{field:value}))
                    with self.assertRaisesRegex(RuntimeErrorCode,code):await db.migrate()
                    async with db.transaction('scope') as c:await c.execute(history_versions.update().values(version=1,checksum=CHECKSUM))
                async with db.transaction('scope') as c:await c.execute(text('DROP TABLE runtime_history_links'))
                with self.assertRaisesRegex(RuntimeErrorCode,'HISTORY_SCHEMA_INCOMPLETE'):await db.migrate()
            finally:await db.close()

    async def test_interrupted_additive_migration_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database('sqlite+aiosqlite:///'+d+'/state.db')
            try:
                original=db.engine.dialect.do_execute
                def fail(cursor,statement,parameters,context=None):
                    if statement.startswith('INSERT INTO runtime_history_schema'):raise RuntimeError('injected failure')
                    return original(cursor,statement,parameters,context)
                with patch.object(db.engine.dialect,'do_execute',side_effect=fail):
                    with self.assertRaisesRegex(RuntimeError,'injected failure'):await db.migrate()
                async with db.engine.connect() as c:
                    names=await c.run_sync(lambda s:inspect(s).get_table_names())
                    self.assertFalse(any(n.startswith('runtime_') for n in names))
                self.assertTrue((await db.migrate())['changed'])
            finally:await db.close()

    async def test_cli_history_end_to_end_without_payload_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            url='sqlite+aiosqlite:///'+d+'/cli.db'
            db=Database(url)
            try:
                await db.migrate()
                store=RuntimeStore(db,'local')
                await store.import_history('old','task','t',{'title':'原始正文'})
                await HistoryQueries(db,'local').link('old','task','t','has_attempt','old','attempt','missing')
                run=await store.submit('one',{})
            finally:await db.close()
            root=Path(__file__).resolve().parents[2]
            async def call(*args):
                p=await asyncio.create_subprocess_exec(sys.executable,str(root/'cli/dios'),'runtime',
                    '--database-url',url,'history',*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                out,err=await p.communicate()
                self.assertEqual(p.returncode,0,err.decode()+out.decode())
                return json.loads(out)
            self.assertEqual(len((await call('list'))['items']),1)
            self.assertNotIn('record',(await call('show','old','task','t'))['record'])
            self.assertEqual((await call('show','old','task','t','--record'))['record']['record']['title'],'原始正文')
            self.assertEqual((await call('links','old','task','t'))['items'][0]['resolution'],'unresolved')
            self.assertEqual((await call('run',run['id']))['run']['id'],run['id'])


import sys
if __name__=='__main__':unittest.main()
