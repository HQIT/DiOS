"""K02-3.03: failed-image cutover, clean restore, governed resumption and measured RTO."""
import asyncio
import json
import os
import socket
import time
from uuid import uuid4
import unittest

import httpx
import test_installation_activation_qualification as recovery

OBSERVATIONS=[]
IMAGES=('DIOS_TEST_RECOVERY_OLD','DIOS_TEST_RECOVERY_NEW','DIOS_TEST_RECOVERY_BAD')


@unittest.skipUnless(all(os.environ.get(k) for k in IMAGES),'fixed old/new/bad images required')
class RecoveryUpgradeQualificationTests(recovery.InstallationActivationQualificationTests):
    async def command(self,*args):
        process=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await asyncio.wait_for(process.communicate(),45)
        self.assertEqual(process.returncode,0,err.decode()[-1500:])
        return out.decode().strip()

    async def stop_api(self):
        if getattr(self,'container',None):
            await self.command('docker','rm','-f',self.container)
            self.container=None

    async def start_api(self,image,*,restored=False,healthy=True):
        await self.stop_api()
        self.container='dios-recovery-upgrade-'+uuid4().hex[:12]
        f=self.fixture
        data=self.target if restored else f.root
        runtime=data/'runtime/state.sqlite' if restored else data/'runtime.sqlite'
        artifacts=data/'runtime/objects' if restored else f.fixture.provider.root
        env={'DIOS_DATABASE_URL':'sqlite+aiosqlite:///'+str(data/'service.sqlite'),
            'DIOS_RUNTIME_DATABASE_URL':'sqlite+aiosqlite:///'+str(runtime),
            'DIOS_RUNTIME_ARTIFACTS_ROOT':str(artifacts),'DIOS_RUNTIME_SCOPE':f.store.scope,
            'DIOS_RUNTIME_PROFILES_DIR':str(f.root/'api-profiles'),'DIOS_RUNTIME_POLICY_FILE':str(f.policy.path),
            'DIOS_RUNTIME_CONSOLE_ENABLED':'true','DIOS_RUNTIME_SCHEDULES_ENABLED':'false',
            'DIOS_MANAGED_RECOVERY_ENABLED':'false','DIOS_CONNECTOR_POLL_ENABLED':'false',
            'DIOS_ACCESS_TOKEN':'isolated-qualification-operator','DIOS_E2AG_MODE':'enforce',
            'DIOS_WORKSPACE_ROOT':str(data/'workspaces' if restored else data/'workspace'),
            'PYTHONDONTWRITEBYTECODE':'1'}
        args=['docker','run','-d','--name',self.container,'--user',f'{os.getuid()}:{os.getgid()}',
            '-p',f'127.0.0.1:{self.port}:8000','-v',f'{f.root}:{f.root}']
        for k,v in env.items():args+=['-e',k+'='+v]
        await self.command(*args,image)
        self.assertEqual(await self.command('docker','inspect','--format','{{.Image}}',self.container),image)
        async with asyncio.timeout(35):
            while True:
                state=json.loads(await self.command('docker','inspect','--format','{{json .State}}',self.container))
                if not state['Running']:
                    self.assertFalse(healthy,await self.command('docker','logs',self.container))
                    self.assertNotEqual(state['ExitCode'],0)
                    return state
                try:
                    if (await self.http.get('/health')).status_code==200:
                        self.assertTrue(healthy)
                        return state
                except httpx.HTTPError:pass
                await asyncio.sleep(.15)

    async def api(self,path):
        response=await self.http.get('/api/os/'+path)
        self.assertEqual(response.status_code,200,response.text[:400])
        return response.json()

    async def stage(self,name,**context):
        if name=='source-ready':
            self.fixture=context['fixture'];self.marks={}
            async def submit_console(key, profile='account-a'):
                f=self.fixture
                _,spec=f.profiles[profile]
                return await f.store.submit(key,{'origin':'task-console/v1','runtime_profile':profile,
                    'profile_digest':spec.fingerprint(),'secret_refs':[],
                    'payload':{'barrier':True,'delay':.1,'sample':{'id':'recovery-static','text':'只读恢复验证'}}},
                    authorization=f.policy.admission(profile,spec.fingerprint(),[]))
            self.fixture.submit=submit_console
            profiles=self.fixture.root/'api-profiles';profiles.mkdir()
            for name,(path,_) in self.fixture.profiles.items():
                (profiles/(name+'.json')).write_bytes(path.read_bytes())
            self.images={key:os.environ['DIOS_TEST_RECOVERY_'+key.upper()] for key in ('old','new','bad')}
            self.assertEqual(len(set(self.images.values())),3)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0));self.port=listener.getsockname()[1]
            self.http=httpx.AsyncClient(base_url=f'http://127.0.0.1:{self.port}',trust_env=False,timeout=3,
                headers={'X-DiOS-Access-Token':'isolated-qualification-operator'})
            self.addAsyncCleanup(self.http.aclose);self.addAsyncCleanup(self.stop_api)
            await self.start_api(self.images['old'])
        elif name=='backup-captured':
            self.target=context['target'];self.run=context['run'];self.queued=context['queued']
            self.assertEqual((await self.api('runtime/runs/'+self.run['id']))['id'],self.run['id'])
            self.assertEqual((await self.api('runtime/runs/'+self.queued['id']))['state'],'queued')
        elif name=='before-source-stop':
            self.failure_started=time.monotonic()
            failed=await self.start_api(self.images['bad'],healthy=False)
            self.marks['failure_detected_seconds']=time.monotonic()-self.failure_started
            self.failed_exit=failed['ExitCode']
            await self.stop_api()
        elif name=='restore-published':
            self.marks['restore_published_seconds']=time.monotonic()-self.failure_started
            await self.start_api(self.images['new'],restored=True)
            self.assertEqual((await self.api('upgrade/status'))['mode'],'restore_hold')
            current=await self.api('runtime/runs/'+self.run['id'])
            self.assertNotEqual(current['state'],'succeeded')
            self.marks['held_api_ready_seconds']=time.monotonic()-self.failure_started
        elif name=='reconciliation-complete':
            current=await self.api('runtime/runs/'+self.run['id'])
            self.assertEqual(current['state'],'succeeded')
            self.assertEqual((await self.api('upgrade/status'))['mode'],'restore_hold')
            self.marks['result_reconciled_seconds']=time.monotonic()-self.failure_started
            await self.stop_api()
        elif name=='activation-committed':
            self.marks['activation_seconds']=time.monotonic()-self.failure_started
            # Restore service with the known-good prior version before retrying the candidate.
            await self.start_api(self.images['old'],restored=True)
            self.assertEqual((await self.api('upgrade/status'))['mode'],'running')
            self.assertEqual((await self.api('runtime/runs/'+self.run['id']))['state'],'succeeded')
            self.marks['restored_service_ready_seconds']=time.monotonic()-self.failure_started
        elif name=='resumed-complete':
            before={r['id']:await self.api('runtime/runs/'+r['id']) for r in (self.run,self.queued)}
            self.assertTrue(all(r['state']=='succeeded' for r in before.values()))
            self.marks['governed_work_completed_seconds']=time.monotonic()-self.failure_started
            await self.start_api(self.images['new'],restored=True)
            for identity,value in before.items():
                after=await self.api('runtime/runs/'+identity)
                for field in ('id','state','attempts','artifacts'):
                    self.assertEqual(after[field],value[field],field)
            self.marks['candidate_ready_after_restore_seconds']=time.monotonic()-self.failure_started
            OBSERVATIONS.append({'images':self.images,'failure_exit_code':self.failed_exit,
                'timings':{k:round(v,3) for k,v in self.marks.items()},
                'rto_start':'known-good API stop immediately before failed candidate start',
                'rto_end':'restored original queued task completed under current E2AG and visible via the rolled-back API',
                'run_id':self.run['id'],'queued_run_id':self.queued['id'],
                'restored_service_image':self.images['old'],'final_service_image':self.images['new'],
                'unknown_result_reconciled_before_activation':True,'both_holds_observed_via_deployed_api':True,
                'rollback_after_clean_restore_usable':True,'candidate_upgrade_preserved_history_and_artifacts':True})