"""Offline activation gates, dual-database rollback and crash receipt recovery."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
import test_installation_readiness as fixtures
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.recovery import _file_info
from app.services.installation_activation import activate_installation, SECTIONS
from app.services.installation_readiness import inspect_restore


class ActivationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.ReadinessTests.asyncSetUp
    asyncTearDown=fixtures.ReadinessTests.asyncTearDown
    backup=fixtures.ReadinessTests.backup
    restored=fixtures.ReadinessTests.restored

    async def prepared(self,settled=True):
        await self.restored()
        if settled:
            # Unit fixture only. Real recovery settles original tasks via reconcile APIs.
            with sqlite3.connect(self.target/'runtime/state.sqlite') as db:
                db.execute("UPDATE runtime_attempts SET state='abandoned'")
                db.execute("UPDATE runtime_runs SET state='queued'")
            with sqlite3.connect(self.target/'service.sqlite') as db:
                db.execute("UPDATE a2a_tasks SET status='completed'")
                db.execute("UPDATE managed_task_executions SET finished=1")
            with sqlite3.connect(self.target/'spools/worker.sqlite') as db:db.execute('DELETE FROM reports')
        self.evidence=self.root/'activation-evidence.json'
        value={'format':1,'restore_receipt_sha256':_file_info(self.target/'installation-restore.json')['sha256'],'operator':'operator:qualification'}
        value.update({name:{'method':'administrator-confirmed','evidence_ref':'fixture:'+name} for name in SECTIONS})
        self.evidence.write_text(json.dumps(value))

    def activate(self):
        return activate_installation(self.target,self.evidence,confirm_source_isolated=True)

    def holds(self):return inspect_restore(self.target)['holds']

    async def test_unsettled_work_rejected_without_releasing_either_gate(self):
        await self.prepared(settled=False)
        with self.assertRaisesRegex(RuntimeErrorCode,'RESTORE_RECONCILIATION_REQUIRED'):self.activate()
        self.assertEqual(self.holds(),{'runtime':True,'service':True})
        self.assertFalse((self.target/'installation-activation-intent.json').exists())

    async def test_explicit_confirmation_and_matching_restore_required(self):
        await self.prepared()
        with self.assertRaisesRegex(RuntimeErrorCode,'SOURCE_ISOLATION_CONFIRMATION_REQUIRED'):
            activate_installation(self.target,self.evidence)
        value=json.loads(self.evidence.read_text());value['restore_receipt_sha256']='0'*64
        self.evidence.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeErrorCode,'ACTIVATION_RESTORE_MISMATCH'):self.activate()
        self.assertEqual(self.holds(),{'runtime':True,'service':True})

    async def test_active_runtime_prevents_offline_activation(self):
        await self.prepared()
        live=Database('sqlite+aiosqlite:///'+str(self.target/'runtime/state.sqlite'))
        try:
            with self.assertRaisesRegex(RuntimeErrorCode,'STORAGE_MAINTENANCE_BUSY'):self.activate()
        finally:await live.close()
        self.assertEqual(self.holds(),{'runtime':True,'service':True})

    async def test_both_gates_release_once_and_old_credentials_stay_revoked(self):
        await self.prepared()
        before=_file_info(self.root/'runtime.sqlite')
        first=self.activate();second=self.activate()
        self.assertEqual(first,second);self.assertEqual(first['status'],'activated')
        self.assertEqual(self.holds(),{'runtime':False,'service':False})
        self.assertEqual(before,_file_info(self.root/'runtime.sqlite'))
        with sqlite3.connect(self.target/'runtime/state.sqlite') as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runtime_worker_credentials WHERE state='active'").fetchone()[0],0)
        value=json.loads(self.evidence.read_text());value['operator']='another'
        self.evidence.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeErrorCode,'ACTIVATION_EVIDENCE_CONFLICT'):self.activate()

    async def test_second_database_failure_rolls_back_first_gate(self):
        await self.prepared()
        with sqlite3.connect(self.target/'service.sqlite') as db:
            db.execute("CREATE TRIGGER deny_activation BEFORE UPDATE ON managed_runtime_control BEGIN SELECT RAISE(ABORT,'fixture commit failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'fixture commit failure'):self.activate()
        self.assertEqual(self.holds(),{'runtime':True,'service':True})
        with sqlite3.connect(self.target/'service.sqlite') as db:db.execute('DROP TRIGGER deny_activation')
        self.assertEqual(self.activate()['status'],'activated')

    async def test_receipt_failure_after_commit_recovers_without_second_activation(self):
        await self.prepared()
        from app.services import installation_activation as module
        original=module.publish_document
        def interrupted(path,chunks):
            if path.name=='installation-activation.json':raise OSError('fixture receipt interrupted')
            return original(path,chunks)
        with patch.object(module,'publish_document',side_effect=interrupted):
            with self.assertRaisesRegex(OSError,'receipt interrupted'):self.activate()
        self.assertEqual(self.holds(),{'runtime':False,'service':False})
        intent=json.loads((self.target/'installation-activation-intent.json').read_text())
        result=self.activate();self.assertEqual(result['after'],intent['after'])
        self.assertEqual(result,self.activate())

    async def test_cli_requires_no_runtime_database_configuration(self):
        await self.prepared()
        cli=Path(__file__).resolve().parents[2]/'cli/dios'
        process=await asyncio.create_subprocess_exec(sys.executable,str(cli),'runtime',
            'installation-activate',str(self.target),'--evidence',str(self.evidence),'--confirm-source-isolated',
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await asyncio.wait_for(process.communicate(),30)
        self.assertEqual(process.returncode,0,err.decode())
        self.assertEqual(json.loads(out)['status'],'activated')

    async def test_additional_hold_is_not_silently_ignored(self):
        await self.prepared()
        with sqlite3.connect(self.target/'service.sqlite') as db:
            db.execute("INSERT INTO managed_runtime_control(scope,mode,revision,updated_at) VALUES('other-hold','restore_hold',1,datetime('now'))")
        with self.assertRaisesRegex(RuntimeErrorCode,'RESTORE_HOLD_REQUIRED'):self.activate()
        self.assertEqual(self.holds(),{'runtime':True,'service':True})

    async def test_interrupted_record_publication_leaves_no_partial_final_file(self):
        await self.prepared()
        from app.services import installation_activation as module
        with patch.object(module.os, 'link', side_effect=OSError('publication interrupted')):
            with self.assertRaisesRegex(OSError, 'publication interrupted'):
                self.activate()
        self.assertFalse((self.target/'installation-activation-intent.json').exists())
        self.assertEqual(self.holds(), {'runtime':True,'service':True})
        self.assertEqual(self.activate()['status'], 'activated')

    async def test_completed_attempt_is_settled_using_native_terminal_state(self):
        await self.prepared()
        with sqlite3.connect(self.target/'runtime/state.sqlite') as db:
            db.execute("UPDATE runtime_attempts SET state='completed'")
            db.execute("UPDATE runtime_runs SET state='succeeded'")
        self.assertTrue(inspect_restore(self.target)['local_execution_settled'])
        self.assertEqual(self.activate()['status'],'activated')