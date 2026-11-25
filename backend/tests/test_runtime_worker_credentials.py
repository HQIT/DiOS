import asyncio
import json
import os
import secrets
import time
import unittest
from unittest.mock import patch
from sqlalchemy import select
import test_runtime_remote_tools as fixtures
from app.runtime.worker_credentials import WorkerCredentials,token_hash
from app.runtime.credential_schema import credentials as rows
from app.runtime.database import RuntimeErrorCode,Database
from app.runtime.store import RuntimeStore
from app.runtime.recovery import backup_sqlite,verify_backup


class WorkerCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.RemoteToolTests();await self.fixture.asyncSetUp()
        self.addCleanup(self.fixture.profile_patch.stop)
        self.addCleanup(self.fixture.fingerprint_patch.stop)
        f=self.fixture;self.f=f.f
        self.machine={k:v for k,v in f.row.items() if k!='token_sha256'}
        self.machine.update(issuance_enabled=True,revision=1,expires_at=time.time()+86400)
        self.write_machine()
        self.key=self.f.root/'issuer.key';self.key.write_bytes(secrets.token_bytes(32));self.key.chmod(0o600)
        self.service=WorkerCredentials(self.f.store,f.identities,self.f.policy,self.key,ttl=60)
        f.gateway.credentials=self.service

    def write_machine(self):
        f=self.fixture;f.identities_file.write_text(json.dumps({'version':1,'audience':'tools:test','machines':[self.machine],'workers':[]}))

    async def asyncTearDown(self):await self.fixture.asyncTearDown()

    def envelope(self,method,params,token=''):
        value=self.fixture.envelope(method,params);value.update(credential_mode='automatic',access_token=token);return value

    async def exchange(self,envelope):
        return await self.service.exchange(self.fixture.cert,time.time()+3600,envelope)

    async def issue(self):
        envelope=self.envelope('credentials.issue',{'nonce':secrets.token_urlsafe(32)})
        return envelope,await self.exchange(envelope)

    async def test_issue_retry_rotation_and_response_recovery(self):
        envelope,first=await self.issue()
        self.assertEqual((await self.exchange(envelope))['access_token'],first['access_token'])
        renewal=self.envelope('credentials.renew',{'nonce':secrets.token_urlsafe(32),'expected_version':1},first['access_token'])
        second=await self.exchange(renewal);retry=await self.exchange(renewal)
        self.assertEqual(second['version'],2);self.assertEqual(second['access_token'],retry['access_token'])
        self.assertNotEqual(first['access_token'],second['access_token'])
        await self.service.authorize(self.fixture.cert,time.time()+3600,self.envelope('describe',{},second['access_token']))
        async with self.f.db.read(self.f.scope) as c:
            record=dict((await c.execute(select(rows).where(rows.c.id==second['credential_id']))).mappings().one())
        self.assertNotIn(second['access_token'],json.dumps(record));self.assertEqual(record['token_hash'],token_hash(second['access_token']))

    async def test_concurrent_same_renewal_is_idempotent(self):
        _,first=await self.issue();renewal=self.envelope('credentials.renew',{'nonce':secrets.token_urlsafe(32),'expected_version':1},first['access_token'])
        a,b=await asyncio.gather(self.exchange(renewal),self.exchange(renewal))
        self.assertEqual(a['access_token'],b['access_token']);self.assertEqual(a['version'],2)

    async def test_revoked_credential_cannot_be_issued_again(self):
        _,first=await self.issue();await self.service.revoke(first['credential_id'],1)
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_CREDENTIAL_REVOKED'):await self.issue()
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_CREDENTIAL_REVOKED'):
            await self.service.authorize(self.fixture.cert,time.time()+3600,self.envelope('describe',{},first['access_token']))

    async def test_machine_disable_and_policy_change_block_renewal(self):
        _,first=await self.issue();renewal=self.envelope('credentials.renew',{'nonce':secrets.token_urlsafe(32),'expected_version':1},first['access_token'])
        self.machine['enabled']=False;self.write_machine()
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_IDENTITY_EXPIRED_OR_REVOKED'):await self.exchange(renewal)
        self.machine['enabled']=True;self.machine['revision']=2;self.write_machine()
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_CREDENTIAL_BINDING_CHANGED'):await self.exchange(renewal)

    async def test_cancelled_work_and_stale_owner_block_exchange(self):
        _,first=await self.issue();renewal=self.envelope('credentials.renew',{'nonce':secrets.token_urlsafe(32),'expected_version':1},first['access_token'])
        wrong={**renewal,'binding':[*renewal['binding'][:4],999]}
        with self.assertRaisesRegex(RuntimeErrorCode,'STALE_OWNER'):await self.exchange(wrong)
        run=await self.f.store.get(self.f.run['id']);await self.f.store.cancel(run['id'],run['version'])
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.exchange(renewal)

    async def test_audit_failure_rolls_back_issuance(self):
        envelope=self.envelope('credentials.issue',{'nonce':secrets.token_urlsafe(32)})
        with patch('app.runtime.worker_credentials.audit',side_effect=OSError('audit unavailable')):
            with self.assertRaises(OSError):await self.exchange(envelope)
        value=await self.exchange(envelope);self.assertEqual(value['version'],1)

    async def test_completed_work_cannot_renew_or_reissue_credential(self):
        from dataclasses import replace
        from app.runtime.work_authorizations import view
        _, first = await self.issue()
        await self.f.store.report(*self.f.binding, 4, 'validating')
        await self.f.verifier(replace(self.f.context, session_id='session-one'), 'accept_result')
        blob = self.f.provider.put([json.dumps({'summary': 'complete', 'evidence': ['fixture:source']}).encode()])
        def valid(chunks):
            return json.loads(b''.join(chunks))['evidence'] == ['fixture:source']
        await self.f.store.complete(*self.f.binding, self.f.provider, blob.key, valid)
        self.assertEqual((await self.f.store.get(self.f.run['id']))['state'], 'succeeded')
        authorization = await view(self.f.store, self.f.run['id'])
        self.assertEqual((authorization['state'], authorization['grant']['state']), ('closed', 'closed'))
        with self.assertRaisesRegex(RuntimeErrorCode, 'STALE_OWNER'):
            await self.exchange(self.envelope('credentials.renew', {
                'nonce': secrets.token_urlsafe(32), 'expected_version': first['version']}, first['access_token']))
        with self.assertRaisesRegex(RuntimeErrorCode, 'STALE_OWNER'):
            await self.issue()

    async def test_expiry_reacquires_only_while_original_work_is_valid(self):
        _,first=await self.issue()
        async with self.f.db.transaction(self.f.scope) as c:
            await c.execute(rows.update().where(rows.c.id==first['credential_id']).values(expires_at=0,previous_until=0))
        _,second=await self.issue();self.assertEqual(second['version'],2)
        run=await self.f.store.get(self.f.run['id']);await self.f.store.revoke(run['id'],run['version'])
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.issue()

    async def test_gateway_dispatch_checks_durable_credential(self):
        f=self.fixture
        envelope=self.envelope('credentials.issue',{'nonce':secrets.token_urlsafe(32)})
        token=await f.gateway.handle(f.cert,time.time()+3600,envelope)
        request=self.envelope('request',{'tool':'search','arguments':{'query':'evidence'},'key':'auto'},token['access_token'])
        item=await f.gateway.handle(f.cert,time.time()+3600,request);await self.f.approve(item)
        advance=self.envelope('advance',{'invocation_id':item['id']},token['access_token'])
        result=await asyncio.wait_for(f.gateway.handle(f.cert,time.time()+3600,advance),15)
        self.assertEqual(result['state'],'succeeded');self.assertEqual(len(self.f.transport.calls),1)

    async def test_restart_and_backup_preserve_hashes(self):
        _,first=await self.issue()
        again=WorkerCredentials(self.f.store,self.fixture.identities,self.f.policy,self.key)
        await again.authorize(self.fixture.cert,time.time()+3600,self.envelope('describe',{},first['access_token']))
        destination=self.f.root/'credential-backup'
        await asyncio.to_thread(backup_sqlite,self.f.url,self.f.provider.root,destination)
        manifest=await asyncio.to_thread(verify_backup,destination)
        self.assertIn('runtime_credential_schema',manifest['schemas'])

    async def test_client_recovers_lost_renewal_response_without_tool_replay(self):
        from app.runtime.remote_tool_channel import RemoteToolProvider
        client=object.__new__(RemoteToolProvider);client.credentials={};client.automatic=True
        count=0;lose=False
        async def wire(binding,message,token):
            nonlocal count,lose
            count+=1;value=await self.exchange(self.envelope(message['method'],message['params'],token))
            if lose:lose=False;raise RuntimeErrorCode('WORKER_GATEWAY_OBSERVATION_UNAVAILABLE')
            return value
        client._wire=wire
        first=await client.credential(self.f.binding)
        client.credentials[tuple(self.f.binding)]['refresh_at']=0;lose=True
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_GATEWAY_OBSERVATION_UNAVAILABLE'):await client.credential(self.f.binding)
        second=await client.credential(self.f.binding)
        self.assertEqual(second['version'],2);self.assertEqual(count,3)
        self.assertNotEqual(first['access_token'],second['access_token']);self.assertEqual(self.f.transport.calls,[])

    async def test_expired_grant_never_receives_new_transport_credential(self):
        from app.runtime.work_auth_schema import grants
        async with self.f.db.transaction(self.f.scope) as c:
            await c.execute(grants.update().where(grants.c.attempt_id==self.f.attempt['id']).values(expires_at=0))
        with self.assertRaisesRegex(RuntimeErrorCode,'EXECUTION_GRANT_EXPIRED'):await self.issue()

    async def test_old_token_grace_is_bounded_and_cannot_rotate_again(self):
        _,first=await self.issue()
        envelope=self.envelope('credentials.renew',{'nonce':secrets.token_urlsafe(32),'expected_version':1},first['access_token'])
        second=await self.exchange(envelope)
        with self.assertRaisesRegex(RuntimeErrorCode,'VERSION_CONFLICT'):
            await self.exchange(self.envelope('credentials.renew',{'nonce':secrets.token_urlsafe(32),'expected_version':1},first['access_token']))
        async with self.f.db.transaction(self.f.scope) as c:
            await c.execute(rows.update().where(rows.c.id==first['credential_id']).values(previous_until=0))
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_TOKEN_DENIED'):
            await self.service.authorize(self.fixture.cert,time.time()+3600,self.envelope('describe',{},first['access_token']))
        await self.service.authorize(self.fixture.cert,time.time()+3600,self.envelope('describe',{},second['access_token']))

    async def test_status_and_audit_never_contain_token(self):
        from app.runtime.work_auth_schema import decisions
        _,first=await self.issue();status=await self.service.status(self.f.run['id'])
        async with self.f.db.read(self.f.scope) as c:
            logs=(await c.execute(select(decisions.c.decision).where(decisions.c.run_id==self.f.run['id']))).scalars().all()
        self.assertNotIn(first['access_token'],json.dumps([status,logs]));self.assertNotIn('token_hash',json.dumps(status))

    async def test_restore_hold_blocks_issue_and_renewal(self):
        from app.runtime.lifecycle import Lifecycle
        _,first=await self.issue();await Lifecycle(self.f.db,self.f.scope).hold_restored()
        try:
            with self.assertRaisesRegex(RuntimeErrorCode,'RESTORE_RECONCILIATION_REQUIRED'):
                await self.exchange(self.envelope('credentials.renew',{'nonce':secrets.token_urlsafe(32),'expected_version':1},first['access_token']))
        finally:
            # PostgreSQL test instances share one disposable DB; remove this test's global hold.
            from app.runtime.lifecycle_schema import control
            async with self.f.db.transaction(self.f.scope) as c:
                await c.execute(control.delete().where(control.c.key=='global'))


