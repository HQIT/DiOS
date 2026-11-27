"""Physical Linux Worker storage exhaustion and node restart qualification.

A restart hook is an operator-provided JSON argv file for the dedicated test node.
It must return only after Docker and the verified SSH endpoint are available again.
"""
import argparse
import asyncio
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'backend'), str(ROOT/'backend/tests')]
from qualify_physical_faults import PhysicalFaults
from qualify_physical_worker import PhysicalWorkers
from app.runtime.database import RuntimeErrorCode
from app.runtime.remote_runtime import RemoteRuntimeStore
from app.runtime.remote_tool_channel import RemoteToolProvider
from types import SimpleNamespace


class StorageRestart(PhysicalFaults):
    observations = []

    async def asyncSetUp(self):
        self.storage_volume = None
        self.storage_helper = None
        await PhysicalWorkers.asyncSetUp(self)

    async def docker(self, *args):
        result = await asyncio.to_thread(self.remote, shlex.join(['docker', *args]),
                                        capture_output=True, text=True)
        return result.stdout

    async def manager(self):
        name = 'dios-physical-' + self.store.scope.removeprefix('worker-test-')[:12] + '-' + str(len(self.names))
        self.names.append(name)
        command = ['docker','run','--rm','--name',name,
            '--label','dios.qualification='+self.store.scope,
            '--network','host','--pid','host','--cpus','1','--memory','512m',
            '-v','/var/run/docker.sock:/var/run/docker.sock',
            '-v',f'{self.root}:{self.root}','-v',f'{self.options.remote_source}:/app:ro']
        if self.storage_volume:
            command += ['-v',f'{self.storage_volume}:{self.root}/durable-worker']
        command += ['-w','/app','--entrypoint','python',self.options.runtime_image,
            '-m','app.runtime.cli','--scope',self.store.scope,'remote-worker',
            '--profile',str(self.profiles['account-a'][0]),'--control-file',str(self.client_file),
            '--artifacts',str(self.root/'durable-worker'),'--poll-interval','.25']
        process = await asyncio.create_subprocess_exec(*self.ssh,shlex.join(command),
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        self.children.append(process)
        return process,name

    async def cleanup_remote(self):
        try:
            await super().cleanup_remote()
        finally:
            if self.storage_volume:
                # Only this test's generated volume, after labelled containers are removed.
                await self.docker('volume','rm',self.storage_volume)

    async def test_storage_full_recovers_without_restarting_worker(self):
        self.storage_volume='dios-fault-'+self.store.scope.removeprefix('worker-test-')
        await self.docker('volume','create','--label','dios.qualification='+self.store.scope,
            '--driver','local','--opt','type=tmpfs','--opt','device=tmpfs','--opt','o=size=8m',self.storage_volume)
        self.storage_helper='dios-storage-'+self.store.scope.removeprefix('worker-test-')
        await self.docker('run','-d','--name',self.storage_helper,
            '--label','dios.qualification='+self.store.scope,'--memory','64m','--network','none',
            '-v',self.storage_volume+':/fault','--entrypoint','python',self.options.runtime_image,
            '-c','import time; time.sleep(600)')
        run=await self.submit('storage-full')
        process,name=await self.manager()
        await self.concurrency(1)
        first=(await self.store.get(run['id']))['attempts'][0]
        container=await self.workload()
        # Stop the manager while the synthetic child produces complete output.
        await self.docker('kill','--signal','STOP',name)
        (self.control/'release').touch()
        async with asyncio.timeout(20):
            while (await self.inspect(container))['State']['Running']:
                await asyncio.sleep(.1)
        self.assertEqual((await self.inspect(container))['State']['ExitCode'],0)
        fill='''import os,errno,json
p='/fault/qualification-fill'
try:
 with open(p,'wb',buffering=0) as f:
  for _ in range(2048): f.write(b'x'*65536)
except OSError as e:
 assert e.errno==errno.ENOSPC
 print(json.dumps({'errno':e.errno,'bytes':os.stat(p).st_size}))
else: raise RuntimeError('bounded volume did not fill')
'''
        full=json.loads(await self.docker('exec',self.storage_helper,'python','-c',fill))
        await self.docker('kill','--signal','CONT',name)
        queued=await self.submit('queued-during-full')
        async with asyncio.timeout(25):
            while True:
                current=await self.store.get(run['id'])
                if process.returncode is not None:
                    out,err=await process.communicate()
                    self.fail('Worker exited on storage exhaustion: '+(out+err).decode()[-2500:])
                if current['state']=='reconciliation_required':break
                await asyncio.sleep(.2)
        await asyncio.sleep(2)
        self.assertIsNone(process.returncode)
        self.assertEqual((await self.store.get(queued['id']))['attempts'],[])
        self.assertNotEqual(current['state'],'succeeded')
        restored=time.monotonic()
        await self.docker('exec',self.storage_helper,'python','-c',
            "from pathlib import Path; Path('/fault/qualification-fill').unlink()")
        result=await self.finished(run['id'],process)
        followup=await self.finished(queued['id'],process)
        self.assertEqual(len(result['attempts']),1)
        self.assertEqual(result['attempts'][0]['id'],first['id'])
        self.assertEqual(result['attempts'][0]['session_id'],first['session_id'])
        self.observations.append({'case':'storage_full','run_id':run['id'],
            'real_errno':full['errno'],'bounded_volume_bytes':8*1024*1024,
            'worker_restarted':False,'no_claim_while_full':True,'original_attempt':first['id'],
            'same_attempt_and_session':True,'additional_workload_launches':0,
            'recovery_seconds':round(time.monotonic()-restored,3),
            'final_state':result['state'],'queued_state':followup['state']})

    async def node_identity(self):
        result=await asyncio.to_thread(self.remote,shlex.join(['python3','-c',
            "import pathlib,json,os; print(json.dumps({'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'pid1_start':pathlib.Path('/proc/1/stat').read_text().split()[21],'pid_namespace':os.readlink('/proc/self/ns/pid')}))"]),
            capture_output=True,text=True)
        return json.loads(result.stdout)

    async def test_node_restart_reconciles_complete_and_incomplete_output(self):
        if not self.options.restart_hook:self.skipTest('explicit dedicated-node restart hook required')
        complete=await self.submit('complete-before-reboot')
        _,spec=self.profiles['account-a']
        partial=await self.store.submit('partial-before-reboot',{
            'runtime_profile':'account-a','profile_digest':spec.fingerprint(),'secret_refs':[],
            'payload':{'barrier':True,'delay':120}},
            authorization=self.policy.admission('account-a',spec.fingerprint(),[]))
        managers=[await self.manager(),await self.manager()]
        active=await self.concurrency(2)
        first=(await self.store.get(complete['id']))['attempts'][0]
        partial_first=(await self.store.get(partial['id']))['attempts'][0]
        for _,name in managers:await self.docker('kill','--signal','STOP',name)
        (self.control/'release').touch()
        container='dios-worker-'+first['id']
        async with asyncio.timeout(20):
            while (await self.inspect(container))['State']['Running']:await asyncio.sleep(.1)
        self.assertTrue((await self.inspect('dios-worker-'+partial_first['id']))['State']['Running'])
        before=await self.node_identity()
        hook=json.loads(self.options.restart_hook.read_text())
        self.assertTrue(isinstance(hook,list) and hook and all(isinstance(x,str) for x in hook))
        started=time.monotonic()
        # The hook performs its own last-moment unrelated-workload guard.
        output=await asyncio.to_thread(subprocess.run,hook+[self.store.scope,self.adapter.scope],
            check=True,capture_output=True,text=True,timeout=180)
        restart=json.loads(output.stdout)
        after=await self.node_identity()
        self.assertNotEqual(before,after)
        for process,_ in managers:await asyncio.wait_for(process.communicate(),20)
        if self.tunnel.returncode is None:self.tunnel.terminate()
        await self.tunnel.communicate()
        port=self.gateway.server.sockets[0].getsockname()[1]
        self.tunnel=await asyncio.create_subprocess_exec(*self.ssh[:-1],'-S','none','-o','ExitOnForwardFailure=yes',
            '-N','-R',f'127.0.0.1:{port}:127.0.0.1:{port}',self.ssh[-1],
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        async with asyncio.timeout(20):
            while True:
                probe=await asyncio.to_thread(self.remote,shlex.join(['ss','-H','-ltn','sport','=',':'+str(port)]),capture_output=True,text=True)
                if probe.stdout.strip():break
                if self.tunnel.returncode is not None:self.fail('restart tunnel failed')
                await asyncio.sleep(.1)
        process,_=await self.manager()
        result=await self.finished(complete['id'],process)
        async with asyncio.timeout(100):
            while True:
                rejected=await self.store.get(partial['id'])
                if rejected['state']=='failed':break
                self.assertNotEqual(rejected['state'],'succeeded')
                await asyncio.sleep(.25)
        self.assertEqual(len(result['attempts']),1)
        self.assertEqual(result['attempts'][0]['session_id'],first['session_id'])
        self.assertEqual(len(rejected['attempts']),1)
        self.assertEqual(rejected['attempts'][0]['error']['code'],'RESULT_INCOMPLETE')
        transport=RemoteToolProvider(self.client_file,SimpleNamespace(scope=self.store.scope),
            'account-a',self.profiles['account-a'][1])
        transport.worker_id=first['worker_id']
        old=RemoteRuntimeStore(self.store.scope,transport);old.instance=first['worker_instance']
        with self.assertRaisesRegex(RuntimeErrorCode,'STALE_OWNER'):
            await old.report(complete['id'],first['id'],first['worker_id'],first['worker_instance'],
                first['epoch'],999,'streaming',session_id=first['session_id'],output=True)
        followup=await self.submit('fresh-after-reboot')
        await self.finished(followup['id'],process)
        self.observations.append({'case':'node_restart','before':before,'after':after,
            'restart':restart,'initial_processes':active,'run_id':complete['id'],
            'complete_state':result['state'],'complete_attempts':1,
            'partial_run_id':partial['id'],'partial_state':rejected['state'],
            'partial_error':rejected['attempts'][0]['error'],'partial_attempts':1,
            'old_owner_rejected':True,'fresh_task_succeeded':True,
            'recovery_seconds':round(time.monotonic()-started,3)})


CASES=['test_storage_full_recovers_without_restarting_worker',
       'test_node_restart_reconciles_complete_and_incomplete_output']

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('ssh-config','host','remote-source','runtime-image','output'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--restart-hook',type=Path)
    parser.add_argument('--case-root',type=Path,required=True)
    parser.add_argument('--case',choices=CASES,action='append')
    options=parser.parse_args();options.ssh_config=Path(options.ssh_config).resolve()
    options.case_root=options.case_root.resolve()
    options.case_root.mkdir(parents=True,exist_ok=True,mode=0o700)
    tempfile.tempdir=str(options.case_root)
    subprocess.run(['ssh','-F',str(options.ssh_config),options.host,shlex.join(['mkdir','-p','-m','700',str(options.case_root)])],check=True)
    StorageRestart.options=options;StorageRestart.__unittest_skip__=False
    cases=options.case or CASES
    started=time.monotonic()
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(StorageRestart(n) for n in cases))
    files=['scripts/qualify_storage_restart.py','backend/app/runtime/worker.py',
           'backend/app/runtime/remote_runtime.py','backend/app/runtime/spool.py']
    report={'work_item':'D01-3.02','passed':result.wasSuccessful() and not result.skipped,
        'tests':result.testsRun,'duration_seconds':round(time.monotonic()-started,3),
        'observations':StorageRestart.observations,
        'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in files},
        'failures':[{'test':t.id(),'traceback':trace} for t,trace in result.failures+result.errors],
        'limits':['8 MiB test volume exhaustion; no host disk is filled.',
                  'Linux node restart on second physical host; not a physical power cut.',
                  'Incomplete output is retained as failed and never counted as successful.']}
    Path(options.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('passed','tests','duration_seconds')}))
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())