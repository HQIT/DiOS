import copy
import hashlib
import json
import secrets
import subprocess
import sys
import unittest

import test_registry as registry_fixtures
import test_workitems as fixtures
from dios_workforce.groups import WorkGroups
from dios_workforce.registry import Registry, RegistryError
from dios_workforce.workitems import WorkItems


class GroupTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.WorkItemTests.setUp
    tearDown=fixtures.WorkItemTests.tearDown
    prepare=fixtures.WorkItemTests.prepare

    async def ready(self):
        await self.prepare();self.groups=WorkGroups(self.work)
        second=registry_fixtures.Client();second.owner=dict(second.owner,id='agent-b',subject='subject:b',actor='actor:b')
        self.registry.revise('other','assistant',1,{'role':'验证'},expected_version=0,identity=second.owner)
        await self.registry.publish('other',second,expected_version=1)
        await self.work.create('child-b','验证结果','review','first-item','验证',self.reviewer)
        self.work.bind('child-b','other',expected_version=1)
        await self.groups.create('parent','汇总','综合分析',self.reviewer)

    async def test_fixed_children_replay_atomic_failure_and_no_cycles(self):
        await self.ready()
        with self.assertRaises(RegistryError):
            await self.groups.split('parent',['request','missing'],self.reviewer,expected_version=1)
        self.assertEqual(self.work.history('parent')['children'],[])
        self.assertEqual(self.work.get('parent')['version'],1)
        await self.groups.split('parent',['request','child-b'],self.reviewer,expected_version=1)
        self.registry.close();self.registry=Registry(self.path);self.work=WorkItems(self.registry);self.groups=WorkGroups(self.work)
        again=await self.groups.split('parent',['request','child-b'],self.reviewer,expected_version=1)
        self.assertEqual(len(again['children']),2)
        self.assertEqual(sum(e['kind']=='split' for e in again['events']),1)
        with self.assertRaisesRegex(RegistryError,'SPLIT_CONFLICT'):
            await self.groups.split('parent',['child-b','request'],self.reviewer,expected_version=1)
        with self.assertRaisesRegex(RegistryError,'SPLIT_ASSIGNMENT_FROZEN'):
            self.work.bind('request','other',expected_version=2)
        await self.groups.create('parent-2','另一汇总','goal',self.reviewer)
        with self.assertRaisesRegex(RegistryError,'CHILD_ALREADY_ASSIGNED'):
            await self.groups.split('parent-2',['request','child-b'],self.reviewer,expected_version=1)
        with self.assertRaisesRegex(RegistryError,'CHILD_NOT_READY_FOR_SPLIT'):
            await self.groups.split('parent-2',['parent-2','parent'],self.reviewer,expected_version=1)

    async def test_parent_never_dispatches_and_pending_children_never_pass(self):
        await self.ready()
        await self.groups.split('parent',['request','child-b'],self.reviewer,expected_version=1)
        with self.assertRaisesRegex(RegistryError,'WORK_ITEM_NOT_DISPATCHABLE'):
            await self.work.dispatch('parent',self.client,expected_version=2)
        with self.assertRaisesRegex(RegistryError,'CHILD_WORK_NOT_CONFIRMED'):
            await self.groups.collect('parent',self.reviewer,expected_version=2)
        with self.assertRaisesRegex(RegistryError,'GROUP_NOT_READY_FOR_REVIEW'):
            await self.groups.confirm('parent',self.reviewer,'accept','yes',expected_version=2)
        self.assertEqual(self.work.get('parent')['state'],'coordinating')
        self.assertEqual(self.client.requests,[])


