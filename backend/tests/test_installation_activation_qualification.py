"""Clean restore qualification: native workload, mTLS reauthentication and offline activation."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import ssl
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.db.database import Base
from app.models.tables import Agent, A2ATask
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.reconciliation import ExitReceipts
from app.runtime.recovery import _file_info
from app.runtime.remote_control import RemoteControl
from app.runtime.remote_tool_channel import RemoteToolProvider
from app.runtime.remote_tool_gateway import ToolGateway
from app.runtime.restore_client import RestoreRemoteClient
from app.runtime.store import RuntimeStore
from app.runtime.worker_identity import WorkerIdentities, tls_context
from app.services.installation_activation import SECTIONS
from app.services.installation_backup import backup_installation
from app.services.installation_readiness import inspect_restore
from app.services.installation_restore import restore_installation
import test_remote_recovery_qualification as native

OBSERVATIONS=[]


@unittest.skipUnless(os.environ.get('DIOS_TEST_DOCKER')=='1','isolated Docker required')
class InstallationActivationQualificationTests(unittest.IsolatedAsyncioTestCase):
    async def cli(self,*args,success=True):
        process=await asyncio.create_subprocess_exec(sys.executable,str(native.ROOT/'cli/dios'),
            'runtime',*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await asyncio.wait_for(process.communicate(),40)
        if success:self.assertEqual(process.returncode,0,err.decode())
        else:self.assertNotEqual(process.returncode,0)
        return json.loads(out) if success else (out+err).decode()

    async def test_checkpoint_result_recovery_then_guarded_activation(self):
        f=native.RemoteRecoveryQualificationTests()
        await f.asyncSetUp()
        self.addAsyncCleanup(f.asyncTearDown)
        # Fixtures register their native cleanup with unittest; use its async cleanup runner here.
        self.addAsyncCleanup(self.clean_fixture,f)
        root=f.root
        service=create_async_engine('sqlite+aiosqlite:///'+str(root/'service.sqlite'))
        self.addAsyncCleanup(service.dispose)
        async with service.begin() as c:await c.run_sync(Base.metadata.create_all)
        workspace=root/'workspace';workspace.mkdir()
        (workspace/'资料.txt').write_text('恢复后保留原资料与出处。')
        async with async_sessionmaker(service)() as session:
            session.add(Agent(id='historical-agent',name='Historical',workspace_path=str(workspace)))
            session.add(A2ATask(id='historical-task',agent_id='historical-agent',status='completed',message={'text':'retained history'}))
            await session.commit()
        run=await f.submit('before-checkpoint-active')
        original_handle=f.server.handle
        async def refuse_result(certificate,expiry,envelope):
            if envelope['message']['method'].startswith('runtime.upload.'):
                raise RuntimeErrorCode('QUALIFICATION_UPLOAD_INTERRUPTED')
            return await original_handle(certificate,expiry,envelope)
        f.server.handle=refuse_result
        process=await f.launch()
        live=await f.concurrency(1)
        queued=await f.submit('before-checkpoint-queued')
        plan={'format':1,'runtime_database_url':f.fixture.url,
            'service_database_url':'sqlite+aiosqlite:///'+str(root/'service.sqlite'),
            'artifacts':str(f.fixture.provider.root),
            'configuration':[{'id':'profile','path':str(f.profiles['account-a'][0])}],
            'workspaces':[{'id':'history','path':str(workspace)}],'spools':[],
            'external':[{'kind':'remote-worker-results','reference':'worker:qualified',
                'recovery':'Retain the original exit receipt and reauthenticate to reconcile it before activation.'}]}
        bundle=root/'backup';target=root/'restored'
        await asyncio.to_thread(backup_installation,plan,bundle)
        with sqlite3.connect(bundle/'runtime/state.sqlite') as db:
            snapshot_version=db.execute('SELECT version FROM runtime_runs WHERE id=?',(run['id'],)).fetchone()[0]
        (f.control/'release').touch()
        out,err=await asyncio.wait_for(process.communicate(),40)
        self.assertNotEqual(process.returncode,0)
        self.assertIn('QUALIFICATION_UPLOAD_INTERRUPTED',(out+err).decode())
        original=await f.store.get(run['id']);attempt=original['attempts'][0]
        retained=LocalArtifacts(root/'worker-0');self.addCleanup(retained.close)
        receipt=ExitReceipts(retained,f.store.scope).load(attempt)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt['observation']['exit_code'],0)
        self.assertFalse(receipt['observation']['alive'])
        # Stop the actual source listener and executor process before the target opens.
        source_address=f.gateway.server.sockets[0].getsockname()
        f.stop.set();await asyncio.wait_for(f.gateway_task,15)
        f.gateway_task=None
        with self.assertRaises(OSError):
            await asyncio.wait_for(asyncio.open_connection(*source_address[:2]),3)
        containers=await asyncio.to_thread(f.adapter.client.containers.list,all=True,
            filters={'label':'dios.runtime.scope='+f.store.scope})
        self.assertLessEqual(len(containers),1)
        for container in containers:
            await asyncio.to_thread(container.reload)
            self.assertFalse(container.attrs['State']['Running'])
        await f.fixture.db.close();await service.dispose()
        source_hash=_file_info(root/'runtime.sqlite')
        await restore_installation(bundle,target)
        self.assertFalse(inspect_restore(target)['local_execution_settled'])
        self.assertEqual((target/'workspaces/history/资料.txt').read_text(),(workspace/'资料.txt').read_text())
        evidence=root/'activation.json'
        evidence_value={'format':1,'restore_receipt_sha256':_file_info(target/'installation-restore.json')['sha256'],
            'operator':'qualification:administrator'}
        evidence_value.update({name:{'method':'administrator-confirmed','evidence_ref':'qualification:'+name} for name in SECTIONS})
        evidence.write_text(json.dumps(evidence_value))
        denied=await self.cli('installation-activate',str(target),'--evidence',str(evidence),'--confirm-source-isolated',success=False)
        self.assertIn('RESTORE_RECONCILIATION_REQUIRED',denied)
        db=Database('sqlite+aiosqlite:///'+str(target/'runtime/state.sqlite'))
        self.addAsyncCleanup(db.close)
        restored=RuntimeStore(db,f.store.scope)
        provider=LocalArtifacts(target/'runtime/objects');self.addCleanup(provider.close)
        # Issue a new certificate from the fixture CA; the original cert is removed from the allowlist.
        def rotate():
            def openssl(*args):
                subprocess.run(['openssl',*args],cwd=root,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            openssl('req','-newkey','rsa:2048','-nodes','-keyout','rotated.key','-out','rotated.csr','-subj','/CN=rotated')
            openssl('x509','-req','-in','rotated.csr','-CA','ca.pem','-CAkey','ca.key','-CAcreateserial',
                '-out','rotated.pem','-days','1','-extfile','client.ext')
        await asyncio.to_thread(rotate)
        identity=json.loads((root/'machines.json').read_text())
        identity['machines'][0].update(certificate_sha256=hashlib.sha256(ssl.PEM_cert_to_DER_cert((root/'rotated.pem').read_text())).hexdigest(),revision=2)
        identities_path=root/'restored-machines.json';identities_path.write_text(json.dumps(identity))
        identities=WorkerIdentities(identities_path,'qualification')
        server=RemoteControl(restored,identities,root/'profiles.json',f.policy,provider)
        gate=ToolGateway(SimpleNamespace(store=restored),identities,root/'profiles.json');gate.runtime_control=server
        stop=asyncio.Event()
        gateway_task=asyncio.create_task(gate.serve('127.0.0.1',0,
            tls_context(root/'ca.pem',root/'server.pem',root/'server.key',server=True),stop))
        async def close_gateway():
            stop.set();await asyncio.wait_for(gateway_task,15)
        self.addAsyncCleanup(close_gateway)
        async with asyncio.timeout(5):
            while gate.server is None:await asyncio.sleep(.01)
        config=json.loads(f.client_file.read_text())
        config['port']=gate.server.sockets[0].getsockname()[1]
        old_config=root/'old-client.json';old_config.write_text(json.dumps(config))
        binding=[receipt[k] for k in ('run_id','attempt_id','worker_id','worker_instance','epoch')]
        transport=RemoteToolProvider(old_config,SimpleNamespace(scope=f.store.scope),'account-a',f.profiles['account-a'][1])
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_IDENTITY_DENIED'):
            await RestoreRemoteClient(f.store.scope,transport).prepare(binding,snapshot_version)
        config.update(certificate_file=str(root/'rotated.pem'),key_file=str(root/'rotated.key'))
        new_config=root/'new-client.json';new_config.write_text(json.dumps(config))
        receipt_file=root/'original-exit.json';receipt_file.write_text(json.dumps(receipt))
        recover_args=['--scope',f.store.scope,'restore-reconcile','--control-file',str(new_config),
            '--profile-file',str(f.profiles['account-a'][0]),'--receipt-file',str(receipt_file),
            '--artifacts',str(retained.root),'--expected-version',str(snapshot_version)]
        # Current policy revocation must win even with a freshly authenticated machine.
        policy_value=json.loads(f.policy.path.read_text())
        denied_policy=json.loads(json.dumps(policy_value));denied_policy['profiles']['account-a']['permissions']['caller']=[]
        f.policy.path.write_text(json.dumps(denied_policy))
        await self.cli(*recover_args,success=False)
        self.assertEqual((await restored.get(run['id']))['version'],snapshot_version)
        f.policy.path.write_text(json.dumps(policy_value))
        recovered=await self.cli(*recover_args)
        self.assertEqual(recovered,await self.cli(*recover_args))
        self.assertEqual(recovered['state'],'succeeded')
        self.assertEqual(recovered['attempt_id'],attempt['id'])
        self.assertEqual(recovered['attempts'][0]['session_id'],attempt['session_id'])
        self.assertEqual(len(recovered['attempts']),1)
        self.assertTrue(inspect_restore(target)['local_execution_settled'])
        with sqlite3.connect(target/'runtime/state.sqlite') as c:
            self.assertEqual(c.execute("SELECT count(*) FROM runtime_worker_credentials WHERE state='active'").fetchone()[0],0)
            audit=c.execute("SELECT action,decision FROM runtime_work_decisions WHERE run_id=? ORDER BY rowid",(run['id'],)).fetchall()
        audit=[(action,json.loads(decision)['decision']) for action,decision in audit]
        self.assertIn(('restore-result','deny'),audit)
        self.assertIn(('restore-result','allow'),audit)
        await close_gateway();await db.close();provider.close()
        activated=await self.cli('installation-activate',str(target),'--evidence',str(evidence),'--confirm-source-isolated')
        self.assertEqual(activated,await self.cli('installation-activate',str(target),'--evidence',str(evidence),'--confirm-source-isolated'))
        self.assertEqual(activated['status'],'activated')
        self.assertGreaterEqual(activated['verified_objects'],3)
        # Reopen through the normal runtime and run the already-queued task using fresh credentials.
        resumed_db=Database('sqlite+aiosqlite:///'+str(target/'runtime/state.sqlite'))
        self.addAsyncCleanup(resumed_db.close)
        f.store=RuntimeStore(resumed_db,f.store.scope)
        resumed_provider=LocalArtifacts(target/'runtime/objects');self.addCleanup(resumed_provider.close)
        f.server=RemoteControl(f.store,identities,root/'profiles.json',f.policy,resumed_provider)
        f.gateway=ToolGateway(SimpleNamespace(store=f.store),identities,root/'profiles.json');f.gateway.runtime_control=f.server
        f.stop=asyncio.Event()
        f.gateway_task=asyncio.create_task(f.gateway.serve('127.0.0.1',0,
            tls_context(root/'ca.pem',root/'server.pem',root/'server.key',server=True),f.stop))
        async with asyncio.timeout(5):
            while f.gateway.server is None:await asyncio.sleep(.01)
        config['port']=f.gateway.server.sockets[0].getsockname()[1]
        new_config.write_text(json.dumps(config));f.client_file=new_config
        resumed=await f.collect(await f.launch())
        self.assertEqual(resumed['state'],'succeeded')
        self.assertEqual(resumed['id'],queued['id'])
        self.assertEqual((await f.collect(await f.launch()))['state'],'idle')
        self.assertEqual(len((await f.store.get(run['id']))['attempts']),1)
        self.assertEqual(source_hash,_file_info(root/'runtime.sqlite'))
        with sqlite3.connect(target/'service.sqlite') as c:
            self.assertEqual(c.execute("SELECT status FROM a2a_tasks WHERE id='historical-task'").fetchone()[0],'completed')
        OBSERVATIONS.append({'source_listener_stopped':True,'source_worker_pid':process.pid,
            'source_worker_returncode':process.returncode,'source_container_count':len(containers),
            'source_containers_stopped':True,'source_database_unchanged':True,
            'checkpoint_version':snapshot_version,'run_id':run['id'],'original_attempt_id':attempt['id'],
            'session_id':attempt['session_id'],'restored_attempt_count':len(recovered['attempts']),
            'old_certificate_rejected':True,'current_e2ag_revocation_rejected':True,
            'result_after_checkpoint_reconciled':True,'repeated_result_receipt_equal':True,
            'verified_objects':activated['verified_objects'],'activation':activated['after'],
            'queued_run_id':queued['id'],'queued_run_after_activation':resumed['state'],
            'duplicate_dispatch_after_completion':False,'image_id':f.image_id,
            'native_concurrency':live})

    @staticmethod
    async def clean_fixture(f):
        # Explicitly mirror fixture-registered cleanup in reverse order.
        await f.cleanup_native()
        await f.fixture.asyncTearDown()