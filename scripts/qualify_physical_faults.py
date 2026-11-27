"""Qualify physical-host transport faults and Worker death with native mTLS/E2AG.

Use a verified Toxiproxy binary on the controller. Faults affect only this fixture.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'backend'), str(ROOT / 'backend/tests')]
from qualify_physical_worker import PhysicalWorkers
from app.runtime.remote_tool_channel import RemoteToolProvider
from app.runtime.remote_runtime import RemoteRuntimeStore


def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class PhysicalFaults(PhysicalWorkers):
    observations = []

    async def asyncSetUp(self):
        self.proxy = None
        self.proxy_log = None
        await super().asyncSetUp()
        self.addAsyncCleanup(self.cleanup_proxy)
        self.api_port, self.proxy_port = available_port(), available_port()
        self.proxy_log = (self.root / 'toxiproxy.log').open('wb')
        self.proxy = await asyncio.create_subprocess_exec(
            str(self.options.toxiproxy), '-host', '127.0.0.1', '-port', str(self.api_port),
            stdout=self.proxy_log, stderr=self.proxy_log)
        async with asyncio.timeout(10):
            while True:
                if self.proxy.returncode is not None:
                    raise RuntimeError('Toxiproxy exited during startup')
                try:
                    await self.api('GET', '/version')
                    break
                except OSError:
                    await asyncio.sleep(.05)
        gateway_port = self.gateway.server.sockets[0].getsockname()[1]
        await self.api('POST', '/proxies', {
            'name': 'control', 'listen': f'127.0.0.1:{self.proxy_port}',
            'upstream': f'127.0.0.1:{gateway_port}', 'enabled': True,
        })
        self.tunnel.terminate()
        await self.tunnel.communicate()
        self.tunnel = await asyncio.create_subprocess_exec(
            *self.ssh[:-1], '-S', 'none', '-o', 'ExitOnForwardFailure=yes', '-N', '-R',
            f'127.0.0.1:{gateway_port}:127.0.0.1:{self.proxy_port}', self.ssh[-1],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async with asyncio.timeout(20):
            while True:
                if self.tunnel.returncode is not None:
                    _, err = await self.tunnel.communicate()
                    raise RuntimeError('Fault tunnel failed: ' + err.decode())
                response = await asyncio.to_thread(
                    self.remote, shlex.join(['ss', '-H', '-ltn', 'sport', '=', ':'+str(gateway_port)]),
                    capture_output=True, text=True)
                if response.stdout.strip():
                    break
                await asyncio.sleep(.1)

    async def api(self, method, path, payload=None):
        def request():
            data = None if payload is None else json.dumps(payload).encode()
            req = urllib.request.Request(f'http://127.0.0.1:{self.api_port}'+path,
                data=data, method=method, headers={'Content-Type':'application/json'})
            # Local qualification API must not inherit an HTTP proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=5) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        return await asyncio.to_thread(request)

    async def cleanup_proxy(self):
        if self.proxy is not None and self.proxy.returncode is None:
            self.proxy.terminate()
            await self.proxy.wait()
        if self.proxy_log is not None:
            self.proxy_log.close()

    async def manager(self):
        name = 'dios-physical-' + self.store.scope.removeprefix('worker-test-')[:12] + '-' + str(len(self.names))
        self.names.append(name)
        command = [
            'docker', 'run', '--rm', '--name', name,
            '--label', 'dios.qualification='+self.store.scope,
            '--network', 'host', '--pid', 'host', '--cpus', '1', '--memory', '512m',
            '-v', '/var/run/docker.sock:/var/run/docker.sock',
            '-v', f'{self.root}:{self.root}', '-v', f'{self.options.remote_source}:/app:ro',
            '-w', '/app', '--entrypoint', 'python', self.options.runtime_image,
            '-m', 'app.runtime.cli', '--scope', self.store.scope, 'remote-worker',
            '--profile', str(self.profiles['account-a'][0]), '--control-file', str(self.client_file),
            '--artifacts', str(self.root/'durable-worker'), '--poll-interval', '.25',
        ]
        process = await asyncio.create_subprocess_exec(*self.ssh, shlex.join(command),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.children.append(process)
        return process, name

    async def finished(self, run_id, process, timeout=120):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            value = await self.store.get(run_id)
            if value['state'] == 'succeeded':
                return value
            if value['state'] in {'failed','cancelled'}:
                self.fail('unexpected terminal result: '+json.dumps(value.get('error')))
            if process.returncode is not None:
                out, err = await process.communicate()
                self.fail('background Worker exited: '+(out+err).decode()[-3000:])
            await asyncio.sleep(.25)
        value = await self.store.get(run_id)
        self.fail('recovery deadline: '+json.dumps(value, default=str)[-4000:])

    async def workload(self):
        result = await asyncio.to_thread(self.remote,
            shlex.join(['docker','ps','-aq','--filter','label=dios.runtime.scope='+self.adapter.scope]),
            capture_output=True, text=True)
        ids = result.stdout.split()
        self.assertEqual(len(ids), 1)
        return ids[0]

    async def inspect(self, container):
        result = await asyncio.to_thread(self.remote, shlex.join(['docker','inspect',container]),
            capture_output=True, text=True)
        return json.loads(result.stdout)[0]

    async def test_partition_recovers_without_manual_worker_restart(self):
        run = await self.submit('partition')
        process, name = await self.manager()
        running = await self.concurrency(1)
        first = (await self.store.get(run['id']))['attempts'][0]
        container = await self.workload()
        await self.api('POST','/proxies/control',{'enabled':False})
        async with asyncio.timeout(30):
            while (await self.inspect(container))['State']['Running']:
                await asyncio.sleep(.25)
        self.assertIsNone(process.returncode)
        self.assertEqual(len((await self.store.get(run['id']))['attempts']), 1)
        (self.control/'release').touch()
        restored_at = time.monotonic()
        await self.api('POST','/proxies/control',{'enabled':True})
        result = await self.finished(run['id'],process)
        self.assertEqual(len(result['attempts']),2)
        self.assertEqual(result['input']['work_authorization_ref'],run['input']['work_authorization_ref'])
        self.assertEqual(result['attempts'][0]['id'], first['id'])
        self.assertGreater(result['attempts'][1]['epoch'],first['epoch'])
        self.observations.append({
            'case':'partition','run_id':run['id'],'worker_process_restarted':False,
            'original_attempt':first['id'],'final_attempt':result['attempts'][1]['id'],
            'original_work_retained':True,'attempt_count':2,'initial_processes':running,
            'first_error':result['attempts'][0]['error'],
            'recovery_seconds':round(time.monotonic()-restored_at,3),'state':result['state'],
        })

    async def test_sigkill_preserves_complete_output_without_relaunch(self):
        run = await self.submit('crash')
        process, name = await self.manager()
        running = await self.concurrency(1)
        first = (await self.store.get(run['id']))['attempts'][0]
        container = await self.workload()
        await asyncio.to_thread(self.remote, shlex.join(['docker','kill','--signal','KILL',name]),
            stdout=subprocess.DEVNULL)
        await asyncio.wait_for(process.communicate(),20)
        self.assertNotEqual(process.returncode,0)
        (self.control/'release').touch()
        async with asyncio.timeout(20):
            while (await self.inspect(container))['State']['Running']:
                await asyncio.sleep(.1)
        self.assertEqual((await self.inspect(container))['State']['ExitCode'],0)
        restarted_at = time.monotonic()
        replacement, _ = await self.manager()
        result = await self.finished(run['id'],replacement)
        self.assertEqual(len(result['attempts']),1)
        self.assertEqual(result['attempts'][0]['id'],first['id'])
        self.assertEqual(result['attempts'][0]['session_id'],first['session_id'])
        self.assertEqual(result['attempts'][0]['process_instance'],first['process_instance'])
        self.observations.append({
            'case':'worker_sigkill','run_id':run['id'],'original_attempt':first['id'],
            'session_id':first['session_id'],'same_attempt_and_session':True,
            'additional_workload_launches':0,'initial_processes':running,
            'recovery_seconds':round(time.monotonic()-restarted_at,3),'state':result['state'],
        })

    async def test_latency_keeps_original_execution(self):
        run = await self.submit('latency')
        process, _ = await self.manager()
        running = await self.concurrency(1)
        first = (await self.store.get(run['id']))['attempts'][0]
        await self.api('POST','/proxies/control/toxics',{
            'name':'slow-read','type':'latency','stream':'downstream',
            'toxicity':1.0,'attributes':{'latency':200,'jitter':0}})
        transport = RemoteToolProvider(self.client_file,SimpleNamespace(scope=self.store.scope),
            'account-a',self.profiles['account-a'][1])
        transport.config['port']=self.proxy_port
        transport.worker_id=first['worker_id']
        client=RemoteRuntimeStore(self.store.scope,transport)
        client.instance=first['worker_instance']
        client.bindings[run['id']]=[run['id'],first['id'],first['worker_id'],first['worker_instance'],first['epoch']]
        started=time.monotonic()
        observed=await client.get(run['id'])
        elapsed=time.monotonic()-started
        self.assertGreaterEqual(elapsed,.2)
        self.assertEqual(observed['attempts'][0]['id'],first['id'])
        (self.control/'release').touch()
        result=await self.finished(run['id'],process)
        self.assertEqual(len(result['attempts']),1)
        self.observations.append({'case':'latency','run_id':run['id'],'latency_ms':200,
            'observed_roundtrip_seconds':round(elapsed,3),'attempt_count':1,
            'initial_processes':running,'state':result['state']})


CASES = ['test_partition_recovers_without_manual_worker_restart',
         'test_sigkill_preserves_complete_output_without_relaunch',
         'test_latency_keeps_original_execution']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ('ssh-config','host','remote-source','runtime-image','toxiproxy','output'):
        parser.add_argument('--'+arg,required=True)
    parser.add_argument('--case',choices=CASES,action='append')
    args=parser.parse_args()
    args.ssh_config=Path(args.ssh_config).resolve()
    args.toxiproxy=Path(args.toxiproxy).resolve()
    PhysicalFaults.options=args
    PhysicalFaults.__unittest_skip__=False
    cases=args.case or CASES
    started=time.monotonic()
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(PhysicalFaults(name) for name in cases))
    files=['scripts/qualify_physical_faults.py','scripts/qualify_physical_worker.py',
        'backend/app/runtime/remote_runtime.py','backend/app/runtime/remote_tool_channel.py',
        'backend/app/runtime/worker.py','backend/app/runtime/reconciliation.py']
    report={
        'work_item':'D01-3.02','passed':result.wasSuccessful() and not result.skipped and len(PhysicalFaults.observations)==len(cases),
        'tests':result.testsRun,'cases':cases,'duration_seconds':round(time.monotonic()-started,3),
        'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'observations':PhysicalFaults.observations,
        'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in files},
        'toxiproxy_sha256':hashlib.sha256(args.toxiproxy.read_bytes()).hexdigest(),
        'failures':[{'test':test.id(),'traceback':trace} for test,trace in result.failures+result.errors],
        'limits':['Two physical Linux/WSL hosts with isolated synthetic tasks.',
            'Worker SIGKILL and restart do not qualify full physical-host reboot.',
            'Storage faults, HA and replication RPO/RTO remain separate qualifications.'],
    }
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('passed','tests','duration_seconds')}))
    return 0 if report['passed'] else 1

if __name__=='__main__':
    raise SystemExit(main())