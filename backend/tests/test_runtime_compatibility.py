import asyncio,json,os,sys,tempfile,unittest
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.runtime.database import Database,RuntimeErrorCode
from app.db.database import Base
from app.models.tables import Agent
from app.api.os.agents import list_agents

class MigrationCompatibility(unittest.IsolatedAsyncioTestCase):
    async def test_existing_agent_api_survives_additive_migration(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database('sqlite+aiosqlite:///'+d+'/legacy.db')
            try:
                async with db.engine.begin() as c:await c.run_sync(Base.metadata.create_all)
                sessions=async_sessionmaker(db.engine,expire_on_commit=False)
                async with sessions() as session:
                    session.add(Agent(id='legacy-1',name='已有员工',capabilities={'source':'legacy'},env={}))
                    await session.commit()
                await db.migrate()
                async with sessions() as session:
                    result=await list_agents(group=None,mode=None,db=session)
                self.assertEqual(result[0]['id'],'legacy-1')
                self.assertEqual(result[0]['name'],'已有员工')
                self.assertEqual(result[0]['capabilities'],{'source':'legacy'})
            finally:await db.close()

    async def test_partial_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database('sqlite+aiosqlite:///'+d+'/broken.db')
            try:
                async with db.engine.begin() as c:await c.execute(text('CREATE TABLE runtime_orphan (id INTEGER)'))
                with self.assertRaisesRegex(RuntimeErrorCode,'UNVERSIONED_RUNTIME_SCHEMA'):await db.migrate()
            finally:await db.close()

    async def test_versioned_missing_table_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database('sqlite+aiosqlite:///'+d+'/broken.db')
            try:
                await db.migrate()
                async with db.engine.begin() as c:await c.execute(text('DROP TABLE runtime_imports'))
                with self.assertRaisesRegex(RuntimeErrorCode,'SCHEMA_INCOMPLETE'):await db.migrate()
            finally:await db.close()

    async def test_existing_cli_runtime_migrate_submit_and_show(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(__file__).resolve().parents[2]
            url='sqlite+aiosqlite:///'+d+'/cli.db'
            path=Path(d)/'input.json';path.write_text('{"中文":"完整"}',encoding='utf-8')
            async def call(*args):
                proc=await asyncio.create_subprocess_exec(sys.executable,str(root/'cli/dios'),'runtime','--database-url',url,*args,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                out,err=await proc.communicate();self.assertEqual(proc.returncode,0,err.decode())
                return json.loads(out)
            self.assertTrue((await call('migrate'))['changed'])
            first=await call('submit','--key','cli-one','--file',str(path))
            again=await call('submit','--key','cli-one','--file',str(path))
            self.assertEqual(first['id'],again['id'])
            self.assertEqual((await call('show',first['id']))['input'],{'中文':'完整'})

if __name__=='__main__':unittest.main()
