"""Independent app against real DiOS HTTP admission, Worker and E2AG storage.

The worker adapter is deterministic; no customer identity or model calls.
"""
import json
from pathlib import Path
import sys
import unittest

import test_runtime_principals as fixtures
from app.api.os import application_contracts
from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.database import RuntimeErrorCode
from dios_workforce.cli import Admin
from dios_workforce.registry import Registry, RegistryError, principal
from test_registry import sample


class API:
    def __init__(self, owner, key): self.owner, self.key = owner, key
    async def request(self, method, path, **kwargs):
        response = await self.owner.client.request(method, '/api/os/runtime/'+path,
            headers=self.owner.headers_for(self.key), **kwargs)
        if response.status_code >= 400: raise RegistryError(str(response.json()))
        return response.json()
    async def identity(self): return await self.request('GET', 'identity')
    async def profiles(self): return (await self.request('GET', 'profiles'))['items']
    async def runtime_contract(self): return await self.request('GET', 'contracts')
    async def submit(self, **request): return await self.request('POST', 'runs', json=request)


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    save = fixtures.PrincipalTests.save
    headers_for = fixtures.PrincipalTests.headers_for
    finish = fixtures.PrincipalTests.finish

    async def asyncSetUp(self):
        await fixtures.PrincipalTests.asyncSetUp(self)
        self.app.include_router(application_contracts.router, prefix='/api/os')
        self.registry = Registry(self.root/'workforce')
        self.api = API(self, 'a')
        fingerprint = (await self.api.profiles())[0]['profile_digest']
        self.registry.template('assistant', 1, sample(fingerprint))
        for key in ('a','b'):
            api = API(self,key)
            self.registry.revise('employee-'+key, 'assistant', 1, {}, expected_version=0,
                identity=principal(await api.identity()))
            await self.registry.publish('employee-'+key, api, expected_version=1)
        self.admin = Admin({'command':[sys.executable,'-m','app.runtime.cli',
            '--database-url','sqlite+aiosqlite:///'+str(self.root/'runtime.sqlite')],
            'catalog':str(self.identities)}, 'one', self.registry)

    async def asyncTearDown(self):
        self.registry.close()
        await fixtures.PrincipalTests.asyncTearDown(self)

    async def test_run_result_isolation_and_history_survive_app_restart(self):
        first = await self.registry.submit('employee-a','review','first-item','检查中文边界','job',self.api)
        await self.finish()
        run = await self.api.request('GET', 'runs/'+first['run_id'])
        self.assertEqual(run['state'], 'succeeded')
        self.assertTrue(run['artifacts'])
        other = API(self,'b')
        with self.assertRaises(RegistryError): await other.request('GET','runs/'+first['run_id'])
        second = await self.registry.submit('employee-b','review','first-item','检查中文边界','job',other)
        self.assertNotEqual(first['run_id'],second['run_id'])
        self.registry.close(); self.registry=Registry(self.root/'workforce')
        repeated = await self.registry.submit('employee-a','review','first-item','检查中文边界','job',self.api)
        self.assertEqual(first['run_id'],repeated['run_id'])
        history=self.registry.history('employee-a')
        self.assertEqual(len(history['tasks']),1)
        self.assertEqual(history['tasks'][0]['revision'],1)

    async def test_disable_admin_cli_revokes_claimed_work_and_preserves_identity(self):
        first = await self.registry.submit('employee-a','review','first-item','review','job',self.api)
        store=self.console.store
        await store.register_worker('worker','instance','host',123)
        attempt=await store.claim('worker','instance');raw=await store.get(first['run_id'])
        context=WorkAuthorizationContext('one',first['run_id'],attempt['id'],'worker','instance',
            attempt['epoch'],attempt['authorization_epoch'],None,raw['input']['work_authorization_ref'])
        await self.verifier(context,'execute')
        result=await self.registry.disable('employee-a',self.admin,expected_version=2)
        self.assertEqual(result['state'],'disabled')
        for action in ('execute','accept_result'):
            with self.assertRaises(RuntimeErrorCode): await self.verifier(context,action)
        with self.assertRaises(RegistryError): await self.api.identity()
        with self.assertRaises(RegistryError):
            await self.registry.submit('employee-a','review','first-item','review','new',self.api)
        self.assertEqual(self.registry.history('employee-a')['tasks'][0]['run_id'],first['run_id'])
        self.assertEqual(principal(await API(self,'b').identity())['id'],'b')

    async def test_profile_precondition_rejects_drift_before_durable_admission(self):
        body={'profile_id':'review','sample_id':'first-item','goal':'review','idempotency_key':'conflict',
              'expected_profile_digest':'0'*64}
        response=await self.client.post('/api/os/runtime/runs',headers=self.headers_for('a'),json=body)
        self.assertEqual(response.status_code,409,response.text)
        self.assertIn('PROFILE_VERSION_CONFLICT',response.text)
        self.assertEqual((await self.api.request('GET','runs'))['items'],[])
        body['expected_profile_digest']='invalid'
        response=await self.client.post('/api/os/runtime/runs',headers=self.headers_for('a'),json=body)
        self.assertEqual(response.status_code,422,response.text)
