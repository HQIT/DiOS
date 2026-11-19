"""Additive v1 failure policy bindings, retry plans and shared circuit ownership."""
import hashlib
from pathlib import Path
from sqlalchemy import MetaData,Table,Column as C,String as S,Integer as I,Float as F,JSON as J,select,inspect
from .database import RuntimeErrorCode
metadata=MetaData()
versions=Table('runtime_fault_schema',metadata,C('version',I,primary_key=True),C('checksum',S(64),nullable=False))
bindings=Table('runtime_fault_bindings',metadata,C('attempt_id',S(64),primary_key=True),C('scope',S(160),nullable=False),
    C('resource',S(160),nullable=False),C('policy',J,nullable=False))
circuits=Table('runtime_circuits',metadata,C('scope',S(160),primary_key=True),C('resource',S(160),primary_key=True),
    C('state',S(32),nullable=False),C('failures',I,nullable=False),C('retry_at',F,nullable=False),
    C('probe_attempt',S(64)),C('reason',S(80)),C('updated_at',F,nullable=False))
plans=Table('runtime_retry_plans',metadata,C('run_id',S(64),primary_key=True),C('scope',S(160),nullable=False),
    C('attempt_id',S(64),nullable=False),C('resource',S(160),nullable=False),C('state',S(32),nullable=False),
    C('category',S(40),nullable=False),C('code',S(160),nullable=False),C('retry_at',F),C('reason',S(160)),C('updated_at',F,nullable=False))
CHECKSUM=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
async def check(c):
    names=set(await c.run_sync(lambda s:inspect(s).get_table_names()))
    if not names.intersection(metadata.tables):return False
    if not set(metadata.tables).issubset(names):raise RuntimeErrorCode('FAULT_SCHEMA_INCOMPLETE')
    rows=(await c.execute(select(versions))).mappings().all()
    if len(rows)!=1 or rows[0]['version']!=1 or rows[0]['checksum']!=CHECKSUM:raise RuntimeErrorCode('FAULT_SCHEMA_INCOMPATIBLE')
    return True
async def migrate(c):
    if await check(c):return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1,checksum=CHECKSUM));return True
