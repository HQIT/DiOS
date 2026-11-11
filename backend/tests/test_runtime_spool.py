import asyncio
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from sqlalchemy import text
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.spool import ReportSpool, Receipt, accept_report, flush_reports
from app.runtime.store import RuntimeStore


class SpoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'outbox.db'
        self.now = 1000.0
        self.spool = ReportSpool(self.path, clock=lambda: self.now)
        self.db = Database('sqlite+aiosqlite:///'+self.temp.name+'/runtime.db')
        await self.db.migrate()
        self.store = RuntimeStore(self.db, 'test')
        self.run = await self.store.submit('one', {'fixture': True})
        await self.store.register_worker('worker', 'instance', 'host', os.getpid())
        self.attempt = await self.store.claim('worker', 'instance')

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    def put(self, sequence=1, phase='preparing', spool=None, **detail):
        return (spool or self.spool).enqueue('test', self.run['id'], self.attempt['id'],
            'worker', 'instance', self.attempt['epoch'], sequence, phase, **detail)

    async def send(self, delivery):
        return await accept_report(self.store, delivery)

    async def test_offline_restart_and_utf8_resume(self):
        self.put(remote={'note': '中文证据😀'})
        self.put(2, 'creating_session')
        self.put(3, 'waiting_model', session_id='session', output=True, output_observed_at=self.now)
        async def disconnected(_): raise ConnectionError('network down')
        self.assertEqual((await flush_reports(self.spool, disconnected))['deferred'], 1)
        self.assertEqual(self.spool.status()['retained'], 3)
        await self.db.close()
        self.db = Database('sqlite+aiosqlite:///'+self.temp.name+'/runtime.db')
        self.store = RuntimeStore(self.db, 'test')
        restarted = ReportSpool(self.path, clock=lambda: self.now)
        self.assertIsNone(restarted.claim())
        self.now += 2
        self.assertEqual((await flush_reports(restarted, self.send))['acknowledged'], 3)
        run = await self.store.get(self.run['id'])
        self.assertEqual(run['attempts'][0]['state'], 'waiting_model')
        self.assertEqual(run['attempts'][0]['remote'], {'note': '中文证据😀'})
        self.assertEqual(run['attempts'][0]['last_output_at'], 1000.0)
        self.assertEqual(restarted.status()['retained'], 0)

    async def test_lost_commit_receipt_replays_once(self):
        self.put()
        async def lost_ack(delivery):
            await self.send(delivery)
            raise TimeoutError('reply lost after commit')
        await flush_reports(self.spool, lost_ack)
        events = await self.store.pending_events('audit')
        self.now += 2
        self.assertEqual((await flush_reports(self.spool, self.send))['acknowledged'], 1)
        self.assertEqual(len(await self.store.pending_events('audit')), len(events))

    async def test_quota_conflict_and_blocked_evidence(self):
        limited = ReportSpool(self.path, max_records=1)
        original = self.put(spool=limited)
        self.assertEqual(self.put(spool=limited), original)
        with self.assertRaisesRegex(RuntimeErrorCode, 'SPOOL_CONFLICT'): self.put(spool=limited, remote={'changed': True})
        with self.assertRaisesRegex(RuntimeErrorCode, 'SPOOL_FULL'): self.put(2, spool=limited)
        delivery = limited.claim()
        limited.defer(delivery, 'STALE_OWNER', blocked=True)
        with self.assertRaisesRegex(RuntimeErrorCode, 'SPOOL_FULL'): self.put(2, spool=limited)
        self.assertEqual(limited.status()['blocked'], 1)
        self.assertEqual(limited.status()['errors'], {'STALE_OWNER': 1})

    async def test_byte_limit_and_oversized_event_preserve_queue(self):
        tiny = ReportSpool(self.path, max_bytes=1)
        with self.assertRaisesRegex(RuntimeErrorCode, 'SPOOL_FULL'): self.put(spool=tiny)
        tiny_event = ReportSpool(self.path, max_event_bytes=1)
        with self.assertRaisesRegex(RuntimeErrorCode, 'SPOOL_EVENT_TOO_LARGE'): self.put(spool=tiny_event)
        self.assertEqual(self.spool.status()['retained'], 0)

    async def test_output_requires_stable_observation_time_and_rejects_invalid_time(self):
        with self.assertRaisesRegex(RuntimeErrorCode, 'OUTPUT_OBSERVATION_REQUIRED'): self.put(output=True)
        self.put(output=True, output_observed_at=-1)
        self.assertEqual((await flush_reports(self.spool, self.send))['blocked'], 1)
        self.assertIsNone((await self.store.get(self.run['id']))['attempts'][0]['last_output_at'])

    async def test_clock_skew_cannot_move_output_into_future_or_backwards(self):
        self.put(output=True, output_observed_at=9999999999)
        await flush_reports(self.spool, self.send)
        first = await self.store.get(self.run['id'])
        timestamp = first['attempts'][0]['last_output_at']
        self.assertLessEqual(timestamp, first['observed_at'])
        self.put(2, 'creating_session', output=True, output_observed_at=1)
        await flush_reports(self.spool, self.send)
        self.assertEqual((await self.store.get(self.run['id']))['attempts'][0]['last_output_at'], timestamp)

    async def test_two_relays_and_expired_delivery_token(self):
        self.put()
        first = self.spool.claim(2)
        other = ReportSpool(self.path, clock=lambda: self.now)
        self.assertIsNone(other.claim())
        self.now += 3
        second = other.claim()
        self.assertNotEqual(first.token, second.token)
        self.assertFalse(self.spool.acknowledge(first, Receipt(first.event_id, first.digest)))
        self.assertFalse(self.spool.defer(first, 'OLD_SENDER'))
        self.assertTrue(other.acknowledge(second, await self.send(second)))

    async def test_stale_owner_blocks_without_mutating_run(self):
        self.put()
        self.put(2, 'creating_session')
        async with self.db.transaction('test') as c:
            await c.execute(text('UPDATE runtime_attempts SET lease_until=0'))
        await self.store.expire()
        before = await self.store.get(self.run['id'])
        result = await flush_reports(self.spool, self.send)
        after = await self.store.get(self.run['id'])
        self.assertEqual(result['blocked'], 1)
        self.assertEqual((after['state'], after['version']), (before['state'], before['version']))
        self.assertEqual(after['state'], 'reconciliation_required')
        self.assertEqual(self.spool.status()['retained'], 2)
        self.assertIsNone(self.spool.status()['next_delivery_at'])
        self.assertIsNone(self.spool.claim())

    async def test_revoke_prevents_replay(self):
        self.put()
        run = await self.store.get(self.run['id'])
        await self.store.revoke(run['id'], run['version'])
        self.assertEqual((await flush_reports(self.spool, self.send))['blocked'], 1)
        self.assertEqual(self.spool.status()['errors'], {'AUTH_REVOKED': 1})

    async def test_throttle_backoff_and_independent_attempt_progress(self):
        self.put()
        self.put(2, 'creating_session')
        # Independent streams share no FIFO tie-break guarantee at identical timestamps.
        self.now += .1
        self.spool.enqueue('test', 'other-run', 'other-attempt', 'worker2', 'instance2', 1, 1, 'preparing')
        calls = []
        async def throttled(delivery):
            calls.append(delivery)
            raise RuntimeErrorCode('RATE_LIMITED')
        await flush_reports(self.spool, throttled)
        self.assertEqual(len(calls), 1)
        other = self.spool.claim()
        self.assertIsNotNone(other)  # the other stream is independent
        self.spool.acknowledge(other, Receipt(other.event_id, other.digest))
        self.now += 2
        await flush_reports(self.spool, throttled)
        self.assertEqual(self.spool.status()['errors'], {'RATE_LIMITED': 1})

    async def test_receipt_mismatch_and_corrupt_payload_retained(self):
        self.put()
        async def bad_receipt(d): return Receipt(d.event_id, 'wrong')
        self.assertEqual((await flush_reports(self.spool, bad_receipt))['blocked'], 1)
        self.assertEqual(self.spool.status()['retained'], 1)
        self.spool.enqueue('test', 'two', 'two', 'w', 'i', 1, 1, 'preparing')
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE reports SET payload=x'00' WHERE blocked=0")
        self.assertEqual((await flush_reports(self.spool, self.send))['blocked'], 1)
        self.assertEqual(self.spool.status()['retained'], 2)

    async def test_cancel_during_send_preserves_delivery(self):
        self.put()
        entered = asyncio.Event()
        async def hanging(_): entered.set(); await asyncio.Event().wait()
        task = asyncio.create_task(flush_reports(self.spool, hanging))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.spool.status()['retained'], 1)
        self.now += 2
        self.assertEqual((await flush_reports(self.spool, self.send))['acknowledged'], 1)

    async def test_scope_and_event_identity_are_checked(self):
        self.put()
        delivery = self.spool.claim()
        with self.assertRaisesRegex(RuntimeErrorCode, 'WORKER_MISMATCH'):
            await accept_report(RuntimeStore(self.db, 'other'), delivery)
        with self.assertRaisesRegex(RuntimeErrorCode, 'SPOOL_DIGEST_MISMATCH'):
            await self.send(replace(delivery, event_id='wrong'))

    async def test_real_process_crash_preserves_committed_report(self):
        script = '''import os,sys
from app.runtime.spool import ReportSpool
s=ReportSpool(sys.argv[1])
s.enqueue('test','run','attempt','worker','instance',1,1,'preparing',remote={'text':'中文'})
s.claim(0.01)
os._exit(77)
'''
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, str(self.path))
        self.assertEqual(await proc.wait(), 77)
        restarted = ReportSpool(self.path, clock=lambda: 9999999999)
        delivery = restarted.claim()
        self.assertIsNotNone(delivery)
        self.assertIn('中文', delivery.payload.decode('utf-8'))

    async def test_unrelated_or_future_schema_is_not_modified(self):
        path = Path(self.temp.name)/'foreign.db'
        with sqlite3.connect(path) as c: c.execute('CREATE TABLE business (id INTEGER)')
        with self.assertRaisesRegex(RuntimeErrorCode, 'SPOOL_SCHEMA_INCOMPATIBLE'): ReportSpool(path)
        with sqlite3.connect(self.path) as c: c.execute('UPDATE spool_meta SET version=99')
        with self.assertRaisesRegex(RuntimeErrorCode, 'SPOOL_SCHEMA_INCOMPATIBLE'): ReportSpool(self.path)

    async def test_cli_status_is_payload_free_and_missing_file_is_not_created(self):
        self.put(remote={'private': 'do-not-display'})
        root = Path(__file__).resolve().parents[2]
        async def cli(path):
            proc = await asyncio.create_subprocess_exec(sys.executable, str(root/'cli/dios'),
                'runtime', 'spool-status', '--file', str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate()
            self.assertEqual(err, b'')
            return proc.returncode, out
        code, out = await cli(self.path)
        self.assertEqual(code, 0)
        self.assertIn(b'"retained": 1', out)
        self.assertNotIn(b'do-not-display', out)
        missing = self.path.with_name('missing.db')
        code, out = await cli(missing)
        self.assertEqual(code, 2)
        self.assertIn(b'SPOOL_NOT_FOUND', out)
        self.assertFalse(missing.exists())
        empty = self.path.with_name('empty.db')
        empty.touch()
        code, out = await cli(empty)
        self.assertEqual(code, 2)
        self.assertIn(b'SPOOL_SCHEMA_INCOMPATIBLE', out)
        self.assertEqual(empty.stat().st_size, 0)


if __name__ == '__main__': unittest.main()
