import json
import unittest

import test_workitems as f
import test_interventions as interventions
from dios_workforce.compensations import Compensations
from dios_workforce.groups import WorkGroups
from dios_workforce.registry import Registry, RegistryError
from dios_workforce.workitems import WorkItems


class CompensationTests(unittest.IsolatedAsyncioTestCase):
    setUp=f.WorkItemTests.setUp
    tearDown=f.WorkItemTests.tearDown
    prepare=f.WorkItemTests.prepare

    async def ready(self):
        await self.prepare();await self.work.dispatch('request',self.client,expected_version=2)
        async def failed(run):return {'id':run,'state':'failed','artifacts':[]}
        self.reviewer.get=failed
        async def tools(run):return {'items':[],'at_limit':False}
        self.reviewer.tools=tools
        await self.work.observe('request',self.reviewer,expected_version=3)
        await self.work.create('undo','补偿','review','first-item','恢复已登记资源',self.reviewer)
        self.work.bind('undo','employee',expected_version=1)
        self.comp=Compensations(self.work)

    async def test_compensation_plan_replay_restart_assignment_and_recursion_fences(self):
        await self.ready()
        first=await self.comp.plan('request','undo',self.reviewer,'补偿已登记副作用','c1',expected_version=4)
        self.assertEqual(first['work_item']['state'],'compensation_pending')
        self.registry.close();self.registry=Registry(self.path);self.work=WorkItems(self.registry);self.comp=Compensations(self.work)
        again=await self.comp.plan('request','undo',self.reviewer,'补偿已登记副作用','c1',expected_version=4)
        self.assertEqual(first,again)
        self.assertEqual(sum(e['kind']=='compensation_planned' for e in again['events']),1)
        with self.assertRaisesRegex(RegistryError,'COMPENSATION_CONFLICT'):
            await self.comp.plan('request','undo',self.reviewer,'changed','c1',expected_version=4)
        with self.assertRaisesRegex(RegistryError,'COMPENSATION_ASSIGNMENT_FROZEN'):
            self.work.bind('undo','employee',expected_version=2)
        with self.assertRaisesRegex(RegistryError,'COMPENSATION_ALREADY_ASSIGNED'):
            await self.comp.plan('undo','request',self.reviewer,'nested','c2',expected_version=2)
        with self.assertRaisesRegex(RegistryError,'COMPENSATION_NOT_CONFIRMED'):
            await self.comp.confirm('request',self.reviewer,'done',expected_version=5)
        await self.work.create('other','other','review','first-item','goal',self.reviewer)
        self.work.bind('other','employee',expected_version=1)
        group=WorkGroups(self.work);await group.create('parent','parent','goal',self.reviewer)
        with self.assertRaisesRegex(RegistryError,'COMPENSATION_ALREADY_ASSIGNED'):
            await group.split('parent',['undo','other'],self.reviewer,expected_version=1)

    async def test_unsettled_source_and_wrong_reviewer_cannot_start_compensation(self):
        await self.ready()
        async def running(run):return {'id':run,'state':'running'}
        self.reviewer.get=running
        with self.assertRaisesRegex(RegistryError,'SOURCE_NOT_SETTLED'):
            await self.comp.plan('request','undo',self.reviewer,'reason','key',expected_version=4)
        self.assertEqual(self.work.get('request')['state'],'execution_failed')
        self.assertEqual(self.comp.history('request')['compensations'],[])
        with self.assertRaisesRegex(RegistryError,'REVIEWER_IDENTITY_MISMATCH'):
            await self.comp.plan('request','undo',self.client,'reason','key',expected_version=4)

    async def test_unknown_effect_partial_observation_and_failed_compensation_stay_open(self):
        await self.ready()
        for page,error in [({'items':[],'at_limit':True},'SOURCE_TOOL_OBSERVATION_INCOMPLETE'),
            ({'items':[{'run_id':self.work.get('request')['run_id'],'state':'result_unknown'}]},'SOURCE_TOOL_RECONCILIATION_REQUIRED')]:
            async def tools(run):return page
            self.reviewer.tools=tools
            with self.assertRaisesRegex(RegistryError,error):
                await self.comp.plan('request','undo',self.reviewer,'reason','key',expected_version=4)
            self.assertEqual(self.work.get('request')['state'],'execution_failed')
        async def empty(run):return {'items':[],'at_limit':False}
        self.reviewer.tools=empty
        await self.comp.plan('request','undo',self.reviewer,'reason','key',expected_version=4)
        await self.work.dispatch('undo',self.client,expected_version=2)
        await self.work.observe('undo',self.reviewer,expected_version=3)
        with self.assertRaisesRegex(RegistryError,'COMPENSATION_NOT_CONFIRMED'):
            await self.comp.confirm('request',self.reviewer,'done',expected_version=5)
        self.assertEqual(self.work.get('request')['state'],'compensation_pending')


class CompensationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=interventions.InterventionIntegrationTests.asyncSetUp
    asyncTearDown=interventions.InterventionIntegrationTests.asyncTearDown
    save=interventions.InterventionIntegrationTests.save
    headers_for=interventions.InterventionIntegrationTests.headers_for
    dispatch=interventions.InterventionIntegrationTests.dispatch
    finish=interventions.InterventionIntegrationTests.finish

    async def test_compensation_preserves_real_revoked_approval_and_requires_fresh_result_access(self):
        # Reuse the real approval fixture and its assertions, then join its history to compensation.
        await interventions.InterventionIntegrationTests.test_real_approval_and_revocation_are_linked_without_business_completion(self)
        source=self.work.get('work-001');store=self.console.store;run=await store.get(source['run_id'])
        await interventions.API(self,'a').cancel(run['id'],expected_version=run['version'])
        attempt=run['attempts'][0]
        await store.confirm_cancel(run['id'],attempt['id'],'approval-worker','instance',attempt['epoch'],'exit-evidence')
        source=await self.work.observe('work-001',self.reviewer,expected_version=source['version'])
        await self.work.create('undo','补偿','review','first-item','恢复已登记资源',self.reviewer)
        self.work.bind('undo','employee-b',expected_version=1)
        comp=Compensations(self.work)
        with self.assertRaises(RegistryError):
            await comp.plan('work-001','undo',self.reviewer,'原执行已取消，核查副作用','c1',expected_version=source['version'])
        self.directory['principals']['c']['objects'][source['run_id']]=['run.read','tool.read'];self.save()
        linked=await comp.plan('work-001','undo',self.reviewer,'原执行已取消，核查副作用','c1',expected_version=source['version'])
        target=await self.work.dispatch('undo',interventions.API(self,'b'),expected_version=2)
        await self.finish()
        with self.assertRaisesRegex(RegistryError,'COMPENSATION_NOT_CONFIRMED'):
            await comp.confirm('work-001',self.reviewer,'核对',expected_version=linked['work_item']['version'])
        self.directory['principals']['c']['objects'][target['run_id']]=['run.read','artifact.read'];self.save()
        target=await self.work.observe('undo',self.reviewer,expected_version=target['version'])
        target=await self.work.confirm('undo',self.reviewer,'accept','补偿成果已核验',expected_version=target['version'])
        self.directory['principals']['c']['objects'][target['run_id']]=['run.read'];self.save()
        with self.assertRaises(RegistryError):
            await comp.confirm('work-001',self.reviewer,'核对',expected_version=linked['work_item']['version'])
        self.assertEqual(self.work.get('work-001')['state'],'compensation_pending')
        self.directory['principals']['c']['objects'][target['run_id']]=['run.read','artifact.read'];self.save()
        closed=await comp.confirm('work-001',self.reviewer,'核对',expected_version=linked['work_item']['version'])
        self.assertEqual(closed['work_item']['state'],'compensated')
        self.assertEqual(closed['approvals'][0]['approval_state'],'invalidated')
        self.assertEqual(closed['compensations'][0]['source_snapshot']['run_id'],source['run_id'])
        self.assertEqual(closed['compensations'][0]['work_item']['work_item']['run_id'],target['run_id'])
        replay=await comp.confirm('work-001',self.reviewer,'核对',expected_version=linked['work_item']['version'])
        self.assertEqual(replay,closed)
        with self.assertRaisesRegex(RegistryError,'DECISION_CONFLICT'):
            await comp.confirm('work-001',self.reviewer,'改判',expected_version=linked['work_item']['version'])
