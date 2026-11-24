"""Lease/recovery tests for standalone issuers; no model or PDP required."""
import asyncio
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import time
import unittest
from sqlalchemy import select
from app.runtime.database import Database,RuntimeErrorCode
from app.runtime.store import RuntimeStore
from app.runtime.principals import FilePrincipals
from app.runtime.delegation_schema import links,dependencies,events
from app.runtime.delegation_identity_schema import issuers
from app.runtime.delegation_check_schema import checks
from app.runtime.delegation_reconciliation import DelegationReconciler,observation
from app.runtime.delegations import Delegations
from app.runtime.work_auth_schema import work,grants

class DelegationReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.db=Database('sqlite+aiosqlite:///'+str(self.root/'runtime.sqlite'));await self.db.migrate()
        self.store=RuntimeStore(self.db,'one');self.manager=Delegations(self.store)
        self.reconciler=DelegationReconciler(self.store,interval=1,retry_base=2,retry_max=8)
        self.path=self.root/'principals.json';self.token=secrets.token_urlsafe(32);now=time.time()
        self.permission=dict(action='read',resource_kind='object',resource_id='one',resource_version='v1',constraint_revision='v1')
        self.doc=dict(version=1,issuer='fixture',audience='dios-runtime',principals={'issuer':dict(subject='subject',actor='actor',scope='one',enabled=True,
            revision=1,expires_at=now+1000,tokens=[dict(sha256=hashlib.sha256(self.token.encode()).hexdigest(),enabled=True,not_before=now-1,expires_at=now+1000)],
            profiles={'profile':dict(actions=['delegation.issue'],permissions=[self.permission])})})
        self.save();principal=FilePrincipals(self.path).authenticate(self.token,'one')
        async with self.db.transaction('one') as c:
            await c.execute(links.insert().values(id='delegation',scope='one',request_digest='a'*64,parent_ref=None,
                subject_ref='subject',actor_ref='target',audience='dios-runtime',permissions=[self.permission],expires_at=now+600,
                depth=0,authorization_epoch=1,state='active',version=1,policy_evaluator={},created_at=now))
            await c.execute(issuers.insert().values(delegation_ref='delegation',scope='one',provider_path=str(self.path),principal=principal,profile_id='profile'))
    async def asyncTearDown(self):await self.db.close();self.temp.cleanup()
    def save(self):self.path.write_text(json.dumps(self.doc))
    async def due(self):
        async with self.db.transaction('one') as c:
            await c.execute(checks.update().values(next_check_at=0,lease_until=0))
    async def observed(self):
        async with self.db.read('one') as c:return await observation(c,'one','delegation',await self.db.now(c))
    async def test_standalone_revocation_is_persistent_without_any_run(self):
        self.doc['principals']['issuer']['enabled']=False;self.save()
        result=await self.reconciler.batch();self.assertEqual(result['items'][0]['outcome'],'revoked')
        self.assertFalse((await self.manager.show('delegation'))['effective'])
        self.doc['principals']['issuer']['enabled']=True;self.save()
        self.assertEqual((await self.reconciler.batch())['checked'],0)
        self.assertEqual((await self.manager.show('delegation'))['chain'][0]['authorization_epoch'],2)
    async def test_outage_backoff_recovers_and_resets_failures(self):
        self.path.write_text('{bad')
        await self.reconciler.batch();first=await self.observed()
        self.assertEqual(first['outcome'],'unavailable');self.assertEqual(first['failures'],1)
        self.assertAlmostEqual(first['next_check_at']-first['checked_at'],2)
        self.assertEqual((await self.reconciler.batch())['checked'],0)
        await self.due();await self.reconciler.batch();second=await self.observed()
        self.assertEqual(second['failures'],2);self.assertAlmostEqual(second['next_check_at']-second['checked_at'],4)
        self.save();await self.due();await self.reconciler.batch();last=await self.observed()
        self.assertEqual((last['outcome'],last['failures']),('valid',0));self.assertTrue(last['last_success_at'])
        self.assertTrue((await self.manager.show('delegation'))['effective'])
    async def test_concurrent_claim_and_replaced_owner_fence_late_revoke(self):
        other=Database(str(self.db.engine.url));peer=DelegationReconciler(RuntimeStore(other,'one'))
        try:
            claims=await asyncio.gather(self.reconciler.claim(),peer.claim());self.assertEqual(sum(x is not None for x in claims),1)
            old=next(x for x in claims if x);await self.due();new=await peer.claim()
            self.assertEqual(new['generation'],old['generation']+1)
            self.assertEqual((await self.reconciler.finish(old,'revoked','AUTH_PRINCIPAL_REVOKED'))['outcome'],'stale')
            self.assertEqual((await peer.finish(new,*peer.inspect(new['binding'])))['outcome'],'valid')
            self.assertTrue((await self.manager.show('delegation'))['effective'])
        finally:await other.close()
    async def test_expired_claim_cannot_revoke(self):
        claim=await self.reconciler.claim();await self.due()
        self.assertEqual((await self.reconciler.finish(claim,'revoked','AUTH_PRINCIPAL_REVOKED'))['outcome'],'stale')
        self.assertTrue((await self.manager.show('delegation'))['effective'])
    async def test_explicit_revoke_wins_over_pending_success(self):
        claim=await self.reconciler.claim();await self.manager.revoke('delegation',1)
        self.assertEqual((await self.reconciler.finish(claim,'valid','IDENTITY_SOURCE_CURRENT'))['outcome'],'superseded')
        detail=await self.manager.show('delegation');self.assertEqual(detail['chain'][0]['authorization_epoch'],2)
        self.assertEqual(len([x for x in detail['events'] if x['operation']=='revoked']),1)
    async def test_scope_separation_and_invalid_observation(self):
        self.assertIsNone(await DelegationReconciler(RuntimeStore(self.db,'other')).claim())
        claim=await self.reconciler.claim()
        with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_IDENTITY_OBSERVATION'):
            await self.reconciler.finish(claim,'revoked','network timeout')
        self.assertTrue((await self.manager.show('delegation'))['effective'])
    async def test_confirmed_permission_removal_revokes_dependent_work_and_grant(self):
        now=time.time()
        async with self.db.transaction('one') as c:
            await c.execute(work.insert().values(id='wa',scope='one',run_id='run',subject='subject',actor='target',
                policy_id='p',policy_digest='a'*64,profile_id='profile',profile_digest='b'*64,permissions={},secret_refs=[],
                state='active',version=1,authorization_epoch=1,created_at=now,expires_at=now+600))
            await c.execute(grants.insert().values(attempt_id='attempt',authorization_id='wa',run_id='run',worker_id='worker',worker_instance='boot',
                lease_epoch=1,authorization_epoch=1,session_id='session',version=1,state='active',expires_at=now+600))
            await c.execute(dependencies.insert().values(authorization_ref='wa',delegation_ref='delegation',scope='one'))
        self.doc['principals']['issuer']['profiles']['profile']['permissions']=[];self.save()
        await self.reconciler.batch()
        async with self.db.read('one') as c:
            self.assertEqual((await c.execute(select(work.c.state))).scalar_one(),'revoked')
            self.assertEqual((await c.execute(select(grants.c.state))).scalar_one(),'revoked')
            self.assertEqual((await c.execute(select(events.c.details))).scalar_one()['revoked_work_count'],1)
    async def test_backup_restore_preserves_lease_and_observation(self):
        from app.runtime.recovery import backup_sqlite,restore_sqlite
        await self.reconciler.batch();before=await self.observed();await self.due();claim=await self.reconciler.claim()
        objects=self.root/'objects';objects.mkdir();backup=self.root/'backup'
        await asyncio.to_thread(backup_sqlite,str(self.db.engine.url),objects,backup)
        target=self.root/'restored';await restore_sqlite(backup,target)
        restored=Database('sqlite+aiosqlite:///'+str(target/'state.sqlite'))
        try:
            agent=DelegationReconciler(RuntimeStore(restored,'one'))
            self.assertIsNone(await agent.claim())
            async with restored.transaction('one') as c:
                row=(await c.execute(select(checks))).mappings().one()
                self.assertEqual(row['last_success_at'],before['last_success_at'])
                await c.execute(checks.update().values(lease_until=0,next_check_at=0))
            new=await agent.claim();self.assertGreater(new['generation'],claim['generation'])
            self.assertEqual((await agent.finish(claim,'revoked','AUTH_PRINCIPAL_REVOKED'))['outcome'],'stale')
        finally:await restored.close()
