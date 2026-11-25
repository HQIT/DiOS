"""Immutable versions, restart/restore and authenticated native-reference access."""
import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import test_runtime_principals as principals
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.objects import Objects, reference
from app.runtime.recovery import backup_sqlite, restore_sqlite, verify_backup


class ObjectScenarios:
    postgres=False

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.url=os.environ['DIOS_TEST_POSTGRES_URL'] if self.postgres else 'sqlite+aiosqlite:///'+str(self.root/'state.sqlite')
        self.db=Database(self.url);await self.db.migrate()
        self.scope='objects-'+uuid4().hex;self.catalog=Objects(self.db,self.scope)
        self.provider=LocalArtifacts(self.root/'objects')

    async def asyncTearDown(self):
        await self.db.close();self.temp.cleanup()

    async def test_versions_cas_replay_and_restart(self):
        first=await self.catalog.publish(self.provider,['中文🙂'.encode()],'first','operator')
        self.assertEqual(await self.catalog.publish(self.provider,['中文🙂'.encode()],'first','operator'),first)
        oid=first['ref']['id']
        results=await asyncio.gather(*[self.catalog.publish(self.provider,[value],key,'operator',object_id=oid,expected_version=1)
            for key,value in [('two',b'two'),('three',b'three')]],return_exceptions=True)
        self.assertEqual(sum(isinstance(x,dict) for x in results),1)
        self.assertEqual([x.code for x in results if isinstance(x,RuntimeErrorCode)],['VERSION_CONFLICT'])
        with self.assertRaisesRegex(RuntimeErrorCode,'IDEMPOTENCY_CONFLICT'):
            await self.catalog.publish(self.provider,[b'changed'],'first','operator')
        await self.db.close();self.db=Database(self.url);self.catalog=Objects(self.db,self.scope)
        self.assertEqual(await self.catalog.get(first['ref']),first)
        page=await self.catalog.versions('file',oid,limit=1)
        self.assertEqual(page['next_after'],1)
        self.assertEqual((await self.catalog.versions('file',oid,after=1))['items'][0]['ref']['version'],2)
        self.assertEqual(b''.join(self.provider.read(first['storage']['key'])),'中文🙂'.encode())

    async def test_digest_reuse_never_merges_identity_and_links_pin_version(self):
        a=await self.catalog.publish(self.provider,[b'same'],'a','operator')
        b=await self.catalog.publish(self.provider,[b'same'],'b','operator')
        self.assertNotEqual(a['ref'],b['ref']);self.assertEqual(a['descriptor'],b['descriptor'])
        target={'namespace':'external-app','key':'opaque-123','version':'v2'}
        link=await self.catalog.link(a['ref'],'associated_with',target,'operator')
        self.assertEqual(link,await self.catalog.link(a['ref'],'associated_with',target,'operator'))
        await self.catalog.link(b['ref'],'derived_from',a['ref'],'operator')
        await self.catalog.publish(self.provider,[b'new'],'a2','operator',object_id=a['ref']['id'],expected_version=1)
        self.assertEqual((await self.catalog.relations(b['ref']))['items'][0]['target'],a['ref'])
        self.assertEqual((await self.catalog.relations(reference('file',a['ref']['id'],2)))['items'],[])
        with self.assertRaisesRegex(RuntimeErrorCode,'NOT_FOUND'):
            await Objects(self.db,'other').get(a['ref'])
        with self.assertRaisesRegex(RuntimeErrorCode,'NOT_FOUND'):
            await self.catalog.link(a['ref'],'derived_from',reference('file','missing'),'operator')

    async def test_backup_restores_all_file_versions_and_opaque_relations(self):
        if self.postgres:self.skipTest('SQLite installation backup contract')
        a=await self.catalog.publish(self.provider,[b'old'],'a','operator')
        b=await self.catalog.publish(self.provider,[b'new'],'b','operator',object_id=a['ref']['id'],expected_version=1)
        await self.catalog.link(b['ref'],'derived_from',a['ref'],'operator')
        bundle=self.root/'backup'
        await asyncio.to_thread(backup_sqlite,self.url,self.provider.root,bundle)
        verified=await asyncio.to_thread(verify_backup,bundle)
        self.assertIsNotNone(verified)
        destination=self.root/'restored'
        restored=await restore_sqlite(bundle,destination)
        self.assertIsNotNone(restored)
        # Resolve the documented restored database independently of original handles.
        paths=list(destination.rglob('*.sqlite'))+list(destination.rglob('*.db'))
        self.assertEqual(len(paths),1)
        db=Database('sqlite+aiosqlite:///'+str(paths[0]))
        try:
            catalog=Objects(db,self.scope)
            self.assertEqual((await catalog.get(a['ref']))['descriptor'],a['descriptor'])
            self.assertEqual((await catalog.get(b['ref']))['descriptor'],b['descriptor'])
            self.assertEqual((await catalog.relations(b['ref']))['items'][0]['target'],a['ref'])
        finally:await db.close()


