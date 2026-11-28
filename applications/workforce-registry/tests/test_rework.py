import json
import unittest

import test_workitems as fixtures
from dios_workforce.registry import Registry, RegistryError, encoded
from dios_workforce.workitems import WorkItems


class ReworkTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.WorkItemTests.setUp
    tearDown=fixtures.WorkItemTests.tearDown
    prepare=fixtures.WorkItemTests.prepare

    async def returned(self):
        await self.prepare()
        value=await self.work.dispatch('request',self.client,expected_version=2)
        import hashlib
        blob=b'review evidence'
        artifact={'id':'result','size':len(blob),'digest':hashlib.sha256(blob).hexdigest()}
        async def get(run):return {'id':run,'state':'succeeded','artifacts':[artifact]}
        async def content(*args):return blob
        self.reviewer.get=get;self.reviewer.artifact=content
        value=await self.work.observe('request',self.reviewer,expected_version=value['version'])
        return await self.work.confirm('request',self.reviewer,'reject','补充边界分析',expected_version=value['version'])

    async def test_rework_replay_conflict_and_unknown_dispatch_survive_restart(self):
        old=await self.returned()
        new=await self.work.rework('request',self.reviewer,self.client,'补充后的目标','rework-1',expected_version=old['version'])
        self.assertEqual(new['round_number'],2)
        self.client.fail=True
        with self.assertRaises(TimeoutError):await self.work.dispatch('request',self.client,expected_version=new['version'])
        self.registry.close();self.registry=Registry(self.path);self.work=WorkItems(self.registry)
        self.assertEqual(await self.work.rework('request',self.reviewer,self.client,'补充后的目标','rework-1',expected_version=old['version']),new)
        with self.assertRaisesRegex(RegistryError,'REWORK_IDEMPOTENCY_CONFLICT'):
            await self.work.rework('request',self.reviewer,self.client,'另一个目标','rework-1',expected_version=old['version'])
        current=await self.work.dispatch('request',self.client,expected_version=new['version'])
        self.assertEqual(len(self.client.runs),2)
        self.assertNotEqual(old['run_id'],current['run_id'])
        self.assertEqual(self.work.history('request')['rounds'][0],old)
        self.assertEqual(self.client.requests[-1],self.client.requests[-2])
        self.assertEqual(self.client.requests[-1]['goal'],'补充后的目标')
        with self.assertRaisesRegex(RegistryError,'WORK_ITEM_VERSION_CONFLICT'):
            await self.work.confirm('request',self.reviewer,'accept','旧轮消息',expected_version=old['version']-1)
        replay=await self.work.create('request','工作项','review','first-item','目标',self.reviewer)
        self.assertEqual(replay['round_number'],2)

    async def test_only_returned_work_can_start_new_round(self):
        await self.prepare()
        with self.assertRaisesRegex(RegistryError,'WORK_ITEM_NOT_RETURNED'):
            await self.work.rework('request',self.reviewer,self.client,'新目标','one',expected_version=2)
        self.assertEqual(len(self.work.history('request')['rounds']),1)

    async def test_legacy_pending_submission_keeps_first_round_key(self):
        await self.prepare()
        self.client.fail=True
        with self.assertRaises(TimeoutError):await self.work.dispatch('request',self.client,expected_version=2)
        before=self.work.get('request')
        # Exact v0.2 table layout, retaining its original pending employee receipt.
        with self.registry.db:
            self.registry.db.execute('ALTER TABLE work_items RENAME TO work_items_new')
            self.registry.db.execute('''CREATE TABLE work_items (
                id TEXT PRIMARY KEY, specification TEXT NOT NULL, reviewer TEXT NOT NULL,
                state TEXT NOT NULL, version INTEGER NOT NULL, employee TEXT, employee_revision INTEGER,
                run_id TEXT, evidence TEXT, decision TEXT)''')
            self.registry.db.execute('INSERT INTO work_items SELECT id,specification,reviewer,state,version,employee,employee_revision,run_id,evidence,decision FROM work_items_new')
            self.registry.db.execute('DROP TABLE work_items_new')
            self.registry.db.execute('DROP TABLE work_rounds');self.registry.db.execute('DROP TABLE rework_receipts')
            self.registry.db.execute('PRAGMA user_version=1')
        self.registry.close();self.registry=Registry(self.path);self.work=WorkItems(self.registry)
        self.assertEqual(self.work.get('request'),before)
        value=await self.work.dispatch('request',self.client,expected_version=2)
        self.assertEqual(len(self.client.runs),1)
        self.assertEqual(value['round_number'],1)
        self.registry.close();self.registry=Registry(self.path)
        self.assertEqual(self.registry.db.execute('PRAGMA user_version').fetchone()[0],5)


