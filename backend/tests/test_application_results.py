import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from sqlalchemy import select
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'sdk/python/src'))
from dios_sdk import ToolChannelClient,WorkloadError
from app.runtime.database import RuntimeErrorCode
from app.runtime.store import encoded
from app.runtime.schema import artifacts
from app.runtime.reconciliation import reconcile_run
import test_runtime_worker as worker_fixtures
import test_runtime_worker_tool_channel as channel_fixtures

class ResultContractTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=worker_fixtures.WorkerTests.asyncSetUp
    asyncTearDown=worker_fixtures.WorkerTests.asyncTearDown
    worker=worker_fixtures.WorkerTests.worker
    async def submit(self,schema):
        return await self.store.submit('schema',{'runtime_profile':'test-profile','profile_digest':self.spec.fingerprint(),
            'payload':{},'input_registration':{'result_schema':schema}})
    async def test_valid_result_and_original_schema_succeed(self):
        await self.submit({'type':'object','required':['summary','evidence']})
        result=await self.worker(worker_fixtures.FakeAdapter()).once()
        self.assertEqual(result['state'],'succeeded')
    async def test_missing_application_field_is_not_success_and_raw_output_retained(self):
        run=await self.submit({'type':'object','required':['application_proof']})
        result=await self.worker(worker_fixtures.FakeAdapter()).once()
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['attempts'][0]['error']['code'],'EVIDENCE_INSUFFICIENT')
        async with self.db.read(self.scope) as c:
            rows=(await c.execute(select(artifacts).where(artifacts.c.run_id==run['id']))).all()
        self.assertEqual(len(rows),2)
    async def test_authoritative_completion_rejects_even_if_worker_validator_accepts(self):
        run=await self.submit({'type':'object','required':['application_proof']})
        blob=self.provider.put([encoded({'summary':'ok','evidence':['source']})])
        with self.assertRaisesRegex(RuntimeErrorCode,'EVIDENCE_INSUFFICIENT'):
            await self.store.complete(run['id'],'a','w','i',1,self.provider,blob.key,lambda chunks:True)
        self.assertEqual((await self.store.get(run['id']))['state'],'queued')
    async def test_old_worker_receipt_cannot_bypass_schema_on_recovery(self):
        run=await self.submit({'type':'object','required':['application_proof']});adapter=worker_fixtures.FakeAdapter()
        with patch('app.runtime.result_contract.validate_result'),patch.object(self.store,'complete',side_effect=ConnectionError('lost')):
            with self.assertRaises(ConnectionError):await self.worker(adapter).once()
        result=await reconcile_run(self.store,adapter,self.provider,run['id'])
        self.assertEqual(result['state'],'failed');self.assertEqual(adapter.started,1)
        self.assertEqual(len(result['attempts']),1)

class SdkGateTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=channel_fixtures.WorkerToolChannelTests.asyncSetUp
    asyncTearDown=channel_fixtures.WorkerToolChannelTests.asyncTearDown
    async def test_public_client_uses_same_bound_gate_and_cannot_approve(self):
        self.channel.session_id=None
        await self.channel.open();await self.channel.bind_session('session-one')
        client=ToolChannelClient(str(self.channel.directory/'channel.sock'))
        await client.ready('session-one')
        item=await client.request('request',{'tool':'search','arguments':{'query':'evidence'},'key':'sdk'})
        self.assertEqual(item['state'],'awaiting_approval');self.assertEqual(self.fixture.transport.calls,[])
        with self.assertRaisesRegex(WorkloadError,'TOOL_CHANNEL_METHOD_DENIED'):await client.request('approve',{})
        f=self.fixture;await f.gate.decide(f.run['id'],item['id'],item['version'],'approved','fixture-approval')
        result=await client.call('search',{'query':'evidence'},'sdk',timeout=5)
        self.assertEqual(result['content'][0]['text'],'中文结果🙂')
        await client.call('search',{'query':'evidence'},'sdk',timeout=5)
        self.assertEqual(len(f.transport.calls),1)
