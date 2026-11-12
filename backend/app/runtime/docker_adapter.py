"""Local/explicit Docker endpoint lifecycle; no Agent vendor or model protocol code."""
import asyncio
import hashlib
import re
import docker
from docker.errors import NotFound, APIError, ImageNotFound
from docker.types import Mount as DockerMount, LogConfig
from .adapter import LaunchSpec, Observation, LogSnapshot
from .database import RuntimeErrorCode


class DockerRuntimeAdapter:
    name = 'local-docker-v1'

    def __init__(self, scope, client=None):
        self.scope = hashlib.sha256(scope.encode()).hexdigest()
        self.client = client or docker.from_env(timeout=5)

    def _name(self, handle):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', handle):
            raise RuntimeErrorCode('INVALID_RUNTIME_HANDLE')
        return 'dios-worker-' + handle

    def _get(self, handle):
        try:
            c = self.client.containers.get(self._name(handle))
        except NotFound:
            return None
        if c.labels.get('dios.runtime.scope') != self.scope:
            raise RuntimeErrorCode('RUNTIME_SCOPE_MISMATCH')
        return c

    def _observation(self, handle, c):
        if c is None:
            return Observation(handle, 'absent', False, None, None, None, 'unknown', 'unknown')
        c.reload()
        state = c.attrs['State']
        pid = state.get('Pid') or None
        return Observation(handle, state['Status'], bool(state.get('Running')), pid,
            c.id + ':' + state.get('StartedAt', ''),
            state.get('ExitCode') if state['Status'] in {'exited', 'dead'} else None,
            state.get('Health', {}).get('Status', 'not_configured'),
            c.labels.get('dios.runtime.host', 'unknown'))

    async def start(self, handle, spec: LaunchSpec):
        spec.validate()
        def launch():
            # Freeze the resolved image identity, not a mutable tag, in the launch digest.
            try:
                image = self.client.images.get(spec.image)
            except ImageNotFound:
                raise RuntimeErrorCode('RUNTIME_IMAGE_MISSING') from None
            fingerprint = hashlib.sha256((spec.fingerprint()+image.id).encode()).hexdigest()
            c = self._get(handle)
            if c is not None:
                if c.labels.get('dios.runtime.spec') != fingerprint:
                    raise RuntimeErrorCode('RUNTIME_SPEC_CONFLICT')
                # Never restart an existing stopped/exited container implicitly.
                return self._observation(handle, c)
            host = str(self.client.info().get('ID', 'unknown'))
            try:
                c = self.client.containers.create(image.id, command=list(spec.command),
                    name=self._name(handle), environment=spec.environment,
                    working_dir=spec.working_dir, user=spec.user, network=spec.network,
                    mounts=[DockerMount(m.target, m.source, type='bind', read_only=m.read_only) for m in spec.mounts],
                    mem_limit=spec.memory_bytes, nano_cpus=int(spec.cpu_count*1_000_000_000),
                    pids_limit=spec.pids_limit, read_only=spec.read_only_root,
                    cap_drop=['ALL'], security_opt=['no-new-privileges:true'],
                    tmpfs={'/tmp':'rw,noexec,nosuid,size=64m'}, init=True,
                    restart_policy={'Name':'no'},
                    log_config=LogConfig(type='json-file', config={'max-size':'4m','max-file':'1'}),
                    labels={'dios.runtime.scope':self.scope, 'dios.runtime.spec':fingerprint,
                            'dios.runtime.kind':spec.kind, 'dios.runtime.host':host})
            except APIError as exc:
                if exc.status_code != 409: raise
                c = self._get(handle)
                if c is None or c.labels.get('dios.runtime.spec') != fingerprint:
                    raise RuntimeErrorCode('RUNTIME_SPEC_CONFLICT') from None
                return self._observation(handle, c)
            c.start()
            return self._observation(handle, c)
        return await asyncio.to_thread(launch)

    async def observe(self, handle):
        return await asyncio.to_thread(lambda: self._observation(handle, self._get(handle)))

    async def logs(self, handle, max_bytes):
        if not 1 <= max_bytes <= 128*1024*1024:
            raise RuntimeErrorCode('INVALID_OUTPUT_LIMIT')
        def read():
            c = self._get(handle)
            if c is None: raise RuntimeErrorCode('RUNTIME_NOT_FOUND')
            result = []
            remaining = max_bytes
            truncated = False
            for stdout in (True, False):
                data = bytearray()
                stream = c.logs(stdout=stdout, stderr=not stdout, stream=True, follow=False)
                try:
                    for chunk in stream:
                        available = remaining
                        data.extend(chunk[:remaining])
                        remaining -= min(remaining, len(chunk))
                        if len(chunk) >= available:
                            # A conservative bound: at the cap, success is prohibited.
                            truncated = True
                            break
                finally:
                    stream.close()
                result.append(bytes(data))
            return LogSnapshot(*result, truncated)
        return await asyncio.to_thread(read)

    async def stop(self, handle, grace_seconds=5):
        if not 0 <= grace_seconds <= 60: raise RuntimeErrorCode('INVALID_STOP_GRACE')
        def stop():
            c = self._get(handle)
            if c is not None:
                c.stop(timeout=grace_seconds)
            return self._observation(handle, c)
        return await asyncio.to_thread(stop)

    async def remove(self, handle):
        def remove():
            c = self._get(handle)
            if c is None: return
            c.reload()
            if c.attrs['State'].get('Running'):
                raise RuntimeErrorCode('RUNTIME_STILL_RUNNING')
            c.remove(force=False)
        await asyncio.to_thread(remove)
