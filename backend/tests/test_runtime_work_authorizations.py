"""Pending regression cases for durable E2AG integration; no real credentials/model."""
import asyncio
from dataclasses import replace
import hashlib
import json
import os
import unittest
from unittest.mock import patch
from sqlalchemy import select, func
import test_runtime_worker as fixtures
from test_runtime_worker import FakeAdapter
from work_auth_fixtures import policy_fixture
from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.store import RuntimeStore
from app.runtime.work_authorizations import WorkAuthorizationVerifier, view
from app.runtime.work_auth_schema import work, grants, decisions
from app.runtime.schema import runs
from app.runtime.worker import Worker
from app.runtime.reconciliation import reconcile_run
from app.runtime.recovery import backup_sqlite, verify_backup
from app.runtime.secret_provider import FileSecretMounts
from app.services.e2ag import evaluate_work_permission


class WorkAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.WorkerTests.asyncSetUp(self)
        self.policy,self.descriptor=policy_fixture(self.root,'test-profile',self.spec)
        self.verifier=WorkAuthorizationVerifier(self.store,self.policy)
    asyncTearDown=fixtures.WorkerTests.asyncTearDown

    async def submit(self,key='one'):
        return await self.store.submit(key,dict(runtime_profile='test-profile',profile_digest=self.spec.fingerprint(),
            secret_refs=[]),authorization=self.descriptor)

    def worker(self,adapter=None):
        return Worker(self.store,adapter or FakeAdapter(),'test-profile',self.spec,self.provider,
            poll_interval=.05,authorization_verifier=self.verifier)

    async def claimed(self):
        r=await self.submit();w=self.worker()
        await self.store.register_worker(w.worker_id,w.instance,'host',os.getpid())
        a=await self.store.claim(w.worker_id,w.instance)
        context=WorkAuthorizationContext(self.scope,r['id'],a['id'],w.worker_id,w.instance,a['epoch'],a['authorization_epoch'],
            None,r['input']['work_authorization_ref'])
        await self.verifier(context,'execute')
        return r,a,context

    async def interrupted(self):
        r=await self.submit();adapter=FakeAdapter()
        with patch.object(self.store,'complete',side_effect=ConnectionError()):
            with self.assertRaises(ConnectionError):await self.worker(adapter).once()
        return r,adapter

    async def test_admission_and_run_are_atomic_on_audit_failure(self):
        with patch('app.runtime.work_authorizations.audit',side_effect=RuntimeErrorCode('AUDIT_UNAVAILABLE')):
            with self.assertRaisesRegex(RuntimeErrorCode,'AUDIT_UNAVAILABLE'):await self.submit()
        async with self.db.read(self.scope) as c:
            self.assertEqual((await c.execute(select(func.count()).select_from(runs).where(runs.c.scope==self.scope))).scalar(),0)
            self.assertEqual((await c.execute(select(func.count()).select_from(work).where(work.c.scope==self.scope))).scalar(),0)

    async def test_idempotent_admission_and_session_bound_grant(self):
        r=await self.submit();again=await self.submit()
        self.assertEqual(r['id'],again['id'])
        result=await self.worker().once();self.assertEqual(result['state'],'succeeded')
        info=await view(self.store,r['id'])
        self.assertEqual(info['state'],'closed');self.assertEqual(info['grant']['session_id'],'fixture-session')
        self.assertEqual(info['grant']['state'],'closed')
        self.assertIn('EXECUTION_SESSION_BOUND',[code for d in info['decisions'] for code in d['reason_codes']])

    async def test_renewal_is_serialized_and_never_exceeds_work_deadline(self):
        r,a,context=await self.claimed()
        async with self.db.transaction(self.scope) as c:
            now=await self.db.now(c)
            await c.execute(work.update().where(work.c.run_id==r['id']).values(expires_at=now+500))
            await c.execute(grants.update().where(grants.c.attempt_id==a['id']).values(expires_at=now+100))
        await asyncio.gather(self.verifier(context,'execute'),self.verifier(context,'execute'))
        async with self.db.read(self.scope) as c:
            g=(await c.execute(select(grants).where(grants.c.attempt_id==a['id']))).mappings().one()
            w=(await c.execute(select(work).where(work.c.run_id==r['id']))).mappings().one()
        self.assertEqual(g['version'],2);self.assertEqual(g['expires_at'],w['expires_at'])

    async def test_renewal_at_work_deadline_does_not_repeat_writes(self):
        r,a,context=await self.claimed()
        async with self.db.transaction(self.scope) as c:
            deadline=(await self.db.now(c))+100
            await c.execute(work.update().where(work.c.run_id==r['id']).values(expires_at=deadline))
            await c.execute(grants.update().where(grants.c.attempt_id==a['id']).values(expires_at=deadline))
        await asyncio.gather(self.verifier(context,'execute'),self.verifier(context,'execute'))
        info=await view(self.store,r['id'])
        self.assertEqual(info['grant']['version'],1)
        self.assertEqual(info['grant']['expires_at'],deadline)

    async def test_expired_grant_cannot_be_reactivated(self):
        _,a,context=await self.claimed()
        async with self.db.transaction(self.scope) as c:
            await c.execute(grants.update().where(grants.c.attempt_id==a['id']).values(expires_at=0))
        with self.assertRaisesRegex(RuntimeErrorCode,'EXECUTION_GRANT_EXPIRED'):await self.verifier(context,'execute')

    async def test_recovery_accepts_complete_output_without_renewing_expired_execution_grant(self):
        r,adapter=await self.interrupted();saved=await self.store.get(r['id'])
        async with self.db.transaction(self.scope) as c:
            await c.execute(grants.update().where(grants.c.attempt_id==saved['attempt_id']).values(expires_at=0))
        result=await reconcile_run(self.store,adapter,self.provider,r['id'],verifier=self.verifier)
        self.assertEqual(result['state'],'succeeded');self.assertEqual(adapter.started,1)
        info=await view(self.store,r['id']);self.assertEqual(info['grant']['expires_at'],0)

    async def test_revoke_prevents_renewal_and_result_reception(self):
        r,adapter=await self.interrupted();saved=await self.store.get(r['id'])
        await self.store.revoke(r['id'],saved['version'])
        result=await reconcile_run(self.store,adapter,self.provider,r['id'],verifier=self.verifier)
        self.assertEqual(result['state'],'failed');self.assertEqual(result['attempts'][0]['error']['code'],'AUTH_REVOKED')
        info=await view(self.store,r['id']);self.assertEqual(info['state'],'revoked');self.assertEqual(info['grant']['state'],'revoked')

    async def test_wrong_scope_worker_epoch_and_session_denied(self):
        _,_,context=await self.claimed()
        for changed in (replace(context,scope='wrong'),replace(context,worker_instance='wrong'),replace(context,lease_epoch=99)):
            with self.assertRaises(RuntimeErrorCode):await self.verifier(changed,'execute')
        await self.verifier(replace(context,session_id='one'),'execute')
        with self.assertRaisesRegex(RuntimeErrorCode,'SESSION_CHANGED'):await self.verifier(replace(context,session_id='two'),'execute')

    async def test_changed_policy_is_audited_and_never_broadens_existing_work(self):
        r,_,context=await self.claimed()
        config=json.loads(self.policy.path.read_text());config['subject']='operator:changed'
        self.policy.path.write_text(json.dumps(config))
        with self.assertRaisesRegex(RuntimeErrorCode,'WORK_POLICY_CHANGED'):await self.verifier(context,'execute')
        info=await view(self.store,r['id'])
        self.assertIn('WORK_POLICY_CHANGED',[code for d in info['decisions'] for code in d['reason_codes']])

    async def test_work_expiry_blocks_recovered_success(self):
        r,adapter=await self.interrupted()
        async with self.db.transaction(self.scope) as c:
            await c.execute(work.update().where(work.c.run_id==r['id']).values(expires_at=0))
        with self.assertRaisesRegex(RuntimeErrorCode,'WORK_AUTHORIZATION_EXPIRED'):
            await reconcile_run(self.store,adapter,self.provider,r['id'],verifier=self.verifier)
        self.assertEqual((await self.store.get(r['id']))['state'],'reconciliation_required')

    async def test_backup_includes_authorization_component(self):
        if self.db.engine.dialect.name!='sqlite':self.skipTest('SQLite backup provider')
        await self.submit();destination=self.root/'backup'
        await asyncio.to_thread(backup_sqlite,self.url,self.provider.root,destination)
        manifest=await asyncio.to_thread(verify_backup,destination)
        self.assertIn('runtime_work_auth_schema',manifest['schemas'])


