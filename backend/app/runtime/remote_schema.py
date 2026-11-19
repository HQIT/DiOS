"""Additive remote Worker identity, operation receipts and bounded upload ledger."""
import hashlib
from pathlib import Path
from sqlalchemy import MetaData,Table,Column as C,String as S,Integer as I,Float as F,JSON as J,Boolean as B,select,inspect,text
from .database import RuntimeErrorCode
metadata=MetaData()
versions=Table('runtime_remote_schema',metadata,C('version',I,primary_key=True),C('checksum',S(64),nullable=False))
peers=Table('runtime_remote_workers',metadata,C('id',S(64),primary_key=True),C('scope',S(160),nullable=False),
    C('instance',S(64),nullable=False),C('certificate',S(64),nullable=False),C('machine_id',S(64),nullable=False),
    C('profile',S(160),nullable=False),C('created_at',F,nullable=False))
receipts=Table('runtime_remote_receipts',metadata,C('id',S(64),primary_key=True),C('scope',S(160),nullable=False),
    C('digest',S(64),nullable=False),C('response',J,nullable=False),C('created_at',F,nullable=False))
uploads=Table('runtime_remote_uploads',metadata,C('id',S(64),primary_key=True),C('scope',S(160),nullable=False),
    C('attempt_id',S(64),nullable=False),C('key',S(64),nullable=False),C('size',I,nullable=False),
    C('received',I,nullable=False),C('complete',B,nullable=False),C('created_at',F,nullable=False))
chunks=Table('runtime_remote_chunks',metadata,C('upload_id',S(64),primary_key=True),C('offset',I,primary_key=True),
    C('key',S(64),nullable=False),C('size',I,nullable=False))
CHECKSUM=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
async def check(c):
    tables=set(await c.run_sync(lambda s:inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):return False
    if not set(metadata.tables).issubset(tables):raise RuntimeErrorCode('REMOTE_SCHEMA_INCOMPLETE')
    rows=(await c.execute(select(versions))).mappings().all()
    if len(rows)!=1 or rows[0]['version']!=1 or rows[0]['checksum']!=CHECKSUM:raise RuntimeErrorCode('REMOTE_SCHEMA_INCOMPATIBLE')
    return True
async def require(c):
    if not await check(c):raise RuntimeErrorCode('REMOTE_MIGRATION_REQUIRED')
async def migrate(c):
    if await check(c):return False
    if c.dialect.name=='postgresql':
        await c.execute(text("ALTER TABLE runtime_attempts ALTER COLUMN process_instance TYPE TEXT"))
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1,checksum=CHECKSUM));return True
