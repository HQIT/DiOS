"""Operator API and registered Console submissions use the same active revision."""

import json
import unittest

import test_console_revisions as fixtures
from app.runtime.execution_revisions import load_profile
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.worker import Worker
from app.services.deployment_worker import DeploymentLoop
from test_runtime_worker import FakeAdapter
from work_auth_fixtures import policy_fixture


class DeploymentConsoleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ConsoleRevisionTests.asyncSetUp
    asyncTearDown = fixtures.ConsoleRevisionTests.asyncTearDown
    publish = fixtures.ConsoleRevisionTests.publish

    async def test_operator_deployment_and_console_binding_reconcile_after_restart(self):
        ref = await self.publish(governed=True)
        _, profile, spec, _, _ = await load_profile(self.db, 'one', ref)
        self.console.policy, _ = policy_fixture(self.root, profile, spec)
        pointer = {'id': profile, 'deployment_id': 'primary', 'enabled': True}
        (self.profiles/'review.json').write_text(json.dumps(pointer))
        body = {'id':'primary', 'revision_ref':ref, 'expected_version':0, 'idempotency_key':'create', 'probe_input':{'payload':{}}}
        denied = await self.client.post('/api/os/runtime/deployments',json=body)
        self.assertEqual(denied.status_code, 401)
        accepted = await self.client.post('/api/os/runtime/deployments',json=body,headers=self.headers)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        repeat = await self.client.post('/api/os/runtime/deployments',json=body,headers=self.headers)
        self.assertEqual(repeat.json()['receipt'], accepted.json()['receipt'])
        self.assertEqual((await self.console.catalog()), [])
        worker = Worker(self.console.store, FakeAdapter(), profile, spec, self.console.provider,
                        revision_ref=ref, authorization_verifier=WorkAuthorizationVerifier(self.console.store,self.console.policy),poll_interval=.01)
        self.assertEqual((await worker.once())['state'], 'succeeded')
        # Recreate the controller to prove readiness comes from persistent evidence.
        await DeploymentLoop(self.console).tick()
        detail = await self.client.get('/api/os/runtime/deployments/primary',headers=self.headers)
        self.assertEqual(detail.json()['status']['active_revision_id'], ref)
        self.assertEqual(detail.json()['deployment']['state'], 'ready')
        catalogue = await self.client.get('/api/os/runtime/profiles',headers=self.headers)
        self.assertEqual(catalogue.json()['items'][0]['deployment_id'], 'primary')
        self.assertEqual(catalogue.json()['items'][0]['revision_ref'], ref)
        run = await self.console.submit(profile,'first-item','核验边界','task')
        self.assertEqual(run['deployment_id'], 'primary')
        self.assertEqual(run['revision_ref'], ref)
        listing = await self.client.get('/api/os/runtime/deployments',headers=self.headers)
        self.assertEqual(listing.json()['items'][0]['deployment'],detail.json()['deployment'])


if __name__ == '__main__':
    unittest.main()
