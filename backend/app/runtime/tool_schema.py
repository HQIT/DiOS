"""Additive invocation/approval ledger; independent of runtime schema v1."""
import hashlib
from pathlib import Path
from sqlalchemy import MetaData, Table, Column as C, String as S, Integer as I, Float as F, JSON as J, UniqueConstraint, Index, select, inspect
from .database import RuntimeErrorCode

metadata=MetaData()
versions=Table('runtime_tool_schema',metadata,C('version',I,primary_key=True),C('checksum',S(64),nullable=False))
invocations=Table('runtime_tool_invocations',metadata,
    C('id',S(64),primary_key=True),C('scope',S(160),nullable=False),C('run_id',S(64),nullable=False),
    C('attempt_id',S(64),nullable=False),C('request_key',S(160),nullable=False),C('request_digest',S(64),nullable=False),
    C('tool_id',S(160),nullable=False),C('registration_digest',S(64),nullable=False),C('binding',J,nullable=False),
    C('permission',J,nullable=False),C('arguments',J,nullable=False),C('arguments_digest',S(64),nullable=False),
    C('state',S(40),nullable=False),C('version',I,nullable=False),C('approval_state',S(32),nullable=False),
    C('approver',S(160)),C('approval_reason',S(2000)),C('expires_at',F,nullable=False),
    C('created_at',F,nullable=False),C('updated_at',F,nullable=False),C('dispatch_deadline',F),
    C('result_key',S(64)),C('reason',S(160)),
    UniqueConstraint('scope','run_id','attempt_id','request_key',name='uq_tool_request'))
Index('ix_tool_run_time',invocations.c.scope,invocations.c.run_id,invocations.c.created_at)
evidence=Table('runtime_tool_evidence',metadata,
    C('id',S(64),primary_key=True),C('scope',S(160),nullable=False),C('invocation_id',S(64),nullable=False),
    C('stage',S(40),nullable=False),C('decision',J,nullable=False),C('created_at',F,nullable=False))
Index('ix_tool_evidence',evidence.c.scope,evidence.c.invocation_id,evidence.c.created_at)
CHECKSUM=hashlib.sha256(Path(__file__).read_text(encoding='utf-8').encode()).hexdigest()

async def check(c):
    tables=set(await c.run_sync(lambda s:inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):return False
    if not set(metadata.tables).issubset(tables):raise RuntimeErrorCode('TOOL_SCHEMA_INCOMPLETE')
    rows=(await c.execute(select(versions))).mappings().all()
    if len(rows)!=1 or rows[0]['version']!=1 or rows[0]['checksum']!=CHECKSUM:
        raise RuntimeErrorCode('TOOL_SCHEMA_INCOMPATIBLE')
    return True

async def require(c):
    if not await check(c):raise RuntimeErrorCode('TOOL_MIGRATION_REQUIRED')

async def migrate(c):
    if await check(c):return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1,checksum=CHECKSUM))
    return True