class GroupIntegrationTests(unittest.IsolatedAsyncioTestCase):
    save=fixtures.WorkItemIntegrationTests.save
    headers_for=fixtures.WorkItemIntegrationTests.headers_for
    finish=fixtures.WorkItemIntegrationTests.finish
    asyncTearDown=fixtures.WorkItemIntegrationTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.WorkItemIntegrationTests.asyncSetUp(self)
        # Separate parent reviewer, leaf reviewer and two employee identities.
        self.tokens['d']=secrets.token_urlsafe(32)
        self.directory['principals']['d']=copy.deepcopy(self.directory['principals']['c'])
        self.directory['principals']['d'].update(subject='subject:d',actor='service:d')
        self.directory['principals']['d']['tokens'][0]['sha256']=hashlib.sha256(self.tokens['d'].encode()).hexdigest();self.save()
        self.parent_reviewer=fixtures.API(self,'d');self.groups=WorkGroups(self.work)
        self.registry.revise('employee-b','assistant',1,{'role':'独立验证'},expected_version=2)
        await self.registry.publish('employee-b',fixtures.API(self,'b'),expected_version=3)
        await self.work.create('work-002','验证边界','review','first-item','独立核对',self.reviewer)
        self.work.bind('work-002','employee-b',expected_version=1)
        await self.groups.create('parent','综合结果','汇总两个岗位的分析',self.parent_reviewer)
        await self.groups.split('parent',['work-001','work-002'],self.parent_reviewer,expected_version=1)

    async def complete_children(self, decisions=('accept','accept')):
        self.results=[]
        for (work_id,employee),decision in zip([('work-001','a'),('work-002','b')],decisions):
            value=await self.work.dispatch(work_id,fixtures.API(self,employee),expected_version=2)
            await self.finish()
            self.directory['principals']['c']['objects'][value['run_id']]=['run.read','artifact.read'];self.save()
            value=await self.work.observe(work_id,self.reviewer,expected_version=value['version'])
            value=await self.work.confirm(work_id,self.reviewer,decision,'岗位结果已核对',expected_version=value['version'])
            self.results.append(value)

    async def test_two_roles_require_separate_parent_review_and_live_access(self):
        await self.complete_children()
        self.assertEqual(self.work.get('parent')['state'],'coordinating')
        with self.assertRaises(RegistryError):await self.groups.collect('parent',self.parent_reviewer,expected_version=2)
        for value in self.results:
            self.directory['principals']['d']['objects'][value['run_id']]=['run.read','artifact.read']
        self.save()
        parent=await self.groups.collect('parent',self.parent_reviewer,expected_version=2)
        self.assertEqual(parent['state'],'awaiting_group_review')
        self.assertEqual([c['id'] for c in parent['evidence']['children']],['work-001','work-002'])
        with self.assertRaisesRegex(RegistryError,'REVIEWER_IDENTITY_MISMATCH'):
            await self.groups.confirm('parent',self.reviewer,'accept','wrong reviewer',expected_version=3)
        self.directory['principals']['d']['objects'].pop(self.results[1]['run_id']);self.save()
        with self.assertRaises(RegistryError):await self.groups.confirm('parent',self.parent_reviewer,'accept','核对完成',expected_version=3)
        self.assertIsNone(self.work.get('parent')['decision'])
        self.directory['principals']['d']['objects'][self.results[1]['run_id']]=['run.read','artifact.read'];self.save()
        parent=await self.groups.confirm('parent',self.parent_reviewer,'accept','核对完成',expected_version=3)
        self.assertEqual(parent['state'],'group_confirmed')
        self.assertIsNone(parent['run_id'])
        self.assertEqual(await self.groups.confirm('parent',self.parent_reviewer,'accept','核对完成',expected_version=3),parent)
        self.registry.close()
        try:
            output=subprocess.run([sys.executable,'-m','dios_workforce.work_cli','--state-dir',str(self.root/'workforce'),
                'show','--id','parent'],capture_output=True,text=True,check=True)
            view=json.loads(output.stdout)
            self.assertEqual(view['work_item'],parent);self.assertEqual(len(view['children']),2)
        finally:self.registry=Registry(self.root/'workforce')

    async def test_parent_actor_cannot_self_review_and_split_cannot_expand_e2ag(self):
        await self.groups.create('self','自审汇总','goal',fixtures.API(self,'a'))
        # Fresh unassigned children allow the self-review check to run.
        await self.work.create('other-a','a','review','first-item','goal',self.reviewer)
        await self.work.create('other-b','b','review','first-item','goal',self.reviewer)
        self.work.bind('other-a','employee-a',expected_version=1);self.work.bind('other-b','employee-b',expected_version=1)
        with self.assertRaisesRegex(RegistryError,'SELF_REVIEW_DENIED'):
            await self.groups.split('self',['other-a','other-b'],fixtures.API(self,'a'),expected_version=1)
        self.directory['principals']['b']['profiles']['review']['permissions']=[];self.save()
        with self.assertRaises(RegistryError):
            await self.work.dispatch('work-002',fixtures.API(self,'b'),expected_version=2)
        self.assertIsNone(self.work.get('work-002')['run_id'])
        with self.assertRaisesRegex(RegistryError,'CHILD_WORK_NOT_CONFIRMED'):
            await self.groups.collect('parent',self.parent_reviewer,expected_version=2)

    async def test_child_rework_is_preserved_and_parent_rejection_does_not_rewrite_children(self):
        await self.complete_children(('reject','accept'))
        for value in self.results:
            self.directory['principals']['d']['objects'][value['run_id']]=['run.read','artifact.read']
        self.save()
        with self.assertRaisesRegex(RegistryError,'CHILD_WORK_NOT_CONFIRMED'):
            await self.groups.collect('parent',self.parent_reviewer,expected_version=2)
        child=await self.work.rework('work-001',self.reviewer,fixtures.API(self,'a'),'按退回意见修订','rework-one',expected_version=5)
        child=await self.work.dispatch('work-001',fixtures.API(self,'a'),expected_version=child['version'])
        await self.finish()
        for identity in ('c','d'):
            self.directory['principals'][identity]['objects'][child['run_id']]=['run.read','artifact.read']
        self.save()
        child=await self.work.observe('work-001',self.reviewer,expected_version=child['version'])
        await self.work.confirm('work-001',self.reviewer,'accept','修订已核对',expected_version=child['version'])
        parent=await self.groups.collect('parent',self.parent_reviewer,expected_version=2)
        self.assertEqual(parent['evidence']['children'][0]['round_number'],2)
        parent=await self.groups.confirm('parent',self.parent_reviewer,'reject','整体方案需重新规划',expected_version=parent['version'])
        self.assertEqual(parent['state'],'group_needs_revision')
        self.assertEqual(self.work.get('work-001')['state'],'confirmed')
        self.assertEqual(len(self.work.history('work-001')['rounds']),2)
        with self.assertRaisesRegex(RegistryError,'GROUP_REWORK_REQUIRES_REPLANNING'):
            await self.work.rework('parent',self.parent_reviewer,fixtures.API(self,'a'),'new','unsafe',expected_version=parent['version'])
