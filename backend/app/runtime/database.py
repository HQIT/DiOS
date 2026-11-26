"""Explicit migrations and database-owned clock/serialization for both providers."""
from contextlib import asynccontextmanager
import asyncio
import hashlib
from pathlib import Path
from sqlalchemy import select, text, inspect, event
from sqlalchemy.ext.asyncio import create_async_engine
from .schema import metadata, versions
from .write_queue import WriteGate, WriteQueueError

SCHEMA_VERSION = 1
# The v1 schema source is frozen after release. Add a migration for future changes.
SCHEMA_CHECKSUM = hashlib.sha256(Path(__file__).with_name('schema.py').read_text(encoding='utf-8').encode('utf-8')).hexdigest()

class RuntimeErrorCode(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class DurableRejection(RuntimeErrorCode):
    """A rejected operation with a terminal security observation to retain."""

    async def persist_after_rollback(self, db, scope):
        raise NotImplementedError


class _BufferedConnection:
    """Finish each buffered DB operation before propagating task cancellation.

    Cancelling aiosqlite between execute and fetchall can strand an SQLite cursor
    even after connection invalidation. The driver operation must finish first.
    """
    def __init__(self, connection): self.connection = connection
    def __getattr__(self, name): return getattr(self.connection, name)
    async def _finish(self, awaitable):
        task = asyncio.ensure_future(awaitable)
        try: return await asyncio.shield(task)
        except asyncio.CancelledError:
            try: await task
            finally: raise
    async def execute(self, *args, **kwargs):
        return await self._finish(self.connection.execute(*args, **kwargs))
    async def run_sync(self, *args, **kwargs):
        return await self._finish(self.connection.run_sync(*args, **kwargs))
    async def commit(self):
        return await self._finish(self.connection.commit())
    async def rollback(self):
        return await self._finish(self.connection.rollback())

class Database:
    def __init__(self, url, *, write_queue_capacity=64, write_queue_timeout=5.0):
        self.engine = create_async_engine(url)
        self._storage_lease=None
        self._closed=False
        if self.engine.dialect.name=='sqlite' and self.engine.url.database not in {None,'',':memory:'}:
            from .storage_lease import StorageLease,database_lock
            self._storage_lease=StorageLease(database_lock(self.engine.url.database))
        if self.engine.dialect.name not in {'sqlite', 'postgresql'}:
            raise RuntimeErrorCode('UNSUPPORTED_DATABASE')
        self.write_gate = WriteGate(write_queue_capacity, write_queue_timeout) if self.engine.dialect.name == 'sqlite' else None
        if self.write_gate:
            @event.listens_for(self.engine.sync_engine, 'connect')
            def configure_sqlite(connection, _):
                cursor = connection.cursor()
                cursor.execute('PRAGMA busy_timeout=15000')
                cursor.execute('PRAGMA foreign_keys=ON')
                cursor.execute('PRAGMA recursive_triggers=ON')
                cursor.execute('PRAGMA synchronous=FULL')
                cursor.close()

    @asynccontextmanager
    async def _write_slot(self):
        if self.write_gate is None:
            yield
            return
        try:
            async with self.write_gate.slot():
                yield
        except WriteQueueError as exc:
            raise RuntimeErrorCode(exc.code) from exc

    @asynccontextmanager
    async def read(self, scope):
        if self._closed:raise RuntimeErrorCode('DATABASE_CLOSED')
        if self.write_gate is None:
            # Preserve the existing PostgreSQL snapshot/serialization contract.
            async with self.transaction(scope) as c:
                yield c
            return
        async with self.engine.connect() as c:
            reader = _BufferedConnection(c)
            try:
                await reader.execute(text('PRAGMA query_only=ON'))
                await reader.execute(text('BEGIN'))
                await self.check_schema(reader)
                yield reader
            except asyncio.CancelledError:
                # Interrupted aiosqlite cursors must not return a read lock to the pool.
                cleanup = asyncio.create_task(c.invalidate())
                try: await asyncio.shield(cleanup)
                except asyncio.CancelledError: await cleanup
                raise
            finally:
                if not c.invalidated:
                    async def reset_read_connection():
                        try:
                            await c.rollback()
                            await c.execute(text('PRAGMA query_only=OFF'))
                            await c.commit()
                        except BaseException:
                            await c.invalidate()
                            raise
                    cleanup = asyncio.create_task(reset_read_connection())
                    try: await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise

    @asynccontextmanager
    async def transaction(self, scope, *, check=True):
        try:
            async with self._transaction(scope, check=check) as c:
                yield c
        except DurableRejection as rejection:
            # The rejected operation has fully rolled back and released its
            # connection/write slot. Persist only the security observation.
            task = asyncio.create_task(rejection.persist_after_rollback(self, scope))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            raise

    @asynccontextmanager
    async def _transaction(self, scope, *, check=True):
        if self._closed:raise RuntimeErrorCode('DATABASE_CLOSED')
        async with self._write_slot(), self.engine.connect() as connection:
            c = _BufferedConnection(connection) if self.write_gate else connection
            try:
                if c.dialect.name == 'sqlite':
                    await c.execute(text('BEGIN IMMEDIATE'))
                else:
                    key = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], 'big', signed=True)
                    await c.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
                if check:
                    await self.check_schema(c)
                yield c
                await c.commit()
            except BaseException:
                await c.rollback()
                raise

    async def check_schema(self, c):
        exists = await c.run_sync(lambda s: inspect(s).has_table('runtime_schema'))
        if not exists:
            raise RuntimeErrorCode('MIGRATION_REQUIRED')
        rows = (await c.execute(select(versions))).mappings().all()
        if len(rows) != 1 or rows[0]['version'] != SCHEMA_VERSION:
            raise RuntimeErrorCode('SCHEMA_INCOMPATIBLE')
        if rows[0]['checksum'] != SCHEMA_CHECKSUM:
            raise RuntimeErrorCode('MIGRATION_CHECKSUM_MISMATCH')

    async def migrate(self):
        from .history_schema import migrate as migrate_history
        from .history_archive_schema import migrate as migrate_history_archive
        from .lifecycle_schema import migrate as migrate_lifecycle
        from .work_auth_schema import migrate as migrate_work_auth
        from .tool_schema import migrate as migrate_tools
        from .credential_schema import migrate as migrate_credentials
        from .remote_schema import migrate as migrate_remote
        from .fault_schema import migrate as migrate_faults
        from .identity_check_schema import migrate as migrate_identity_checks
        from .logout_schema import migrate as migrate_logout
        from .delegation_schema import migrate as migrate_delegation
        from .delegation_identity_schema import migrate as migrate_delegation_identity
        from .delegation_check_schema import migrate as migrate_delegation_check
        from .schedule_schema import migrate as migrate_schedules
        from .child_schema import migrate as migrate_children
        from .child_handoff_schema import migrate as migrate_handoffs
        from .capacity_schema import migrate as migrate_capacity
        from .execution_budget_schema import migrate as migrate_execution_budgets
        from .tool_budget_schema import migrate as migrate_tool_budgets
        from .usage_schema import migrate as migrate_usage
        from .outcome_schema import migrate as migrate_outcomes
        from .monitor_schema import migrate as migrate_monitors
        from .revision_schema import migrate as migrate_revisions
        from .deployment_schema import migrate as migrate_deployments
        from .compatibility_schema import migrate as migrate_compatibility
        from .object_schema import migrate as migrate_objects
        from .object_access_schema import migrate as migrate_object_access
        from .object_lifetime_schema import migrate as migrate_object_lifetime
        from .object_collection_schema import migrate as migrate_object_collection
        from .knowledge_schema import migrate as migrate_knowledge
        async with self.transaction('__runtime_migration__', check=False) as c:
            existing = await c.run_sync(lambda s: inspect(s).get_table_names())
            if 'runtime_schema' in existing:
                await self.check_schema(c)
                actual = set(existing)
                if not set(metadata.tables).issubset(actual):
                    raise RuntimeErrorCode('SCHEMA_INCOMPLETE')
                for name, table in metadata.tables.items():
                    columns = await c.run_sync(lambda s, n=name: inspect(s).get_columns(n))
                    if not set(table.columns.keys()).issubset({col['name'] for col in columns}):
                        raise RuntimeErrorCode('SCHEMA_INCOMPLETE')
                changed = await migrate_history(c)
                archive_changed = await migrate_history_archive(c)
                lifecycle_changed = await migrate_lifecycle(c)
                auth_changed = await migrate_work_auth(c)
                tools_changed = await migrate_tools(c)
                credentials_changed = await migrate_credentials(c)
                remote_changed = await migrate_remote(c)
                fault_changed = await migrate_faults(c)
                identity_changed = await migrate_identity_checks(c)
                logout_changed = await migrate_logout(c)
                delegation_changed = await migrate_delegation(c)
                delegation_identity_changed = await migrate_delegation_identity(c)
                delegation_check_changed = await migrate_delegation_check(c)
                schedules_changed = await migrate_schedules(c)
                children_changed = await migrate_children(c)
                handoffs_changed = await migrate_handoffs(c)
                capacity_changed = await migrate_capacity(c)
                budget_changed = await migrate_execution_budgets(c)
                tool_budget_changed = await migrate_tool_budgets(c)
                usage_changed = await migrate_usage(c)
                outcome_changed = await migrate_outcomes(c)
                monitor_changed = await migrate_monitors(c)
                revision_changed = await migrate_revisions(c)
                deployment_changed = await migrate_deployments(c)
                compatibility_changed = await migrate_compatibility(c)
                objects_changed = await migrate_objects(c)
                object_access_changed = await migrate_object_access(c)
                object_lifetime_changed = await migrate_object_lifetime(c)
                object_collection_changed = await migrate_object_collection(c)
                knowledge_changed = await migrate_knowledge(c)
                return {'version': SCHEMA_VERSION, 'history_version': 1, 'history_archive_version': 1, 'lifecycle_version': 1,
                        'work_authorization_version': 1, 'tool_version': 1, 'credential_version': 1, 'remote_version': 1, 'fault_version': 1, 'identity_check_version': 1, 'logout_version': 1, 'delegation_version': 1, 'delegation_identity_version': 1, 'delegation_check_version': 1, 'schedule_version': 1, 'child_version': 1, 'child_handoff_version': 1, 'capacity_version': 1, 'execution_budget_version': 1, 'tool_budget_version': 1, 'usage_version': 1, 'outcome_version': 1, 'monitor_version': 1, 'revision_version': 1, 'deployment_version': 1, 'knowledge_version': 1, 'object_collection_version': 1, 'object_lifetime_version': 1, 'object_access_version': 1, 'object_version': 1, 'compatibility_version': 1, 'changed': knowledge_changed or object_collection_changed or object_lifetime_changed or object_access_changed or objects_changed or compatibility_changed or deployment_changed or revision_changed or monitor_changed or outcome_changed or usage_changed or tool_budget_changed or budget_changed or capacity_changed or handoffs_changed or children_changed or schedules_changed or archive_changed or delegation_check_changed or delegation_identity_changed or delegation_changed or logout_changed or identity_changed or fault_changed or changed or lifecycle_changed or auth_changed or tools_changed or credentials_changed or remote_changed}
            if any(n.startswith('runtime_') for n in existing):
                raise RuntimeErrorCode('UNVERSIONED_RUNTIME_SCHEMA')
            await c.run_sync(metadata.create_all)
            await c.execute(versions.insert().values(version=SCHEMA_VERSION, checksum=SCHEMA_CHECKSUM))
            await migrate_history(c)
            await migrate_history_archive(c)
            await migrate_lifecycle(c)
            await migrate_work_auth(c)
            await migrate_tools(c)
            await migrate_credentials(c)
            await migrate_remote(c)
            await migrate_faults(c)
            await migrate_identity_checks(c)
            await migrate_logout(c)
            await migrate_delegation(c)
            await migrate_delegation_identity(c)
            await migrate_delegation_check(c)
            await migrate_schedules(c)
            await migrate_children(c)
            await migrate_handoffs(c)
            await migrate_capacity(c)
            await migrate_execution_budgets(c)
            await migrate_tool_budgets(c)
            await migrate_usage(c)
            await migrate_outcomes(c)
            await migrate_monitors(c)
            await migrate_revisions(c)
            await migrate_deployments(c)
            await migrate_compatibility(c)
            await migrate_objects(c)
            await migrate_object_access(c)
            await migrate_object_lifetime(c)
            await migrate_object_collection(c)
            await migrate_knowledge(c)
            return {'version': SCHEMA_VERSION, 'history_version': 1, 'history_archive_version': 1, 'lifecycle_version': 1, 'work_authorization_version': 1, 'tool_version': 1, 'credential_version': 1, 'remote_version': 1, 'fault_version': 1, 'identity_check_version': 1, 'logout_version': 1, 'delegation_version': 1, 'delegation_identity_version': 1, 'delegation_check_version': 1, 'schedule_version': 1, 'child_version': 1, 'child_handoff_version': 1, 'capacity_version': 1, 'execution_budget_version': 1, 'tool_budget_version': 1, 'usage_version': 1, 'outcome_version': 1, 'monitor_version': 1, 'revision_version': 1, 'deployment_version': 1, 'knowledge_version': 1, 'object_collection_version': 1, 'object_lifetime_version': 1, 'object_access_version': 1, 'object_version': 1, 'compatibility_version': 1, 'changed': True}

    @staticmethod
    async def now(c):
        query = "SELECT (julianday('now') - 2440587.5) * 86400.0" if c.dialect.name == 'sqlite' else 'SELECT EXTRACT(EPOCH FROM clock_timestamp())'
        return float((await c.execute(text(query))).scalar_one())

    async def close(self):
        if self.write_gate:
            await self.write_gate.drain()
        await self.engine.dispose()
        self._closed=True
        if self._storage_lease:self._storage_lease.close()
