"""G05: late remote result stays isolated while a human records the disposition."""
import json
import os
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import test_interventions as fixtures
from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.database import RuntimeErrorCode
from app.runtime.remote_control import RemoteControl
from app.runtime.remote_runtime import RemoteRuntimeStore
from app.runtime.worker_identity import WorkerIdentities
from app.services.run_evidence import RunEvidence

OBSERVATIONS=[]


class TakeoverJointTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.InterventionIntegrationTests.asyncSetUp
    asyncTearDown=fixtures.InterventionIntegrationTests.asyncTearDown
    save=fixtures.InterventionIntegrationTests.save
    headers_for=fixtures.InterventionIntegrationTests.headers_for
    dispatch=fixtures.InterventionIntegrationTests.dispatch

    async def test_late_result_is_reviewed_without_accepting_cancelled_work(self):
        source=await self.dispatch();store=self.console.store
        raw=await store.get(source['run_id'])
        cert='b'*64;identity_file=self.root/'workers.json'
        identity_file.write_text(json.dumps({'version':1,'audience':'test','machines':[dict(
            enabled=True,issuance_enabled=True,revision=1,subject='worker:review',worker_id='remote',
            certificate_sha256=cert,not_before=time.time()-1,expires_at=time.time()+3600,
            bindings=[{'scope':'one','profile':'review'}])]}))
        profiles=self.root/'remote-profiles.json'
        profiles.write_text(json.dumps({'version':1,'profiles':{'review':{'enabled':True,'path':str(self.profiles/'review.json')}}}))
        server=RemoteControl(store,WorkerIdentities(identity_file,'test'),profiles,self.policy,self.console.provider)
        async def exchange(envelope):return await server.handle(cert,time.time()+3600,envelope)
        remote=RemoteRuntimeStore('one',SimpleNamespace(worker_id='remote:'+uuid4().hex,config={'audience':'test'},
            profile='review',spec=self.spec,exchange=exchange))
        await remote.register_worker(remote.worker_id,'instance','fixture',os.getpid())
        attempt=await remote.claim(remote.worker_id,'instance',profile_id='review')
        binding=remote.bindings[source['run_id']]
        context=WorkAuthorizationContext('one',*binding,attempt['authorization_epoch'],None,raw['input']['work_authorization_ref'])
        await remote.verifier(context,'execute')
        await remote.report(*binding,1,'preparing');await remote.report(*binding,2,'creating_session')
        await remote.verifier(replace(context,session_id='late-session'),'execute')
        await remote.report(*binding,3,'waiting_model',session_id='late-session')
        blob=self.console.provider.put([b'{"summary":"late synthetic result"}'])
        await remote.upload(binding,self.console.provider,blob.key)
        pending=await self.control.takeover('work-001',self.reviewer,fixtures.API(self,'a'),
            '人工接管并复核在途结果','take',expected_version=source['version'])
        self.assertEqual(pending['state'],'takeover_pending')
        await remote.confirm_cancel(*binding,'fixture-process-exit')
        owned=await self.control.reconcile('work-001',self.reviewer,fixtures.API(self,'a'),expected_version=pending['version'])
        before=await store.get(source['run_id']);self.assertEqual(owned['state'],'human_owned')
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):
                await remote.bound('complete',binding,key=blob.key)
        after=await store.get(source['run_id'])
        self.assertEqual((after['state'],after['version']),(before['state'],before['version']))
        page=await RunEvidence(self.console).view(source['run_id'],section='transfers')
        self.assertEqual(len(page['items']),1);item=page['items'][0]
        self.assertEqual(item['attempt_id'],attempt['id'])
        self.assertEqual(item['descriptor']['digest'],blob.key)
        self.assertEqual(item['state'],'verified')
        self.assertEqual(b''.join(self.console.provider.read(blob.key)),b'{"summary":"late synthetic result"}')
        self.assertEqual((await self.reviewer.get(source['run_id']))['artifacts'],[])
        # Rejected output transfer history is an operator view; ordinary run.read does not expose rejected payloads.
        visible=await self.reviewer.request('GET','runs/'+source['run_id']+'/evidence?section=transfers')
        self.assertEqual(visible['items'],[])
        note='复核迟到记录 '+item['id']+'：原执行已取消，拒绝采纳，按新轮目标重做'
        next_round=await self.control.handback('work-001',self.reviewer,fixtures.API(self,'a'),
            '人工复核后重新执行',note,'review-late',expected_version=owned['version'])
        history=self.work.history('work-001')
        self.assertEqual(next_round['round_number'],2);self.assertIsNone(next_round['decision'])
        self.assertEqual(history['rounds'][0]['run_id'],source['run_id'])
        last=json.loads(history['events'][-1]['details'])
        self.assertEqual(last['note'],note)
        self.assertEqual(last['operator'],owned['reviewer'])
        OBSERVATIONS.append(dict(case='G05',work_item='work-001',run_id=source['run_id'],attempt_id=attempt['id'],
            session_id='late-session',transfer_id=item['id'],retained_digest=blob.key,late_replays=2,
            retained_transfers=1,accepted_as_result=False,human_note=note,business_round=2,
            runtime_state=after['state'],business_state=next_round['state'],operator_view_only=True))
