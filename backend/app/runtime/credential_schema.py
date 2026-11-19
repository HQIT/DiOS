"""Independent, additive certificate-bound credential state; never stores token plaintext."""
import hashlib
from pathlib import Path
from sqlalchemy import MetaData,Table,Column as C,String as S,Integer as I,Float as F,JSON as J,select,inspect,Index
from .database import RuntimeErrorCode
metadata=MetaData()
versions=Table('runtime_credential_schema',metadata,C('version',I,primary_key=True),C('checksum',S(64),nullable=False))
credentials=Table('runtime_worker_credentials',metadata,
    C('id',S(64),primary_key=True),C('scope',S(160),nullable=False),C('run_id',S(64),nullable=False),
    C('attempt_id',S(64),nullable=False),C('binding',J,nullable=False),C('authority',J,nullable=False),
    C('key_id',S(64),nullable=False),C('state',S(32),nullable=False),C('version',I,nullable=False),
    C('nonce',S(128),nullable=False),C('token_hash',S(64),nullable=False),C('previous_hash',S(64)),
    C('previous_until',F),C('expires_at',F,nullable=False),C('created_at',F,nullable=False),C('updated_at',F,nullable=False))
Index('ix_worker_credential_run',credentials.c.scope,credentials.c.run_id)
CHECKSUM=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
async def check(c):
    tables=set(await c.run_sync(lambda s:inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):return False
    if not set(metadata.tables).issubset(tables):raise RuntimeErrorCode('CREDENTIAL_SCHEMA_INCOMPLETE')
    rows=(await c.execute(select(versions))).mappings().all()
    if len(rows)!=1 or rows[0]['version']!=1 or rows[0]['checksum']!=CHECKSUM:raise RuntimeErrorCode('CREDENTIAL_SCHEMA_INCOMPATIBLE')
    return True
async def require(c):
    if not await check(c):raise RuntimeErrorCode('CREDENTIAL_MIGRATION_REQUIRED')
async def migrate(c):
    if await check(c):return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1,checksum=CHECKSUM));return True
