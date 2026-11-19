"""Deferred receipt recovery regressions. No model or external tool invocation."""
import asyncio
import unittest
from unittest.mock import patch, AsyncMock
from sqlalchemy import select
import test_runtime_tool_invocations as fixtures
from app.runtime.database import RuntimeErrorCode, Database
from app.runtime.artifacts import LocalArtifacts
from app.runtime.store import RuntimeStore
from app.runtime.tool_invocations import ToolInvocations
from app.runtime.tool_schema import invocations
from app.runtime.work_auth_schema import grants
from app.runtime.recovery import backup_sqlite, verify_backup, restore_sqlite
from app.runtime.lifecycle import Lifecycle


class ToolReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=fixtures.ToolInvocationTests();await self.f.asyncSetUp()
    async def asyncTearDown(self):await self.f.asyncTearDown()

    async def stranded(self):
        f=self.f;item=await f.approve(await f.request())
        with patch.object(f.gate,'recover_receipt',side_effect=ConnectionError('control unavailable')):
            with self.assertRaises(ConnectionError):await f.gate.dispatch(f.run['id'],item['id'],item['version'])
        saved=await f.gate.get(f.run['id'],item['id'])
        self.assertEqual(saved['state'],'dispatched');self.assertTrue(saved['receipt_available'])
        return saved

    async def test_complete_receipt_recovers_after_control_write_failure_without_reexecution(self):
        f=self.f;item=await self.stranded()
        result=await f.gate.recover_receipt(f.run['id'],item['id'],item['version'])
        self.assertEqual(result['state'],'succeeded')
        repeated=await f.gate.recover_receipt(f.run['id'],item['id'],item['version'])
        self.assertEqual(repeated['version'],result['version'])
        await f.gate.dispatch(f.run['id'],item['id'],1)
        self.assertEqual(len(f.transport.calls),1)

    async def test_expiry_and_revocation_preserve_tool_fact_without_renewal_or_run_success(self):
        f=self.f;item=await self.stranded();run=await f.store.get(f.run['id'])
        await f.store.revoke(run['id'],run['version'])
        async with f.db.transaction(f.scope) as c:
            await c.execute(grants.update().where(grants.c.attempt_id==f.attempt['id']).values(expires_at=0))
            before=(await c.execute(select(grants).where(grants.c.attempt_id==f.attempt['id']))).mappings().one()
        result=await f.gate.recover_receipt(run['id'],item['id'])
        self.assertEqual(result['state'],'succeeded')
        after_run=await f.store.get(run['id']);self.assertTrue(after_run['revoked']);self.assertNotEqual(after_run['state'],'succeeded')
        async with f.db.read(f.scope) as c:
            after=(await c.execute(select(grants).where(grants.c.attempt_id==f.attempt['id']))).mappings().one()
        self.assertEqual(dict(before),dict(after))

    async def test_original_contract_used_when_current_catalog_changes(self):
        f=self.f;item=await self.stranded()
        f.catalog_file.write_text('{"version":1,"tools":{}}')
        result=await f.gate.recover_receipt(f.run['id'],item['id'])
        self.assertEqual(result['state'],'succeeded');self.assertEqual(len(f.transport.calls),1)

    async def test_partial_receipt_stays_unknown_and_replay_does_not_bump_version(self):
        f=self.f;item=await f.approve(await f.request());f.transport.failure=True
        result=await f.gate.dispatch(f.run['id'],item['id'],item['version'])
        self.assertEqual(result['state'],'result_unknown')
        again=await f.gate.recover_receipt(f.run['id'],item['id'])
        self.assertEqual(again['version'],result['version']);self.assertEqual(len(f.transport.calls),1)

    async def test_binding_change_never_recovers_success(self):
        f=self.f;item=await self.stranded()
        async with f.db.transaction(f.scope) as c:
            await c.execute(invocations.update().where(invocations.c.id==item['id']).values(arguments_digest='0'*64))
        with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_RECEIPT_BINDING_MISMATCH'):
            await f.gate.recover_receipt(f.run['id'],item['id'])
        self.assertEqual((await f.gate.get(f.run['id'],item['id']))['state'],'dispatched')

    async def test_recovery_audit_failure_rolls_back_and_keeps_receipt(self):
        f=self.f;item=await self.stranded()
        with patch('app.runtime.tool_invocations.audit',side_effect=OSError('audit unavailable')):
            with self.assertRaises(OSError):await f.gate.recover_receipt(f.run['id'],item['id'])
        self.assertTrue(f.gate.receipts.available(item['id']))
        result=await f.gate.recover_receipt(f.run['id'],item['id'])
        self.assertEqual(result['state'],'succeeded')

    async def test_periodic_recovery_uses_receipt_and_does_not_call_transport(self):
        f=self.f;item=await self.stranded()
        results=await f.gate.recover_pending('test-profile')
        self.assertEqual(results,[{'id':item['id'],'state':'succeeded'}]);self.assertEqual(len(f.transport.calls),1)

    async def test_backup_preserves_unlinked_receipt_and_restored_hold_blocks_recovery_write(self):
        f=self.f;item=await self.stranded();bundle=f.root/'receipt-backup'
        await asyncio.to_thread(backup_sqlite,f.url,f.provider.root,bundle)
        manifest=await asyncio.to_thread(verify_backup,bundle)
        self.assertEqual(manifest['format'],2);self.assertEqual(len(manifest['tool_receipts']['files']),1)
        destination=f.root/'restored';await restore_sqlite(bundle,destination)
        db=Database('sqlite+aiosqlite:///'+str(destination/'state.sqlite'))
        try:
            store=RuntimeStore(db,f.scope);provider=LocalArtifacts(destination/'objects')
            gate=ToolInvocations(store,f.policy,f.gate.catalog,provider,f.transport,f.gate.approvers)
            self.assertIsNotNone(gate.receipts.load(item['id']))
            with self.assertRaisesRegex(RuntimeErrorCode,'RESTORE_RECONCILIATION_REQUIRED'):
                await gate.recover_receipt(f.run['id'],item['id'])
        finally:await db.close()

    async def test_pending_tool_result_blocks_cutover_even_when_worker_is_terminal(self):
        f=self.f;item=await self.stranded()
        from app.runtime.schema import attempts
        async with f.db.transaction(f.scope) as c:
            await c.execute(attempts.update().where(attempts.c.id==f.attempt['id']).values(state='failed'))
        status=await Lifecycle(f.db,f.scope).begin_drain()
        self.assertEqual(status['active_attempts'],0);self.assertEqual(status['tool_calls_inflight'],1)
        self.assertFalse(status['ready_for_cutover'])

    async def test_corrupt_output_keeps_original_call_pending(self):
        f=self.f;item=await self.stranded();value=f.gate.receipts.load(item['id'])
        f.provider.path(value['output']['key']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeErrorCode,'ARTIFACT_DIGEST_MISMATCH'):
            await f.gate.recover_receipt(f.run['id'],item['id'])
        self.assertEqual((await f.gate.get(f.run['id'],item['id']))['state'],'dispatched')
        self.assertEqual(len(f.transport.calls),1)

    async def test_missing_receipt_does_not_guess_a_result(self):
        f=self.f;item=await self.stranded();f.gate.receipts.path(item['id']).unlink()
        with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_RECEIPT_NOT_FOUND'):
            await f.gate.recover_receipt(f.run['id'],item['id'])
        self.assertEqual((await f.gate.get(f.run['id'],item['id']))['state'],'dispatched')
        self.assertEqual(len(f.transport.calls),1)

    async def test_tool_reported_error_is_failed_and_invalid_contract_is_unknown(self):
        f=self.f
        for key,response,state in [('reported',{'content':[],'isError':True},'failed'),
                ('invalid',{'content':'not-an-array'},'result_unknown')]:
            item=await f.approve(await f.request(key=key))
            with patch.object(f.transport,'call',new=AsyncMock(return_value=response)) as invoke:
                result=await f.gate.dispatch(f.run['id'],item['id'],item['version'])
                self.assertEqual(result['state'],state);self.assertTrue(result['receipt_available'])
                invoke.assert_awaited_once()
