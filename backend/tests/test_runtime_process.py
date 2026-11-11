import asyncio,json,os,sys,tempfile,time,unittest
from pathlib import Path
from uuid import uuid4
from app.runtime.database import Database,RuntimeErrorCode
from app.runtime.store import RuntimeStore
from app.runtime.process import supervise

class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def run_child(self,source,**options):
        return await supervise([sys.executable,'-c',source],cwd=Path.cwd(),env=dict(os.environ),**options)

    async def test_exit_retains_partial_output(self):
        r=await self.run_child("import sys; print('部分结果',flush=True); sys.exit(7)")
        self.assertEqual(r.exit_code,7);self.assertEqual(r.reason,'PROCESS_EXITED')
        self.assertIn('部分结果',r.stdout.decode());self.assertGreater(r.pid,0)

    async def test_lifetime_budget_kills_and_retains_output(self):
        r=await self.run_child("import time; print('partial',flush=True); time.sleep(30)",timeout=.2)
        self.assertEqual(r.reason,'PROCESS_DEADLINE_EXCEEDED');self.assertIn(b'partial',r.stdout)
        self.assertLess(r.duration,3)

    @unittest.skipIf(os.name=='nt','Unix process-group fixture; Windows job-object coverage remains pending')
    async def test_parent_exit_with_inherited_pipe_is_bounded(self):
        r=await self.run_child("import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print('parent done',flush=True)",timeout=4,pipe_grace=.15)
        self.assertEqual(r.reason,'PROCESS_PIPE_STALLED');self.assertFalse(r.output_complete)
        self.assertLess(r.duration,2)

    async def test_output_limit_is_explicit(self):
        r=await self.run_child("print('x'*10000)",max_output_bytes=500)
        self.assertEqual(r.reason,'OUTPUT_LIMIT_EXCEEDED');self.assertEqual(len(r.stdout)+len(r.stderr),500)
        self.assertFalse(r.output_complete)

    async def test_missing_path_is_preflight_failure(self):
        with self.assertRaisesRegex(RuntimeErrorCode,'DEPENDENCY_MISSING'):
            await supervise(['git','--version'],cwd=Path.cwd(),env={})

    @unittest.skipIf(os.name=='nt','Unix process exit assertion')
    async def test_cancelled_supervisor_reaps_child(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'pid'
            source="import os,time,pathlib; pathlib.Path("+repr(str(target))+").write_text(str(os.getpid())); time.sleep(30)"
            task=asyncio.create_task(self.run_child(source))
            for _ in range(100):
                if target.exists():break
                await asyncio.sleep(.02)
            self.assertTrue(target.exists())
            pid=int(target.read_text());task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)

class ProcessPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def scenario(self,url):
        db=Database(url);await db.migrate();scope=str(uuid4());store=RuntimeStore(db,scope)
        env=dict(os.environ,DIOS_FIXTURE_URL=url,DIOS_FIXTURE_SCOPE=scope)
        async def child(mode,*args):
            return await supervise([sys.executable,'-m','tests.runtime_process_fixture',mode,*args],cwd=Path.cwd(),env=env,timeout=20)
        try:
            for n in range(4):await store.submit('job-'+str(n),{'fixture':n})
            results=await asyncio.gather(*(child('claim') for _ in range(4)))
            self.assertTrue(all(r.exit_code==0 for r in results),[r.stderr for r in results])
            rows=[json.loads(r.stdout) for r in results]
            self.assertEqual(len({r['pid'] for r in rows}),4)
            self.assertEqual(len({r['attempt']['run_id'] for r in rows}),4)
            crash=await child('committed-crash');self.assertEqual(crash.exit_code,77)
            run_id=crash.stdout.decode().strip()
            self.assertEqual((await store.get(run_id))['state'],'queued')
            pending=len(await store.pending_events('check'))
            crash=await child('uncommitted-crash',run_id);self.assertEqual(crash.exit_code,78)
            self.assertEqual((await store.get(run_id))['state'],'queued')
            self.assertEqual(len(await store.pending_events('check')),pending)
        finally:await db.close()

    async def test_sqlite_independent_processes_and_crashes(self):
        with tempfile.TemporaryDirectory() as d:await self.scenario('sqlite+aiosqlite:///'+d+'/state.db')

    @unittest.skipUnless(os.environ.get('DIOS_TEST_POSTGRES_URL'),'isolated PostgreSQL test URL not set')
    async def test_postgres_independent_processes_and_crashes(self):
        await self.scenario(os.environ['DIOS_TEST_POSTGRES_URL'])

if __name__=='__main__':unittest.main()