@unittest.skipUnless(os.environ.get('DIOS_TEST_POSTGRES_URL'),'isolated PostgreSQL not enabled')
class PostgresWorkerCredentialTests(WorkerCredentialTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Use an independent PostgreSQL fixture rather than altering an existing SQLite run.
        from app.runtime.schema import metadata
        from app.runtime.work_auth_schema import metadata as wm
        from app.runtime.tool_schema import metadata as tm
        worker_id='cw-'+secrets.token_hex(12)
        self.f.binding=(*self.f.binding[:2],worker_id,*self.f.binding[3:])
        self.machine['worker_id']=worker_id;self.write_machine()
        def copied(records,table):
            values=[dict(row) for row in records]
            for row in values:
                if row.get('worker_id')=='worker':row['worker_id']=worker_id
                if table.name=='runtime_workers' and row.get('id')=='worker':row['id']=worker_id
            return values
        old=self.f.db;db=Database(os.environ['DIOS_TEST_POSTGRES_URL']);await db.migrate()
        async with old.read(self.f.scope) as source:
            async with db.transaction(self.f.scope) as target:
                for meta in (metadata,wm,tm):
                    for table in meta.sorted_tables:
                        if 'scope' not in table.c:continue
                        records=(await source.execute(select(table).where(table.c.scope==self.f.scope))).mappings().all()
                        if records:await target.execute(table.insert(),copied(records,table))
                # Attempts/grants and events are scoped through their Run/authorization.
                from app.runtime.schema import attempts
                from app.runtime.work_auth_schema import grants
                for table in (attempts,grants):
                    records=(await source.execute(select(table).where(table.c.run_id==self.f.run['id']))).mappings().all()
                    if records:await target.execute(table.insert(),copied(records,table))
        await old.close();self.f.db=db;self.f.url=os.environ['DIOS_TEST_POSTGRES_URL']
        self.f.store=RuntimeStore(db,self.f.scope);self.f.gate.store=self.f.store;self.service.store=self.f.store
    async def test_restart_and_backup_preserve_hashes(self):
        self.skipTest('SQLite backup provider')