class SQLiteObjects(ObjectScenarios,unittest.IsolatedAsyncioTestCase):pass


@unittest.skipUnless(os.environ.get('DIOS_TEST_POSTGRES_URL'),'PostgreSQL not configured')
class PostgresObjects(ObjectScenarios,unittest.IsolatedAsyncioTestCase):
    postgres=True


class ObjectApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=principals.PrincipalTests.asyncSetUp
    asyncTearDown=principals.PrincipalTests.asyncTearDown
    save=principals.PrincipalTests.save
    headers_for=principals.PrincipalTests.headers_for
    submit_as=principals.PrincipalTests.submit_as
    get=principals.PrincipalTests.get
    finish=principals.PrincipalTests.finish

    async def test_native_ids_share_revoke_and_runtime_replacement(self):
        run=await self.submit_as('a');await self.finish()
        detail=await self.console.detail(run['id'])
        refs=[detail['artifacts'][0]['object_ref'],detail['attempts'][0]['object_ref']]
        for ref in refs:
            path=f"objects/{ref['kind']}/{ref['id']}/versions/{ref['version']}"
            a=await self.get('a',path);self.assertEqual(a.status_code,200,a.text)
            self.assertNotIn('storage',a.json())
            self.assertEqual(a.json()['binding']['run_id'],run['id'])
            self.assertEqual((await self.get('b',path)).status_code,404)
            self.directory['principals']['b']['objects'][run['id']]=['run.read','artifact.read'];self.save()
            self.assertEqual((await self.get('b',path)).status_code,200)
            del self.directory['principals']['b']['objects'][run['id']];self.save()
            self.assertEqual((await self.get('b',path)).status_code,404)
        # Replacing a profile/runtime does not change references to original facts.
        path=self.profiles/'review.json';value=json.loads(path.read_text());value['launch']['image']='replacement';path.write_text(json.dumps(value))
        self.assertEqual((await self.console.detail(run['id']))['artifacts'][0]['object_ref'],refs[0])

    async def test_file_api_idempotency_and_operator_boundary(self):
        body={'content_base64':base64.b64encode('版本一'.encode()).decode(),'idempotency_key':'file'}
        path='/api/os/runtime/objects/files'
        self.assertEqual((await self.client.post(path,json=body)).status_code,401)
        self.assertEqual((await self.client.post(path,headers=self.headers_for('a'),json=body)).status_code,403)
        first=await self.client.post(path,headers=self.headers,json=body)
        self.assertEqual(first.status_code,200,first.text);ref=first.json()['ref']
        self.assertEqual((await self.client.post(path,headers=self.headers,json=body)).json(),first.json())
        detail=f"objects/file/{ref['id']}/versions/1"
        self.assertEqual((await self.get('a',detail)).status_code,403)
        response=await self.client.get('/api/os/runtime/'+detail,headers=self.headers)
        self.assertEqual(response.json(),first.json());self.assertNotIn('storage',response.json())
