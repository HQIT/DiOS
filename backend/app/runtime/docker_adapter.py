"""Local/explicit Docker endpoint lifecycle; no Agent vendor or model protocol code."""
import asyncio
import hashlib
import re
import docker
from requests.exceptions import ConnectionError as TransportConnectionError, Timeout as TransportTimeout
from docker.errors import NotFound, APIError, ImageNotFound
from docker.types import Mount as DockerMount, LogConfig
from .adapter import LaunchSpec, Observation, LogSnapshot
from .database import RuntimeErrorCode
from .linux_cgroup import kill_frozen_container


async def _native_call(operation):
    """Keep ownership until a synchronous daemon operation actually returns."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


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
            from .execution_isolation import from_spec, validate, verify_container
            if from_spec(spec) and self.client.api.base_url != 'http+docker://localhost':
                # Host-path validation and peer credentials require this Worker's
                # local Linux daemon. Remote hosts use their own enrolled Worker.
                raise RuntimeErrorCode('EXECUTION_ISOLATION_UNSUPPORTED')
            validate(spec, host_paths=True)
            mounts = [DockerMount(m.target, m.source, type='bind', read_only=m.read_only) for m in spec.mounts]
            if from_spec(spec):
                for mount in mounts:
                    # Exclude nested host mounts rather than relying on kernel-specific
                    # best-effort recursive read-only behavior.
                    mount.setdefault('BindOptions', {})['NonRecursive'] = True
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
                verify_container(c, spec)
                # Never restart an existing stopped/exited container implicitly.
                return self._observation(handle, c)
            host = str(self.client.info().get('ID', 'unknown'))
            try:
                c = self.client.containers.create(image.id, command=list(spec.command) if spec.command is not None else None,
                    name=self._name(handle), environment=spec.environment,
                    # Host Docker client defaults are outside the authorized launch digest.
                    use_config_proxy=False,
                    working_dir=spec.working_dir, user=spec.user, network=spec.network,
                    mounts=mounts,
                    mem_limit=spec.memory_bytes, nano_cpus=int(spec.cpu_count*1_000_000_000),
                    pids_limit=spec.pids_limit, read_only=spec.read_only_root,
                    cap_drop=['ALL'], security_opt=['no-new-privileges:true'],
                    tmpfs={'/tmp':'rw,noexec,nosuid,size=64m'}, init=True,
                    restart_policy={'Name':'no'},
                    extra_hosts=spec.host_aliases or None,
                    log_config=LogConfig(type='json-file', config={'max-size':'4m','max-file':'1'}),
                    labels={'dios.runtime.scope':self.scope, 'dios.runtime.spec':fingerprint,
                            'dios.runtime.kind':spec.kind, 'dios.runtime.host':host})
            except APIError as exc:
                if exc.status_code != 409: raise
                c = self._get(handle)
                if c is None or c.labels.get('dios.runtime.spec') != fingerprint:
                    raise RuntimeErrorCode('RUNTIME_SPEC_CONFLICT') from None
                verify_container(c, spec)
                return self._observation(handle, c)
            verify_container(c, spec)
            c.start()
            return self._observation(handle, c)
        return await _native_call(launch)

    async def observe(self, handle):
        return await self._read(lambda: self._observation(handle, self._get(handle)))

    async def _read(self, operation):
        try:
            return await _native_call(operation)
        except (TransportConnectionError, TransportTimeout):
            raise RuntimeErrorCode('RUNTIME_OBSERVATION_UNAVAILABLE') from None
        except APIError as exc:
            if exc.status_code == 429 or (exc.status_code is not None and exc.status_code >= 500):
                raise RuntimeErrorCode('RUNTIME_OBSERVATION_UNAVAILABLE') from None
            raise

    async def inspect_existing(self, native_id):
        from .docker_binding import inspect_existing
        return await inspect_existing(self, native_id)

    def bind_existing(self, handle, identity):
        from .docker_binding import BoundDockerAdapter
        return BoundDockerAdapter(self, handle, identity)

    async def endpoint(self, handle, port):
        if type(port) is not int or not 1 <= port <= 65535:
            raise RuntimeErrorCode('INVALID_SERVICE_PORT')
        def address():
            c = self._get(handle)
            if c is None:
                raise RuntimeErrorCode('RUNTIME_NOT_FOUND')
            c.reload()
            network = c.attrs.get('HostConfig', {}).get('NetworkMode')
            details = c.attrs.get('NetworkSettings', {}).get('Networks', {}).get(network, {})
            ip = details.get('IPAddress')
            if not ip:
                raise RuntimeErrorCode('RUNTIME_ENDPOINT_UNAVAILABLE')
            return f'http://{ip}:{port}'
        return await _native_call(address)

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
        return await self._read(read)

    async def stop(self, handle, grace_seconds=5):
        if not 0 <= grace_seconds <= 60: raise RuntimeErrorCode('INVALID_STOP_GRACE')
        def stop():
            c = self._get(handle)
            if c is not None:
                c.reload()
                if c.attrs['State'].get('Paused'):
                    # Docker kill thaws the cgroup, permitting descendants to run.
                    # Native whole-cgroup kill preserves the freeze until exit.
                    kill_frozen_container(c.id, c.attrs['State'].get('Pid'))
                    c.wait(timeout=5)
                else:
                    c.stop(timeout=grace_seconds)
            return self._observation(handle, c)
        return await _native_call(stop)

    async def _freeze(self, handle, process_instance, paused):
        def change():
            c = self._get(handle)
            current = self._observation(handle, c)
            if not process_instance or current.process_instance != process_instance:
                raise RuntimeErrorCode('PROCESS_CHANGED')
            if not current.alive:
                return current
            if paused and current.state != 'paused':
                c.pause()
            elif not paused and current.state == 'paused':
                c.unpause()
            return self._observation(handle, c)
        return await _native_call(change)

    async def pause(self, handle, process_instance):
        return await self._freeze(handle, process_instance, True)

    async def resume(self, handle, process_instance):
        return await self._freeze(handle, process_instance, False)

    async def remove(self, handle):
        def remove():
            c = self._get(handle)
            if c is None: return
            c.reload()
            if c.attrs['State'].get('Running'):
                raise RuntimeErrorCode('RUNTIME_STILL_RUNNING')
            c.remove(force=False)
        await _native_call(remove)