@unittest.skipUnless(os.environ.get('DIOS_TEST_POSTGRES_URL'),'isolated PostgreSQL not enabled')
class PostgresWorkAuthorizationTests(WorkAuthorizationTests):
    async def asyncSetUp(self):
        await super().asyncSetUp();await self.db.close()
        self.url=os.environ['DIOS_TEST_POSTGRES_URL'];self.db=Database(self.url);await self.db.migrate()
        self.store=RuntimeStore(self.db,self.scope);self.verifier=WorkAuthorizationVerifier(self.store,self.policy)


class WorkPolicyTests(unittest.TestCase):
    def test_permissions_intersect_as_complete_tuples(self):
        target=dict(action='runtime.execute',resource_kind='runtime_profile',resource_id='a',resource_version='v1',constraint_revision='p1')
        one=dict(target,action='runtime.accept_result');two=dict(target,resource_id='b')
        layers={k:[one,two] for k in ('caller','service','workload','resource')}
        self.assertFalse(evaluate_work_permission(layers,target).permits_execution)
        for values in layers.values():values.append(target)
        self.assertTrue(evaluate_work_permission(layers,target).permits_execution)
        layers['resource']=[]
        self.assertFalse(evaluate_work_permission(layers,target).permits_execution)

    def test_secret_scope_version_and_content_checks(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);secret=root/'key';secret.write_text('fixture-not-a-credential')
            row=dict(state='active',version='v1',scope='one',profiles=['profile'],path=str(secret),
                sha256=hashlib.sha256(secret.read_bytes()).hexdigest())
            catalog=root/'secrets.json';catalog.write_text(json.dumps({'version':1,'secrets':{'model':row}}))
            requests=[dict(ref='model',version='v1',target='/run/secrets/model')]
            provider=FileSecretMounts(catalog,'one')
            self.assertTrue(provider.resolve('profile',requests)[0].read_only)
            with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_ACCESS_DENIED'):provider.resolve('other',requests)
            with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_VERSION_CHANGED'):provider.resolve('profile',[dict(requests[0],version='v2')])
            secret.write_text('changed')
            with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_CONTENT_CHANGED'):provider.resolve('profile',requests)
