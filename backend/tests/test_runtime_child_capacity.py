"""Root claims reserve bounded subtrees atomically; children use reserved slots."""
import asyncio
from dataclasses import replace
import os
from uuid import uuid4
import unittest
from app.runtime.capacity import Capacity
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.store import RuntimeStore
from app.runtime.authorization import WorkAuthorizationContext
from test_runtime_children import ChildRunTests


class ChildCapacityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ChildRunTests.asyncSetUp
    asyncTearDown=ChildRunTests.asyncTearDown
    input=ChildRunTests.input
    child=ChildRunTests.child

    async def finish_parent(self):
        from app.runtime.adapter import Observation
        observation=Observation(self.binding[1],"exited",False,None,"fixture-process",1,"not_configured","fixture-host")
        await self.store.observe_process(self.binding,observation,adapter="test-lifecycle",boot_id="boot")
        await self.store.finish_failed(self.binding,"fixture-complete")

    async def activate(self, run):
        worker_id=str(uuid4())
        await self.store.register_worker(worker_id,'instance','host',os.getpid())
        attempt=await self.store.claim(worker_id,'instance',profile_id='test-profile',child_capacity=self.plan())
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt['run_id'],run['id'])
        binding=(run['id'],attempt['id'],worker_id,'instance',attempt['epoch'])
        context=WorkAuthorizationContext(self.scope,run['id'],attempt['id'],worker_id,'instance',attempt['epoch'],attempt['authorization_epoch'],None,run['input']['work_authorization_ref'])
        await self.verifier(context,'execute')
        await self.store.report(*binding,1,'preparing')
        await self.store.report(*binding,2,'creating_session')
        context=replace(context,session_id='session-'+worker_id)
        await self.verifier(context,'execute')
        await self.store.report(*binding,3,'waiting_model',session_id=context.session_id)
        return binding,context

    def plan(self):
        return dict(max_children=3,profiles={'test-profile':self.spec.fingerprint()})

    async def submit_root(self,key):
        return await self.store.submit(key,dict(self.input(),child_capacity_request=self.plan()),authorization=self.descriptor)

    async def claim_new(self, store=None):
        store=store or self.store
        worker=str(uuid4())
        await store.register_worker(worker,'instance','host',os.getpid())
        return await store.claim(worker,'instance',profile_id='test-profile',child_capacity=self.plan())

    async def test_multiple_parents_leave_slots_for_descendants(self):
        cap=Capacity(self.store)
        await cap.put('all',{'kind':'scope'},4,0)
        second=await self.submit_root('second')
        self.assertIsNone(await self.claim_new())
        status=(await cap.status())['items'][0]
        self.assertEqual((status['reserved_attempts'],status['tree_reserved_slots'],status['available']),(1,3,0))
        first=await self.child('child-1')
        from app.runtime.capacity import queue_status
        async with self.db.read(self.scope) as c:
            self.assertFalse((await queue_status(c,self.scope,first))['blocked'])
            self.assertTrue((await queue_status(c,self.scope,second))['blocked'])
        binding,context=await self.activate(first)
        nested=dict(self.parent_binding,owner=list(binding),session_id=context.session_id)
        second_child=await self.child('child-2',binding=nested)
        await self.activate(second_child)
        third=await self.child('child-3')
        await self.activate(third)
        status=(await cap.status())['items'][0]
        self.assertEqual((status['reserved_attempts'],status['tree_reserved_slots'],status['available']),(4,0,0))
        self.assertEqual((await self.store.get(second['id']))['state'],'queued')

    async def test_unknown_or_cancel_requested_parent_retains_reservation(self):
        cap=Capacity(self.store)
        await cap.put('all',{'kind':'scope'},4,0)
        await self.submit_root('other')
        current=await self.store.get(self.parent['id'])
        await self.store.cancel(current['id'],current['version'])
        self.assertIsNone(await self.claim_new())
        await self.store.mark_unknown(self.binding,'fixture-disconnected')
        status=(await cap.status())['items'][0]
        self.assertEqual((status['reserved_attempts'],status['tree_reserved_slots'],status['unresolved_attempts']),(1,3,1))
        self.assertIsNone(await self.claim_new())

    async def test_terminal_root_releases_unused_reservation(self):
        cap=Capacity(self.store)
        await cap.put('all',{'kind':'scope'},4,0)
        second=await self.submit_root('second')
        await self.finish_parent()
        attempt=await self.claim_new()
        self.assertEqual(attempt['run_id'],second['id'])
        status=(await cap.status())['items'][0]
        self.assertEqual((status['reserved_attempts'],status['tree_reserved_slots']),(1,3))

    async def test_independent_connections_cannot_reserve_same_spare_slots(self):
        await self.finish_parent()
        await Capacity(self.store).put('all',{'kind':'scope'},8,0)
        for n in range(8):await self.submit_root('root-'+str(n))
        url=self.db.engine.url.render_as_string(hide_password=False)
        dbs=[Database(url) for _ in range(8)]
        try:
            claims=await asyncio.gather(*(self.claim_new(RuntimeStore(db,self.scope)) for db in dbs))
            self.assertEqual(sum(c is not None for c in claims),2)
            status=(await Capacity(self.store).status())['items'][0]
            self.assertEqual((status['reserved_attempts'],status['tree_reserved_slots']),(2,6))
        finally:
            for db in dbs:await db.close()

    async def test_future_child_profile_limit_is_reserved_before_parent_launch(self):
        await self.finish_parent()
        await Capacity(self.store).put('leaf',{'kind':'profiles','profiles':['leaf']},2,0)
        await self.submit_root('other')
        worker=str(uuid4())
        await self.store.register_worker(worker,'instance','host',os.getpid())
        plan=dict(max_children=3,profiles={'leaf':'a'*64})
        self.assertIsNone(await self.store.claim(worker,'instance',profile_id='test-profile',child_capacity=plan))
        await Capacity(self.store).put('leaf',{'kind':'profiles','profiles':['leaf']},3,1)
        self.assertIsNotNone(await self.store.claim(worker,'instance',profile_id='test-profile',child_capacity=plan))
        with self.assertRaisesRegex(RuntimeErrorCode,'CHILD_CAPACITY_PROFILE_CHANGED'):
            # This active parent reservation permits only the pinned leaf profile.
            from app.runtime.child_capacity import check_child
            async with self.db.transaction(self.scope) as c:
                run=await self.store._run(c,self.parent['id'])
                roots=await __import__('app.runtime.child_capacity',fromlist=['reservations']).reservations(c,self.scope)
                await check_child(c,self.scope,roots[0]['root'],run)

@unittest.skipUnless(os.environ.get('DIOS_TEST_POSTGRES_URL'),'isolated PostgreSQL required')
class PostgresChildCapacityTests(ChildCapacityTests):
    database_url=os.environ.get('DIOS_TEST_POSTGRES_URL')
