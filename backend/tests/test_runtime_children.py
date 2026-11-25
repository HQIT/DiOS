import asyncio
from dataclasses import replace
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4
from sqlalchemy import select, func

from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.child_schema import children
from app.runtime.database import RuntimeErrorCode
from app.runtime.schema import runs, attempts
from app.runtime.work_auth_schema import work
from app.runtime.work_authorizations import WorkAuthorizationVerifier, view
from app.runtime.work_policy import permission
from app.runtime.recovery import backup_sqlite, verify_backup
import test_runtime_worker as worker_fixtures
from work_auth_fixtures import policy_fixture


class ChildRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await worker_fixtures.WorkerTests.asyncSetUp(self)
        self.policy, descriptor = policy_fixture(self.root,'test-profile',self.spec)
        config = json.loads(self.policy.path.read_text())
        for layer in config['profiles']['test-profile']['permissions'].values():
            layer.append(permission(descriptor,'spawn'))
        self.policy.path.write_text(json.dumps(config))
        self.descriptor = self.policy.admission('test-profile',self.spec.fingerprint(),[])
        self.verifier = WorkAuthorizationVerifier(self.store,self.policy)
        self.parent = await self.store.submit('parent',self.input(),authorization=self.descriptor)
        self.binding,self.context = await self.activate(self.parent)
        self.parent_binding = dict(owner=list(self.binding),session_id=self.context.session_id,max_children=3,max_depth=3)

    asyncTearDown = worker_fixtures.WorkerTests.asyncTearDown

    def input(self):
        return dict(runtime_profile='test-profile',profile_digest=self.spec.fingerprint(),secret_refs=[])

    async def activate(self, run):
        worker_id = str(uuid4())
        await self.store.register_worker(worker_id,'instance','host',os.getpid())
        attempt = await self.store.claim(worker_id,'instance',profile_id='test-profile')
        self.assertEqual(attempt['run_id'],run['id'])
        binding = (run['id'],attempt['id'],worker_id,'instance',attempt['epoch'])
        context = WorkAuthorizationContext(self.scope,run['id'],attempt['id'],worker_id,'instance',attempt['epoch'],attempt['authorization_epoch'],None,run['input']['work_authorization_ref'])
        await self.verifier(context,'execute')
        await self.store.report(*binding,1,'preparing')
        await self.store.report(*binding,2,'creating_session')
        context = replace(context,session_id='session-'+worker_id)
        await self.verifier(context,'execute')
        await self.store.report(*binding,3,'waiting_model',session_id=context.session_id)
        return binding,context

    async def child(self,key='child',*,binding=None,descriptor=None):
        return await self.store.submit(key,self.input(),authorization=descriptor or self.descriptor,
            parent_binding=binding or self.parent_binding)

    async def counts(self):
        async with self.db.read(self.scope) as c:
            return tuple([await c.scalar(select(func.count()).select_from(t)) for t in (runs,work,children)])

    async def test_admission_deadline_idempotency_and_execution_binding(self):
        values = await asyncio.gather(*(self.child() for _ in range(3)))
        self.assertEqual(len({r['id'] for r in values}),1)
        child = values[0]
        self.assertEqual(await self.counts(),(2,2,1))
        parent_auth = await view(self.store,self.parent['id'])
        self.assertLessEqual((await view(self.store,child['id']))['expires_at'],parent_auth['expires_at'])
        _,context = await self.activate(child)
        self.assertTrue(await self.verifier(context,'execute'))
        family = (await self.store.get(child['id']))['family']
        self.assertEqual(family['parent']['parent_run_id'],self.parent['id'])
        self.assertEqual(family['parent']['depth'],1)
        self.assertEqual(family['parent_close_policy'],'request_cancel')

    async def test_limit_is_shared_by_tree_and_completed_children_do_not_refund(self):
        first = await self.child('one')
        binding,context = await self.activate(first)
        sub = dict(self.parent_binding,owner=list(binding),session_id=context.session_id,max_children=32)
        await self.child('two',binding=sub)
        third = await self.child('three')
        await self.store.cancel(third['id'],third['version'])
        with self.assertRaisesRegex(RuntimeErrorCode,'CHILD_LIMIT_EXCEEDED'):
            await self.child('four',binding=dict(self.parent_binding,max_children=32))
        self.assertEqual(await self.counts(),(4,4,3))

    async def test_depth_and_session_epoch_identity_cannot_be_forged(self):
        for altered,code in [(dict(self.parent_binding,session_id='other'),'SESSION_CHANGED'),
                             (dict(self.parent_binding,owner=[*self.binding[:4],99]),'STALE_OWNER')]:
            with self.assertRaisesRegex(RuntimeErrorCode,code):await self.child(binding=altered)
        with self.assertRaisesRegex(RuntimeErrorCode,'CHILD_IDENTITY_MISMATCH'):
            await self.child(descriptor=dict(self.descriptor,actor='other'))
        first = await self.child(binding=dict(self.parent_binding,max_depth=1))
        binding,context = await self.activate(first)
        with self.assertRaisesRegex(RuntimeErrorCode,'CHILD_DEPTH_EXCEEDED'):
            await self.child('nested',binding=dict(self.parent_binding,owner=list(binding),session_id=context.session_id))
        self.assertEqual(await self.counts(),(2,2,1))

    async def test_three_levels_execute_and_fourth_is_denied(self):
        binding = dict(self.parent_binding,max_children=8)
        for depth in range(1,4):
            child = await self.child(str(depth),binding=binding)
            owner,context = await self.activate(child)
            self.assertTrue(await self.verifier(context,'execute'))
            self.assertEqual((await self.store.get(child['id']))['family']['parent']['depth'],depth)
            binding = dict(binding,owner=list(owner),session_id=context.session_id)
        with self.assertRaisesRegex(RuntimeErrorCode,'CHILD_DEPTH_EXCEEDED'):
            await self.child('fourth',binding=binding)
        self.assertEqual(await self.counts(),(4,4,3))

    async def test_competing_keys_cannot_overbook_last_slot(self):
        binding = dict(self.parent_binding,max_children=1)
        results = await asyncio.gather(*(self.child(str(i),binding=binding) for i in range(4)),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,dict) for x in results),1)
        self.assertEqual([x.code for x in results if isinstance(x,RuntimeErrorCode)],['CHILD_LIMIT_EXCEEDED']*3)
        self.assertEqual(await self.counts(),(2,2,1))

    async def test_success_closes_queued_children_and_keeps_relation(self):
        child = await self.child()
        parent = await self.store.get(self.parent['id'])
        async with self.db.transaction(self.scope) as c:
            await self.store._event(c,parent,'fixture.success',{},await self.db.now(c),state='succeeded')
        result = await self.store.get(child['id'])
        self.assertEqual(result['state'],'cancelled')
        self.assertEqual(result['family']['parent']['parent_run_id'],parent['id'])

    async def test_family_metadata_respects_related_run_visibility(self):
        from app.runtime.child_authorizations import view as family_view
        child=await self.child()
        async with self.db.read(self.scope) as c:
            self.assertIsNone(await family_view(c,self.scope,child['id'],runs.c.id==child['id']))
            self.assertIsNone(await family_view(c,self.scope,self.parent['id'],runs.c.id==self.parent['id']))
            full=await family_view(c,self.scope,child['id'],runs.c.id.in_([child['id'],self.parent['id']]))
            self.assertEqual(full['parent']['parent_run_id'],self.parent['id'])

    async def test_input_cannot_forge_parent_or_erase_source_identity(self):
        with self.assertRaisesRegex(RuntimeErrorCode,'CHILD_BINDING_INVALID'):
            await self.store.submit('forged',dict(self.input(),parent_execution=self.parent_binding),authorization=self.descriptor)
        with self.assertRaisesRegex(RuntimeErrorCode,'CHILD_IDENTITY_MISMATCH'):
            await self.store.submit('foreign',dict(self.input(),principal={'id':'foreign'}),authorization=self.descriptor,parent_binding=self.parent_binding)
        self.assertEqual(await self.counts(),(1,1,0))

    async def test_restart_keeps_child_identity_and_parent_close_policy(self):
        from app.runtime.database import Database
        from app.runtime.store import RuntimeStore
        child=await self.child()
        await self.db.close()
        self.db=Database(self.url);self.store=RuntimeStore(self.db,self.scope)
        restored=await self.store.get(child['id'])
        self.assertEqual(restored['family']['parent']['parent_attempt_id'],self.binding[1])
        parent=await self.store.get(self.parent['id'])
        await self.store.cancel(parent['id'],parent['version'])
        self.assertEqual((await self.store.get(child['id']))['state'],'cancelled')

    async def test_spawn_permission_and_child_capabilities_are_intersections(self):
        expanded = dict(self.descriptor,permissions={k:[*v,dict(permission(self.descriptor,'execute'),resource_id='other')]
            for k,v in self.descriptor['permissions'].items()})
        with self.assertRaisesRegex(RuntimeErrorCode,'CHILD_PERMISSION_EXPANSION'):await self.child(descriptor=expanded)
        async with self.db.transaction(self.scope) as c:
            permissions = {k:[p for p in v if p['action']!='runtime.spawn'] for k,v in self.descriptor['permissions'].items()}
            await c.execute(work.update().where(work.c.run_id==self.parent['id']).values(permissions=permissions))
        with self.assertRaisesRegex(RuntimeErrorCode,'CHILD_SPAWN_DENIED'):await self.child()
        self.assertEqual(await self.counts(),(1,1,0))

    async def test_parent_cancel_cascades_without_claiming_active_process_stopped(self):
        active = await self.child('active');binding,context = await self.activate(active)
        queued = await self.child('queued')
        parent = await self.store.get(self.parent['id'])
        await self.store.cancel(parent['id'],parent['version'])
        current = await self.store.get(active['id'])
        self.assertEqual(current['state'],'executing');self.assertTrue(current['cancel_requested'])
        self.assertEqual((await self.store.get(queued['id']))['state'],'cancelled')
        with self.assertRaisesRegex(RuntimeErrorCode,'PARENT_AUTHORIZATION_INVALID'):
            await self.verifier(context,'execute')
        await self.store.confirm_cancel(*binding,'fixture:process-exited')
        self.assertEqual((await self.store.get(active['id']))['state'],'cancelled')

    async def test_parent_lease_loss_blocks_child_and_expiry_cascades(self):
        child = await self.child();_,context = await self.activate(child)
        async with self.db.transaction(self.scope) as c:
            await c.execute(attempts.update().where(attempts.c.id==self.binding[1]).values(lease_until=0))
        with self.assertRaisesRegex(RuntimeErrorCode,'PARENT_AUTHORIZATION_INVALID'):
            await self.verifier(context,'execute')
        self.assertEqual(await self.store.expire(),1)
        self.assertTrue((await self.store.get(child['id']))['cancel_requested'])

    async def test_parent_revoke_and_terminal_completion_cancel_descendants(self):
        first = await self.child('first');binding,context = await self.activate(first)
        grand = await self.child('grand',binding=dict(self.parent_binding,owner=list(binding),session_id=context.session_id))
        parent = await self.store.get(self.parent['id'])
        await self.store.revoke(parent['id'],parent['version'])
        self.assertTrue((await self.store.get(first['id']))['cancel_requested'])
        self.assertEqual((await self.store.get(grand['id']))['state'],'cancelled')

    async def test_parent_terminal_failure_and_cancel_audit_are_atomic(self):
        child = await self.child()
        original = self.store._event
        async def failing(c,run,kind,detail,now,**changes):
            if kind=='run.parent_cancel_requested':raise RuntimeErrorCode('AUDIT_UNAVAILABLE')
            return await original(c,run,kind,detail,now,**changes)
        parent = await self.store.get(self.parent['id'])
        with patch.object(self.store,'_event',side_effect=failing):
            with self.assertRaisesRegex(RuntimeErrorCode,'AUDIT_UNAVAILABLE'):
                await self.store.cancel(parent['id'],parent['version'])
        self.assertFalse((await self.store.get(parent['id']))['cancel_requested'])
        self.assertEqual((await self.store.get(child['id']))['state'],'queued')
        async with self.db.transaction(self.scope) as c:
            await original(c,parent,'fixture.terminal',{},await self.db.now(c),state='failed')
        self.assertEqual((await self.store.get(child['id']))['state'],'cancelled')

    async def test_failed_admission_rolls_back_relation_and_count_and_backup_preserves_it(self):
        with patch('app.runtime.work_authorizations.audit',side_effect=RuntimeErrorCode('AUDIT_UNAVAILABLE')):
            with self.assertRaisesRegex(RuntimeErrorCode,'AUDIT_UNAVAILABLE'):await self.child()
        self.assertEqual(await self.counts(),(1,1,0))
        await self.child()
        target = self.root/'backup'
        self.provider.root.mkdir(exist_ok=True)
        await asyncio.to_thread(backup_sqlite,self.url,self.provider.root,target)
        result = await asyncio.to_thread(verify_backup,target)
        self.assertIn('runtime_child_schema',result['schemas'])
