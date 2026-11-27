"""D01-2.03: isolated native PostgreSQL dump/restore and POSIX installation recovery."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'backend'), str(ROOT / 'backend/tests')]
os.environ['DIOS_TEST_DOCKER'] = '1'
import qualify_shared_services as shared
from app.db.database import Base
from app.models.tables import Agent, A2ATask
from app.runtime.artifacts import LocalArtifacts
from app.runtime.backup_tree import capture_tree, inspect_tree
from app.runtime.credential_schema import credentials
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.lifecycle import Lifecycle
from app.runtime.object_access import ObjectPolicies
from app.runtime.provider_transfer import reflect, rows, row_digest, comparable
from app.runtime.schema import artifacts
from app.runtime.store import RuntimeStore
from app.services.installation_restore import hold_service
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

SOURCE = {}
TARGET = {}
OBSERVATIONS = {}


async def inventory(db):
    async with db.engine.connect() as conn:
        metadata = await reflect(conn)
        return {name: await rows(conn, table, 100000) for name, table in metadata.tables.items()}


def restore_dump(path):
    name = TARGET['container']
    shared.command('docker', 'cp', str(path), name + ':/tmp/runtime.dump')
    shared.command('docker', 'exec', '--env-file', str(TARGET['root'] / 'restore.env'), name,
                   'pg_restore', '-h', 'localhost', '-U', 'dios_owner', '-d', 'dios',
                   '--single-transaction', '--exit-on-error', '--no-owner', '--no-acl', '/tmp/runtime.dump')


class SharedRestoreQualification(shared.SharedQualification):
    async def test_clean_shared_restore(self):
        # Real CLI gateway and two independent mTLS Worker processes, including E2AG and TLS roles.
        await super().test_shared_state_objects_roles_and_tls()
        native = dict(shared.OBSERVATIONS)
        original_runs = [await self.store.get(r['id']) for r in native['runs']]
        for value in original_runs:
            value.pop('observed_at')
        async with self.fixture.db.read(self.store.scope) as conn:
            linked = [dict(r) for r in (await conn.execute(select(artifacts))).mappings()]
        principal = dict(issuer='fixture', audience='fixture', scope=self.store.scope,
                         id='alice', subject='user:alice', actor='app:test')
        ref = dict(kind='artifact', id=linked[0]['id'], version=1)
        await ObjectPolicies(self.fixture.db, self.store.scope).change(
            ref, [dict(principal=principal, actions=['object.read', 'object.download'],
                       expires_at=time.time()+3600)], 0, 'restore-policy', 'operator:test')
        attempt = original_runs[0]['attempts'][0]
        async with self.fixture.db.transaction(self.store.scope) as conn:
            await conn.execute(credentials.insert().values(
                id='recovery-stale-credential', scope=self.store.scope, run_id=original_runs[0]['id'],
                attempt_id=attempt['id'], binding={'fixture': 'stale credential'}, authority={},
                key_id='fixture', state='active', version=3, nonce='fixture', token_hash='a'*64,
                previous_hash='b'*64, previous_until=time.time()+300, expires_at=time.time()+600,
                created_at=time.time(), updated_at=time.time()))
        lifecycle = Lifecycle(self.fixture.db, self.store.scope)
        await lifecycle.begin_drain()
        self.assertTrue((await lifecycle.wait_drained())['ready_for_cutover'])
        await self.stop_cli_gateway()
        self.gateway_log.close()
        self.assertTrue(all(p.returncode == 0 for p in self.children))
        self.fixture.provider.close()
        # The compatibility service database remains an independently owned SQLite component.
        service_path = self.root / 'service.sqlite'
        service = create_async_engine('sqlite+aiosqlite:///' + str(service_path))
        async with service.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(service)() as session:
            session.add(Agent(id='restored-agent', name='恢复验证'))
            session.add(A2ATask(id='restored-service-task', agent_id='restored-agent',
                                status='completed', message={'text': '中文完整🙂'}))
            await session.commit()
        await service.dispose()
        before = await inventory(self.fixture.db)
        await self.fixture.db.close()
        # All fixture writers are stopped; the existing capture_tree rejects unstable files/symlinks.
        bundle = SOURCE['root'] / 'installation-backup'
        tree = await asyncio.to_thread(capture_tree, self.root, bundle, lambda: None)
        dump = await asyncio.to_thread(shared.backup_dump)
        dump_path = SOURCE['root'] / 'runtime.dump'
        self.assertEqual(hashlib.sha256(dump_path.read_bytes()).hexdigest(), dump['sha256'])
        # A source-control outage follows the consistent backup; target has independent storage.
        await asyncio.to_thread(shared.command, 'docker', 'stop', SOURCE['container'])
        started = time.monotonic()
        target = Database(TARGET['runtime_url'])
        self.addAsyncCleanup(target.close)
        self.assertEqual(await inventory(target), {})
        bad = SOURCE['root'] / 'truncated.dump'
        bad.write_bytes(dump_path.read_bytes()[:-64])
        with self.assertRaisesRegex(RuntimeError, 'QUALIFICATION_COMMAND_FAILED'):
            await asyncio.to_thread(restore_dump, bad)
        self.assertEqual(await inventory(target), {})
        await asyncio.to_thread(restore_dump, dump_path)
        restored = TARGET['root'] / 'restored-installation'
        restored_tree = await asyncio.to_thread(capture_tree, bundle, restored, lambda: None)
        self.assertEqual(restored_tree, tree)
        after = await inventory(target)
        self.assertEqual(set(after), set(before))
        table_digests = {}
        for name in before:
            old, new = row_digest(before[name]), row_digest(after[name])
            self.assertEqual(old, new, name)
            table_digests[name] = dict(old, matched=True)
        # Nonempty restore is rejected, not silently merged or replaced; transaction rollback preserves data.
        with self.assertRaisesRegex(RuntimeError, 'QUALIFICATION_COMMAND_FAILED'):
            await asyncio.to_thread(restore_dump, dump_path)
        replay = await inventory(target)
        self.assertEqual({n: row_digest(r) for n, r in replay.items()},
                         {n: row_digest(r) for n, r in after.items()})
        # Private target remains unpublished throughout; use the existing recovery transformations.
        await Lifecycle(target, self.store.scope).hold_restored()
        held = await inventory(target)
        for name in before:
            expected, actual = comparable(name, before[name], held[name])
            self.assertEqual(expected, actual, name)
        (restored / 'service.sqlite').rename(restored / 'service.pending.sqlite')
        service_hold = hold_service(restored, [])
        (restored / 'service.pending.sqlite').rename(restored / 'service.sqlite')
        with sqlite3.connect(restored / 'service.sqlite') as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(), ('ok',))
            self.assertEqual(db.execute("SELECT status FROM a2a_tasks WHERE id='restored-service-task'").fetchone(), ('completed',))
            self.assertEqual(db.execute("SELECT mode FROM managed_runtime_control WHERE scope='__dios_installation_restore__'").fetchone(), ('restore_hold',))
        store = RuntimeStore(target, self.store.scope)
        for original in original_runs:
            actual = await store.get(original['id'])
            actual.pop('observed_at')
            self.assertEqual(original, actual)
        objects = LocalArtifacts(restored / 'artifacts')
        self.addCleanup(objects.close)
        for row in linked:
            blob = objects.verify(row['storage_key'])
            self.assertEqual(blob.digest, row['digest'])
        first = objects.path(linked[0]['storage_key'])
        original_bytes = first.read_bytes()
        first.write_bytes(original_bytes + b'corrupt')
        with self.assertRaisesRegex(RuntimeErrorCode, 'ARTIFACT_DIGEST_MISMATCH'):
            objects.verify(linked[0]['storage_key'])
        first.write_bytes(original_bytes)
        objects.verify(linked[0]['storage_key'])
        policy = ObjectPolicies(target, self.store.scope)
        await policy.check(ref, principal, 'object.download', audit=False)
        with self.assertRaisesRegex(RuntimeErrorCode, 'NOT_FOUND'):
            await policy.check(ref, dict(principal, id='bob', subject='user:bob'), 'object.download', audit=False)
        for operation in (store.submit('unexpected-new-task', {}), store.claim('worker', 'instance'),
                          Lifecycle(target, self.store.scope).resume(0)):
            with self.assertRaisesRegex(RuntimeErrorCode, 'RESTORE_RECONCILIATION_REQUIRED'):
                await operation
        async with target.read(self.store.scope) as conn:
            credential = (await conn.execute(select(credentials).where(credentials.c.id=='recovery-stale-credential'))).mappings().one()
        self.assertEqual((credential['state'], credential['version'], credential['previous_until']), ('revoked', 4, None))
        restored_backup = Database(TARGET['backup_url'])
        self.addAsyncCleanup(restored_backup.close)
        from sqlalchemy.exc import DBAPIError
        with self.assertRaises(DBAPIError):
            async with restored_backup.engine.begin() as conn:
                await conn.execute(text('UPDATE runtime_runs SET version=version WHERE false'))
        with self.assertRaises(DBAPIError):
            async with target.engine.begin() as conn:
                await conn.execute(text('CREATE TABLE forbidden_restore_ddl(id integer)'))
        self.assertEqual(inspect_tree(bundle, lambda: None), tree)
        OBSERVATIONS.update(dict(native=native, database_backup=dump,
            table_digests=table_digests, tables=len(before), rows=sum(len(r) for r in before.values()),
            files=len(tree['files']), file_bytes=tree['bytes'],
            source_stopped=True, independent_empty_postgres=True,
            truncated_dump_rolled_back=True, nonempty_target_rejected_without_changes=True,
            complete_tree_matched=True, history_identity_matched=True,
            artifact_references_verified=len(linked), corrupted_artifact_rejected=True,
            acl_allowed_alice_denied_bob=True, runtime_ddl_denied=True, backup_mutation_denied=True,
            runtime_and_service_restore_hold=True, old_credential_revoked=True,
            unexpected_submit_claim_resume_rejected=True, service_recovery=service_hold,
            restore_and_verification_seconds=round(time.monotonic()-started, 3)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--postgres-image', default='postgres:16-alpine')
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    shared.prepare(root / 'source', args.postgres_image)
    SOURCE.update(shared.CONFIG)
    shared.CONFIG.clear()
    shared.prepare(root / 'target', SOURCE['postgres_image'])
    TARGET.update(shared.CONFIG)
    shared.command('docker', 'cp', str(TARGET['root'] / 'pg-ca.pem'), TARGET['container'] + ':/tmp/pg-ca.pem')
    shared.private(TARGET['root'] / 'restore.env', 'PGPASSWORD=' + TARGET['owner_password'] +
                   '\nPGSSLMODE=verify-full\nPGSSLROOTCERT=/tmp/pg-ca.pem\n')
    shared.CONFIG.clear()
    shared.CONFIG.update(SOURCE)
    combined = root / 'trusted-fixture-ca.pem'
    combined.write_bytes((SOURCE['root'] / 'pg-ca.pem').read_bytes() + (TARGET['root'] / 'pg-ca.pem').read_bytes())
    os.environ['PGSSLROOTCERT'] = str(combined)
    asyncio.run(shared.migrate())
    SharedRestoreQualification.__unittest_skip__ = False
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        SharedRestoreQualification('test_clean_shared_restore')]))
    report = dict(work_item='D01-2.03', passed=result.wasSuccessful() and not result.skipped and bool(OBSERVATIONS),
        tests=result.testsRun, errors=len(result.errors), failures=len(result.failures), skipped=len(result.skipped),
        postgres_image=SOURCE['postgres_image'], observations=OBSERVATIONS,
        source_sha256={p:hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in [
            'scripts/qualify_shared_restore.py','scripts/qualify_shared_services.py',
            'backend/app/runtime/lifecycle.py','backend/app/runtime/provider_transfer.py',
            'backend/app/runtime/backup_tree.py','backend/app/services/installation_restore.py','deploy/shared/roles.sql']},
        limits=['Native PostgreSQL 16 and POSIX objects on one Linux/WSL host; two independent mTLS Worker processes before backup.',
                'Quiesced consistent backup; target remains unpublished and restore-held after verification.',
                'No shared-backend execution activation, live incremental replication, automatic failover or enterprise capacity claim.',
                'All credentials, private keys, fixture logs and dumps remain in the private output directory.'])
    (root / 'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('passed','tests','errors','failures','skipped')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())