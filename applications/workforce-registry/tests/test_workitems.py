import json
import subprocess
import sys
import unittest

import test_registry as fixtures
import test_runtime_integration as runtime
from dios_workforce.registry import Registry, RegistryError
from dios_workforce.workitems import WorkItems


class WorkItemTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.RegistryTests.setUp
    tearDown=fixtures.RegistryTests.tearDown

    async def prepare(self):
        await self.registry.publish('employee',self.client,expected_version=1)
        self.reviewer=fixtures.Client();self.reviewer.owner=dict(fixtures.IDENTITY,id='human',subject='human:reviewer',actor='human:reviewer')
        self.work=WorkItems(self.registry)
        await self.work.create('request','工作项','review','first-item','目标',self.reviewer)
        self.work.bind('request','employee',expected_version=1)

    async def test_duplicate_create_dispatch_recovery_and_process_restart(self):
        await self.prepare()
        self.client.fail=True
        with self.assertRaises(TimeoutError):await self.work.dispatch('request',self.client,expected_version=2)
        self.assertEqual(self.work.get('request')['state'],'dispatching')
        self.registry.close();self.registry=Registry(self.path);self.work=WorkItems(self.registry)
        value=await self.work.dispatch('request',self.client,expected_version=2)
        self.assertEqual(value['state'],'running');self.assertEqual(len(self.client.runs),1)
        replay=await self.work.create('request','工作项','review','first-item','目标',self.reviewer)
        self.assertEqual(replay,value)
        with self.assertRaisesRegex(RegistryError,'IDEMPOTENCY_CONFLICT'):
            await self.work.create('request','不同','review','first-item','目标',self.reviewer)
        self.registry.close()
        try:
            result=subprocess.run([sys.executable,'-m','dios_workforce.work_cli','--state-dir',str(self.path),
                'show','--id','request'],capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(result.stdout)['work_item']['run_id'],value['run_id'])
        finally:self.registry=Registry(self.path)

    async def test_bound_publication_cannot_silently_change(self):
        await self.prepare()
        self.registry.revise('employee','assistant',1,{'name':'新版'},expected_version=2)
        await self.registry.publish('employee',self.client,expected_version=3)
        with self.assertRaisesRegex(RegistryError,'EMPLOYEE_PUBLICATION_CHANGED'):
            await self.work.dispatch('request',self.client,expected_version=2)
        self.assertEqual(self.client.requests,[])

    async def test_timeout_and_invalid_result_keep_pending_business_state(self):
        await self.prepare();value=await self.work.dispatch('request',self.client,expected_version=2)
        async def timed_out(run):raise TimeoutError()
        self.reviewer.get=timed_out
        with self.assertRaises(TimeoutError):await self.work.observe('request',self.reviewer,expected_version=value['version'])
        self.assertEqual(self.work.get('request'),value)
        async def result(run):return {'id':run,'state':'succeeded','artifacts':[]}
        self.reviewer.get=result
        with self.assertRaisesRegex(RegistryError,'EVIDENCE_REQUIRED'):
            await self.work.observe('request',self.reviewer,expected_version=value['version'])
        self.assertIsNone(self.work.get('request')['decision'])
        async def failed(run):return {'id':run,'state':'failed','artifacts':[]}
        self.reviewer.get=failed
        failed_item=await self.work.observe('request',self.reviewer,expected_version=value['version'])
        self.assertEqual(failed_item['state'],'execution_failed')
        with self.assertRaisesRegex(RegistryError,'NOT_READY_FOR_CONFIRMATION'):
            await self.work.confirm('request',self.reviewer,'accept','同意',expected_version=failed_item['version'])


class API(runtime.API):
    async def get(self,run_id):return await self.request('GET','runs/'+run_id)
    async def artifact(self,run_id,artifact_id):
        response=await self.owner.client.get('/api/os/runtime/runs/'+run_id+'/artifacts/'+artifact_id+'/download',
            headers=self.owner.headers_for(self.key))
        if response.status_code!=200:raise RegistryError('ARTIFACT_ACCESS_DENIED')
        return response.content