class ReworkIntegrationTests(unittest.IsolatedAsyncioTestCase):
    save=fixtures.WorkItemIntegrationTests.save
    headers_for=fixtures.WorkItemIntegrationTests.headers_for
    finish=fixtures.WorkItemIntegrationTests.finish
    asyncSetUp=fixtures.WorkItemIntegrationTests.asyncSetUp
    asyncTearDown=fixtures.WorkItemIntegrationTests.asyncTearDown
    run_task=fixtures.WorkItemIntegrationTests.run_task

    async def returned(self):
        value=await self.run_task()
        value=await self.work.observe('work-001',self.reviewer,expected_version=value['version'])
        return await self.work.confirm('work-001',self.reviewer,'reject','需补充边界条件',expected_version=value['version'])

    async def test_two_real_runs_have_separate_authority_and_preserved_decisions(self):
        old=await self.returned()
        self.registry.revise('employee-a','assistant',1,{'name':'改进后的助手'},expected_version=2)
        await self.registry.publish('employee-a',self.api,expected_version=3)
        current=await self.work.rework('work-001',self.reviewer,self.api,'按反馈补充分析','round-2',expected_version=old['version'])
        self.assertEqual(current['employee_revision'],2)
        current=await self.work.dispatch('work-001',self.api,expected_version=current['version'])
        self.assertNotEqual(old['run_id'],current['run_id'])
        first=await self.console.store.get(old['run_id']);second=await self.console.store.get(current['run_id'])
        self.assertNotEqual(first['input']['work_authorization_ref'],second['input']['work_authorization_ref'])
        await self.finish()
        # Sharing the first Run does not grant the second Run.
        with self.assertRaises(RegistryError):
            await self.work.observe('work-001',self.reviewer,expected_version=current['version'])
        self.directory['principals']['c']['objects'][current['run_id']]=['run.read','artifact.read'];self.save()
        current=await self.work.observe('work-001',self.reviewer,expected_version=current['version'])
        current=await self.work.confirm('work-001',self.reviewer,'accept','已核对补充内容',expected_version=current['version'])
        self.registry.close();self.registry=Registry(self.root/'workforce');self.work=WorkItems(self.registry)
        rounds=self.work.history('work-001')['rounds']
        self.assertEqual(rounds,[old,current])
        self.assertEqual([r['decision']['decision'] for r in rounds],['reject','accept'])
        self.assertEqual([r['employee_revision'] for r in rounds],[1,2])

    async def test_revoked_review_access_and_disabled_employee_block_rework(self):
        old=await self.returned()
        grants=self.directory['principals']['c']['objects'].copy()
        self.directory['principals']['c']['objects'].clear();self.save()
        with self.assertRaises(RegistryError):
            await self.work.rework('work-001',self.reviewer,self.api,'new','round-2',expected_version=old['version'])
        self.directory['principals']['c']['objects']=grants;self.save()
        await self.registry.disable('employee-a',self.admin,expected_version=2)
        with self.assertRaisesRegex(RegistryError,'EMPLOYEE_NOT_ACTIVE'):
            await self.work.rework('work-001',self.reviewer,self.api,'new','round-2',expected_version=old['version'])
        self.assertEqual(self.work.history('work-001')['rounds'],[old])

    async def test_new_round_still_rechecks_live_e2ag_at_dispatch(self):
        old=await self.returned()
        current=await self.work.rework('work-001',self.reviewer,self.api,'new','round-2',expected_version=old['version'])
        self.directory['principals']['a']['profiles']['review']['permissions']=[];self.save()
        with self.assertRaises(RegistryError):
            await self.work.dispatch('work-001',self.api,expected_version=current['version'])
        self.assertIsNone(self.work.get('work-001')['run_id'])
        self.assertEqual(self.work.history('work-001')['rounds'][0],old)
