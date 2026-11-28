"""Run existing recovery assertions with Docker Workers on a second Linux host.

SSH is qualification transport only. Runtime identity and authorization remain mTLS/E2AG.
The SSH alias must use verified host keys and a dedicated test directory.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'backend/tests')]
from test_remote_recovery_qualification import RemoteRecoveryQualificationTests, OBSERVATIONS


class MirroredControlPath:
    """Synchronize only explicit synthetic fault/barrier files to the test node."""
    def __init__(self, path, remote):
        self.path, self.remote = path, remote

    def __truediv__(self, name):
        if name not in {'outage', 'release'}:
            raise ValueError('unexpected control file')
        return type(self)(self.path / name, self.remote)

    def write_text(self, value):
        self.path.write_text(value)
        self.remote('cat > ' + shlex.quote(str(self.path)), input=value.encode())

    def touch(self):
        self.path.touch()
        self.remote(shlex.join(['touch', str(self.path)]))

    def unlink(self):
        self.path.unlink()
        self.remote(shlex.join(['rm', '--', str(self.path)]))


def remote_command(command, options):
    host = getattr(options, 'remote_docker_host', None)
    return shlex.join(['env', 'DOCKER_HOST=' + host, 'sh', '-c', command]) if host else command


class PhysicalWorkers(RemoteRecoveryQualificationTests):
    options = None
    security_observations = []

    def remote(self, command, **kwargs):
        kwargs.setdefault('check', True)
        return subprocess.run(self.ssh + [remote_command(command, self.options)], **kwargs)

    async def asyncSetUp(self):
        self.ssh = ['ssh', '-F', str(self.options.ssh_config), self.options.host]
        self.tunnel = None
        self.names = []
        self.units = []
        await super().asyncSetUp()
        self.addAsyncCleanup(self.cleanup_remote)
        # The destination is freshly created; refuse to reuse arbitrary directories.
        await asyncio.to_thread(self.remote, shlex.join(['mkdir', '-m', '700', str(self.root)]))
        payload = self.root / 'worker-inputs.tar'
        with tarfile.open(payload, 'w') as tar:
            for name in ('account-a.json', 'account-b.json', 'client.json', 'ca.pem', 'client.pem', 'client.key', 'control'):
                tar.add(self.root / name, arcname=name)
        with payload.open('rb') as stream:
            await asyncio.to_thread(self.remote, shlex.join(['tar', 'xf', '-', '-C', str(self.root)]), stdin=stream)
        # Reverse forwarding terminates in the remote Linux namespace, not a host-wide listener.
        local_port = self.gateway.server.sockets[0].getsockname()[1]
        self.tunnel = await asyncio.create_subprocess_exec(
            *self.ssh[:-1], '-S', 'none', '-o', 'ExitOnForwardFailure=yes', '-N', '-R',
            f'127.0.0.1:{local_port}:127.0.0.1:{local_port}', self.ssh[-1],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async with asyncio.timeout(20):
            while True:
                if self.tunnel.returncode is not None:
                    _, err = await self.tunnel.communicate()
                    raise RuntimeError('SSH forward failed: ' + err.decode())
                ready = await asyncio.to_thread(self.remote,
                    shlex.join(['ss', '-H', '-ltn', 'sport', '=', ':'+str(local_port)]),
                    capture_output=True, text=True)
                if ready.stdout.strip():
                    break
                await asyncio.sleep(.1)
        self.control = MirroredControlPath(self.control, self.remote)

    async def launch(self, profile='account-a', *, once=True):
        name = 'dios-physical-' + self.store.scope.removeprefix('worker-test-')[:12] + '-' + str(len(self.names))
        self.names.append(name)
        path, _ = self.profiles[profile]
        command = [
            'docker', 'run', '--rm', '--name', name,
            '--label', 'dios.qualification=' + self.store.scope,
            '--network', 'host', '--pid', 'host', '--cpus', '1', '--memory', '512m',
            '-v', '/var/run/docker.sock:/var/run/docker.sock',
            '-v', f'{self.root}:{self.root}', '-v', f'{self.options.remote_source}:/app:ro',
            '-w', '/app', '--entrypoint', 'python', self.options.runtime_image,
            '-m', 'app.runtime.cli', '--scope', self.store.scope, 'remote-worker',
            '--profile', str(path), '--control-file', str(self.client_file),
            '--artifacts', str(self.root / ('worker-' + str(len(self.children)))),
            '--once', '--poll-interval', '.1',
        ]
        if getattr(self.options, 'remote_python', None):
            # A host-side manager can use a rootless daemon and the host SSH tunnel.
            # The workload remains an isolated Docker container with the same profile.
            unit = name + '.service'
            self.units.append(unit)
            environment = ['PYTHONPATH=' + self.options.remote_source]
            if getattr(self.options, 'remote_docker_host', None):
                environment.append('DOCKER_HOST=' + self.options.remote_docker_host)
            command = ['systemd-run', '--user', '--wait', '--pipe', '--collect', '--quiet',
                '--unit', unit, '-p', 'RuntimeMaxSec=240', '-p', 'MemoryMax=512M',
                '-p', 'TasksMax=64', 'env', *environment,
                self.options.remote_python, '-m', 'app.runtime.cli', '--scope', self.store.scope,
                'remote-worker', '--profile', str(path), '--control-file', str(self.client_file),
                '--artifacts', str(self.root / ('worker-' + str(len(self.children)))),
                '--once', '--poll-interval', '.1']
        if not once:
            command.remove('--once')
        process = await asyncio.create_subprocess_exec(
            *self.ssh, remote_command(shlex.join(command), self.options),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.children.append(process)
        return process

    async def collect(self, process):
        out, err = await asyncio.wait_for(process.communicate(), 90)
        self.assertEqual(process.returncode, 0, err.decode())
        return json.loads(out)

    async def test_revoked_machine_cannot_claim_and_duplicate_submit_keeps_identity(self):
        run = await self.submit('identity')
        self.assertEqual((await self.submit('identity'))['id'], run['id'])
        path = self.root / 'machines.json'
        identities = json.loads(path.read_text())
        identities['machines'][0]['enabled'] = False
        path.write_text(json.dumps(identities))
        process = await self.launch()
        out, err = await asyncio.wait_for(process.communicate(), 90)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('WORKER_IDENTITY_EXPIRED_OR_REVOKED', (out + err).decode())
        self.assertEqual((await self.store.get(run['id']))['attempts'], [])
        identities['machines'][0]['enabled'] = True
        path.write_text(json.dumps(identities))
        (self.control / 'release').touch()
        result = await self.collect(await self.launch())
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['id'], run['id'])
        self.assertEqual(len(result['attempts']), 1)
        self.security_observations.append({
            'run_id': run['id'], 'revoked_machine_denied_before_claim': True,
            'duplicate_submit_same_run': True, 'final_attempts': 1,
            'final_state': result['state'],
        })

    async def cleanup_remote(self):
        # Only resources labelled with this fixture's random scope may be removed.
        command = 'ids=$(docker ps -aq --filter ' + shlex.quote('label=dios.qualification=' + self.store.scope) + '); if [ -n "$ids" ]; then docker rm -f $ids >/dev/null; fi'
        try:
            if self.units:
                stopped = await asyncio.to_thread(self.remote,
                    shlex.join(['systemctl', '--user', 'stop', *self.units]), check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                if stopped.returncode not in (0, 5):
                    raise RuntimeError('qualification manager cleanup failed: '+stopped.stderr.decode())
            await asyncio.to_thread(self.remote, command)
            command = 'ids=$(docker ps -aq --filter ' + shlex.quote('label=dios.runtime.scope=' + self.adapter.scope) + '); if [ -n "$ids" ]; then docker rm -f $ids >/dev/null; fi'
            await asyncio.to_thread(self.remote, command)
        finally:
            if self.tunnel is not None and self.tunnel.returncode is None:
                self.tunnel.terminate()
                await self.tunnel.communicate()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-config', type=Path, required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--remote-source', required=True)
    parser.add_argument('--runtime-image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    PhysicalWorkers.options = args
    os.environ['DIOS_TEST_DOCKER'] = '1'
    # unittest's class decorator evaluated before CLI parsing.
    PhysicalWorkers.__unittest_skip__ = False
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PhysicalWorkers))
    source_paths = ['scripts/qualify_physical_worker.py', 'backend/tests/test_remote_recovery_qualification.py']
    report = {
        'work_item': 'D01-3.02', 'passed': result.wasSuccessful() and len(OBSERVATIONS) == 2 and len(PhysicalWorkers.security_observations) == 1 and not result.skipped,
        'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'duration_seconds': round(time.monotonic() - started, 3),
        'implementation_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'runtime_image': args.runtime_image, 'native': OBSERVATIONS, 'identity': PhysicalWorkers.security_observations,
        'source_sha256': {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in source_paths},
        'failure_cases': [{'test': case.id(), 'traceback': trace} for case, trace in result.errors + result.failures],
        'limits': [
            'Controller and native Docker Workers run on two physical hosts; SSH forwards the test mTLS control connection.',
            'Rate-limit and quota faults are synthetic; no model service is called.',
            'Full host restart, network partition, storage faults, replication RPO and HA are not qualified by these scenarios.',
            'Test manager containers have Docker socket and host PID access; these are trusted managers, not untrusted workloads.',
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('passed','tests','errors','failures','duration_seconds')}))
    return 0 if report['passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())