class WorkItemIntegrationTests(unittest.IsolatedAsyncioTestCase):
    save=runtime.RuntimeIntegrationTests.save
    headers_for=runtime.RuntimeIntegrationTests.headers_for
    finish=runtime.RuntimeIntegrationTests.finish
    asyncTearDown=runtime.RuntimeIntegrationTests.asyncTearDown

    async def asyncSetUp(self):
        await runtime.RuntimeIntegrationTests.asyncSetUp(self)
        self.work=WorkItems(self.registry);self.reviewer=API(self,'c')
        await self.work.create('work-001','分析边界','review','first-item','检查中文证据',self.reviewer)
        self.work.bind('work-001','employee-a',expected_version=1)

    async def run_task(self):
        value=await self.work.dispatch('work-001',self.api,expected_version=2)
        self.directory['principals']['c']['objects'][value['run_id']]=['run.read','artifact.read'];self.save()
        await self.finish()
        return value

    async def test_success_requires_explicit_human_decision_and_keeps_evidence(self):
        value=await self.run_task()
        self.assertEqual(self.work.get('work-001')['state'],'running')
        value=await self.work.observe('work-001',self.reviewer,expected_version=value['version'])
        self.assertEqual(value['state'],'awaiting_confirmation');self.assertTrue(value['evidence']['artifacts'])
        with self.assertRaisesRegex(RegistryError,'REVIEWER_IDENTITY_MISMATCH'):
            await self.work.confirm('work-001',API(self,'a'),'accept','同意',expected_version=value['version'])
        accepted=await self.work.confirm('work-001',self.reviewer,'accept','已核对位置与来源',expected_version=value['version'])
        self.assertEqual(accepted['state'],'confirmed')
        self.assertEqual(await self.work.confirm('work-001',self.reviewer,'accept','已核对位置与来源',expected_version=value['version']),accepted)
        with self.assertRaisesRegex(RegistryError,'DECISION_CONFLICT'):
            await self.work.confirm('work-001',self.reviewer,'reject','改判',expected_version=value['version'])
        self.registry.close();self.registry=Registry(self.root/'workforce');self.work=WorkItems(self.registry)
        events=self.work.history('work-001')['events']
        self.assertEqual(sum(e['kind']=='human_decision' for e in events),1)
        self.assertEqual(self.work.get('work-001')['evidence'],accepted['evidence'])

    async def test_revoked_reviewer_access_cannot_confirm_cached_evidence(self):
        value=await self.run_task()
        value=await self.work.observe('work-001',self.reviewer,expected_version=value['version'])
        self.directory['principals']['c']['objects'].clear();self.save()
        with self.assertRaises(RegistryError):
            await self.work.confirm('work-001',self.reviewer,'accept','已核对',expected_version=value['version'])
        self.assertEqual(self.work.get('work-001')['state'],'awaiting_confirmation')

    async def test_corrupt_artifact_cannot_enter_review_and_reject_is_not_success(self):
        value=await self.run_task()
        original=self.reviewer.artifact
        async def corrupt(*args):return b'corrupt'
        self.reviewer.artifact=corrupt
        with self.assertRaisesRegex(RegistryError,'ARTIFACT_INTEGRITY_FAILED'):
            await self.work.observe('work-001',self.reviewer,expected_version=value['version'])
        self.reviewer.artifact=original
        value=await self.work.observe('work-001',self.reviewer,expected_version=value['version'])
        value=await self.work.confirm('work-001',self.reviewer,'reject','需要补充证据',expected_version=value['version'])
        self.assertEqual(value['state'],'needs_revision')

    async def test_self_review_stale_version_and_disabled_employee_rejected(self):
        await self.work.create('self','自审','review','first-item','goal',API(self,'a'))
        with self.assertRaisesRegex(RegistryError,'SELF_REVIEW_DENIED'):
            self.work.bind('self','employee-a',expected_version=1)
        with self.assertRaisesRegex(RegistryError,'VERSION_CONFLICT'):
            self.work.bind('work-001','employee-b',expected_version=1)
        await self.registry.disable('employee-a',self.admin,expected_version=2)
        with self.assertRaises(RegistryError):await self.work.dispatch('work-001',self.api,expected_version=2)
        self.assertIsNone(self.work.get('work-001')['run_id'])
