"""Additive work authorization component; never changes runtime schema v1."""
import hashlib
from pathlib import Path
from sqlalchemy import MetaData, Table, Column as C, String as S, Integer as I, Float as F, JSON as J
from sqlalchemy import select, inspect

metadata=MetaData()
versions=Table('runtime_work_auth_schema',metadata,C('version',I,primary_key=True),C('checksum',S(64),nullable=False))
work=Table('runtime_work_authorizations',metadata,
    C('id',S(80),primary_key=True),C('scope',S(160),nullable=False),C('run_id',S(64),unique=True,nullable=False),
    C('subject',S(160),nullable=False),C('actor',S(160),nullable=False),
    C('policy_id',S(160),nullable=False),C('policy_digest',S(64),nullable=False),
    C('profile_id',S(160),nullable=False),C('profile_digest',S(64),nullable=False),
    C('permissions',J,nullable=False),C('secret_refs',J,nullable=False),
    C('state',S(32),nullable=False),C('version',I,nullable=False),C('authorization_epoch',I,nullable=False),
    C('created_at',F,nullable=False),C('expires_at',F,nullable=False))
grants=Table('runtime_work_grants',metadata,
    C('attempt_id',S(64),primary_key=True),C('authorization_id',S(80),nullable=False),C('run_id',S(64),nullable=False),
    C('worker_id',S(64),nullable=False),C('worker_instance',S(64),nullable=False),C('lease_epoch',I,nullable=False),
    C('authorization_epoch',I,nullable=False),C('session_id',S(160)),C('version',I,nullable=False),
    C('state',S(32),nullable=False),C('expires_at',F,nullable=False))
decisions=Table('runtime_work_decisions',metadata,
    C('id',S(64),primary_key=True),C('scope',S(160),nullable=False),C('run_id',S(64),nullable=False),
    C('attempt_id',S(64)),C('action',S(80),nullable=False),C('decision',J,nullable=False),C('created_at',F,nullable=False))
CHECKSUM=hashlib.sha256(Path(__file__).read_text(encoding='utf-8').encode()).hexdigest()

async def check(c):
    from .database import RuntimeErrorCode
    tables=set(await c.run_sync(lambda s:inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):return False
    if not set(metadata.tables).issubset(tables):raise RuntimeErrorCode('WORK_AUTH_SCHEMA_INCOMPLETE')
    rows=(await c.execute(select(versions))).mappings().all()
    if len(rows)!=1 or rows[0]['version']!=1:raise RuntimeErrorCode('WORK_AUTH_SCHEMA_INCOMPATIBLE')
    if rows[0]['checksum']!=CHECKSUM:raise RuntimeErrorCode('WORK_AUTH_CHECKSUM_MISMATCH')
    return True

async def require(c):
    from .database import RuntimeErrorCode
    if not await check(c):raise RuntimeErrorCode('WORK_AUTH_MIGRATION_REQUIRED')

async def migrate(c):
    if await check(c):return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1,checksum=CHECKSUM))
    return True
