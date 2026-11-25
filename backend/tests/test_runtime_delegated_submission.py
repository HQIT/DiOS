"""Authenticated issuance -> task admission -> execution using native OPA."""
import asyncio
import json
import unittest

from sqlalchemy import func, select
from app.runtime.schema import runs
from app.runtime.work_authorizations import WorkAuthorizationVerifier, view
from app.runtime.worker import Worker
from app.runtime.store import digest
from test_runtime_delegation_identity import DelegationIdentityTests
from test_runtime_worker import FakeAdapter


class DelegatedSubmissionTests(DelegationIdentityTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        profile = dict(id='test-profile', bridge='jsonl-v1', governance={'mode':'e2ag'},
            launch={'image':'test-image','command':['test-command']})
        catalog = self.root/'inputs.json'
        sample = {'id':'one','name':'合成输入'}
        sample['sha256'] = digest(sample)
        catalog.write_text(json.dumps({'version':1,'inputs':[sample],
            'dynamic_inputs':True,'schema':{'type':'object'}}))
        profile['console'] = dict(enabled=True, input_kind='delegation-fixture',
            input_catalog=str(catalog), synthetic=True)
        self.profiles = self.root/'profiles'
        self.profiles.mkdir()
        (self.profiles/'one.json').write_text(json.dumps(profile))
        self.console.profiles_dir = self.profiles
        self.directory['principals']['peer']['subject'] = self.chain[0]['subject_ref']
        self.directory['principals']['peer']['profiles']['test-profile']['actions'] += [
            'run.submit','run.read','artifact.read']
        self.save_identity()
        self.configure_anchor(self.chain[0]['id'])
        self.body['actor'] = 'actor:peer'
        self.child = await self.issue_api()
        self.request_body = dict(profile_id='test-profile',sample_id='one',
            goal='验证受控执行',idempotency_key='submitted',delegation_ref=self.child['id'])

    def configure_anchor(self, reference):
        config = json.loads(self.policy.path.read_text())
        row = config['profiles']['test-profile']
        if reference is None: row.pop('delegation_ref',None)
        else: row['delegation_ref'] = reference
        self.policy.path.write_text(json.dumps(config))

    async def submit_http(self, body=None, headers=None):
        return await self.http.post('/api/os/runtime/runs',
            headers=self.headers('peer') if headers is None else headers,
            json=self.request_body if body is None else body)

    async def count_runs(self):
        async with self.db.read(self.scope) as c:
            return await c.scalar(select(func.count()).select_from(runs))

    async def test_authenticated_reference_executes_without_policy_rewrite(self):
        before = self.policy.path.read_bytes()
        response = await self.submit_http()
        self.assertEqual(response.status_code,202,response.text)
        run_id = response.json()['id']
        run = await self.store.get(run_id)
        self.assertEqual(run['input']['delegation_ref'],self.child['id'])
        authorization = await view(self.store,run_id)
        self.assertLessEqual(authorization['expires_at'],self.child['expires_at'])
        adapter = FakeAdapter()
        worker = Worker(self.store,adapter,'test-profile',self.spec,self.provider,
            authorization_verifier=WorkAuthorizationVerifier(self.store,self.policy),poll_interval=.05)
        final = await worker.once()
        self.assertEqual(final['id'],run_id)
        self.assertEqual(final['state'],'succeeded')
        self.assertEqual(adapter.started,1)
        self.assertEqual(self.policy.path.read_bytes(),before)
        # Historical replay returns the same receipt; it never reopens Work.
        replay = await self.submit_http()
        self.assertEqual(replay.status_code,202,replay.text)
        self.assertEqual(replay.json()['id'],run_id)
        self.assertEqual(replay.json()['authorization']['state'],'closed')

    async def test_concurrent_duplicate_admission_has_one_work_and_changed_ref_conflicts(self):
        before = await self.count_runs()
        results = await asyncio.gather(*(self.submit_http() for _ in range(3)))
        self.assertTrue(all(r.status_code==202 for r in results),[r.text for r in results])
        self.assertEqual(len({r.json()['id'] for r in results}),1)
        self.assertEqual(await self.count_runs(),before+1)
        changed = await self.submit_http(dict(self.request_body,delegation_ref=self.chain[-1]['id']))
        self.assertEqual(changed.json()['detail'],'IDEMPOTENCY_CONFLICT')

    async def test_operator_and_other_actor_or_subject_cannot_select_delegation(self):
        before = await self.count_runs()
        response = await self.submit_http(headers={'X-DiOS-Access-Token':'test-operator'})
        self.assertEqual(response.json()['detail'],'AUTH_PRINCIPAL_REQUIRED')
        for field,value in [('actor','actor:other'),('subject','subject:other')]:
            old = self.directory['principals']['peer'][field]
            self.directory['principals']['peer'][field] = value
            self.save_identity()
            response = await self.submit_http()
            self.assertEqual(response.json()['detail'],'AUTH_DELEGATION_BINDING_MISMATCH')
            self.directory['principals']['peer'][field] = old
        self.assertEqual(await self.count_runs(),before)

    async def test_reference_cannot_replace_configured_ancestor(self):
        self.configure_anchor(self.chain[1]['id'])
        before = await self.count_runs()
        response = await self.submit_http()
        self.assertEqual(response.json()['detail'],'DELEGATION_ANCESTOR_REQUIRED')
        self.assertEqual(await self.count_runs(),before)

    async def test_optional_profile_anchor_and_dynamic_input(self):
        self.configure_anchor(None)
        body = {k:v for k,v in self.request_body.items() if k!='sample_id'}
        body['input_payload'] = {'id':'dynamic','name':'动态输入'}
        response = await self.submit_http(body)
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual((await self.store.get(response.json()['id']))['input']['delegation_ref'],self.child['id'])

    async def test_revoked_and_insufficient_delegations_roll_back_task_admission(self):
        before = await self.count_runs()
        limited = await self.issue_api(dict(self.body,idempotency_key='limited',permissions=[self.permissions[0]]))
        response = await self.submit_http(dict(self.request_body,delegation_ref=limited['id']))
        self.assertEqual(response.json()['detail'],'DELEGATION_DENIED')
        await self.manager.revoke(self.child['id'],self.child['version'])
        response = await self.submit_http()
        self.assertEqual(response.json()['detail'],'DELEGATION_REVOKED')
        self.assertEqual(await self.count_runs(),before)

    async def test_ancestor_revocation_dependency_fences_newly_submitted_work(self):
        response = await self.submit_http()
        self.assertEqual(response.status_code,202,response.text)
        run_id = response.json()['id']
        await self.manager.revoke(self.chain[0]['id'],self.chain[0]['version'])
        self.assertEqual((await view(self.store,run_id))['state'],'revoked')
        adapter = FakeAdapter()
        worker = Worker(self.store,adapter,'test-profile',self.spec,self.provider,
            authorization_verifier=WorkAuthorizationVerifier(self.store,self.policy),poll_interval=.05)
        final = await worker.once()
        self.assertEqual(final['id'],run_id)
        self.assertEqual(final['state'],'failed')
        self.assertEqual(adapter.started,0)

    async def test_invalid_ref_or_forged_identity_never_creates_task(self):
        before = await self.count_runs()
        for body in [dict(self.request_body,actor='actor:peer'),
                     dict(self.request_body,delegation_ref=''),dict(self.request_body,delegation_ref=42)]:
            self.assertEqual((await self.submit_http(body)).status_code,422)
        response = await self.submit_http(dict(self.request_body,delegation_ref='missing'))
        self.assertEqual(response.json()['detail'],'DELEGATION_NOT_FOUND')
        self.assertEqual(await self.count_runs(),before)


def load_tests(loader, tests, pattern):
    # Shared OPA fixture has its own suite; only this module's new contract cases.
    return unittest.TestSuite(DelegatedSubmissionTests(name) for name in
        DelegatedSubmissionTests.__dict__ if name.startswith('test_'))
