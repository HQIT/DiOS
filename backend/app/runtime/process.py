"""Bounded supervision for a trusted Adapter command, independent of model RPC completion."""
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from uuid import uuid4
from .database import RuntimeErrorCode

@dataclass(frozen=True)
class ProcessResult:
    pid: int
    instance: str
    exit_code: int
    stdout: bytes
    stderr: bytes
    reason: str | None
    duration: float
    output_complete: bool

async def supervise(argv, *, cwd, env, timeout=60, max_output_bytes=8*1024*1024, pipe_grace=0.5):
    """Only for registered Adapter commands. This function is not a read-only sandbox."""
    if not argv or not env.get('PATH') or not Path(cwd).is_dir():
        raise RuntimeErrorCode('DEPENDENCY_MISSING')
    executable = shutil.which(argv[0],path=env['PATH'])
    if not executable: raise RuntimeErrorCode('DEPENDENCY_MISSING')
    if timeout <= 0 or max_output_bytes < 1 or pipe_grace <= 0:
        raise RuntimeErrorCode('INVALID_PROCESS_LIMIT')
    options = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(executable,*argv[1:],cwd=cwd,env=env,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,**options)
    instance = str(uuid4())
    output, errors = bytearray(),bytearray()
    overflow = False
    async def capture(stream,destination):
        nonlocal overflow
        while chunk := await stream.read(65536):
            remaining=max_output_bytes-len(output)-len(errors)
            destination.extend(chunk[:max(0,remaining)])
            if len(chunk)>remaining: overflow=True
    readers=[asyncio.create_task(capture(proc.stdout,output)),asyncio.create_task(capture(proc.stderr,errors))]
    def terminate():
        try:
            if os.name=='nt':
                if proc.returncode is None:proc.kill()
            else:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
    reason=None
    complete=True
    try:
        # returncode is updated on child exit even when a descendant keeps RPC pipes open.
        while proc.returncode is None:
            if time.monotonic()-started>=timeout:
                reason='PROCESS_DEADLINE_EXCEEDED';terminate();break
            if overflow:
                reason='OUTPUT_LIMIT_EXCEEDED';terminate();break
            await asyncio.sleep(0.02)
        try:
            await asyncio.wait_for(asyncio.gather(*readers),pipe_grace)
        except asyncio.TimeoutError:
            reason=reason or 'PROCESS_PIPE_STALLED';complete=False;terminate()
        await asyncio.wait_for(proc.wait(),5)
        if overflow:reason=reason or 'OUTPUT_LIMIT_EXCEEDED';complete=False
        if proc.returncode and reason is None:reason='PROCESS_EXITED'
        return ProcessResult(proc.pid,instance,proc.returncode,bytes(output),bytes(errors),reason,time.monotonic()-started,complete)
    except asyncio.CancelledError:
        terminate()
        raise
    finally:
        if proc.returncode is None:terminate()
        for task in readers:
            if not task.done():task.cancel()
        await asyncio.gather(*readers,return_exceptions=True)
        try:
            await asyncio.wait_for(proc.wait(),5)
        finally:
            # asyncio Process exposes no close() API. The transport owns pipes that
            # descendants can retain after the direct child has already exited.
            # Close explicitly while the loop is alive, including cancellation paths.
            proc._transport.close()
