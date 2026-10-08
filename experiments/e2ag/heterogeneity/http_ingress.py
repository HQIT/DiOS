"""Experimental signed HTTP delivery adapter; not DiOS's existing Git webhook.

Same envelope/journal contract as file and AMQP; identity is bound to one
administrator-configured endpoint and secret. HTTP success means handoff only.
"""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse
import uvicorn

from file_ingress import IngressError, MAX_BYTES, normalize_envelope


def signature(raw, secret):
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def decode(raw, supplied, secret):
    if not secret or not hmac.compare_digest(supplied, signature(raw, secret)):
        raise IngressError("INVALID_HTTP_SIGNATURE")
    return normalize_envelope(raw, "fixture", "http")


async def receive_one(callback, folder, secret):
    # Each process serves one request then stops, making recovery ownership
    # explicit. This is an experimental lifecycle, not a production server.
    from run_file_recovery import append_record, save_new
    app = FastAPI()
    captured = {}
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(app, log_level="critical", access_log=False, timeout_graceful_shutdown=5)
    server = uvicorn.Server(config)

    async def finish():
        server.should_exit = True

    @app.post("/{route:path}")
    async def ingest(route: str, request: Request):
        raw = bytearray()
        event = None
        try:
            if route != "webhook/fixture":
                raise IngressError("UNCONFIGURED_HTTP_ENDPOINT")
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise IngressError("MESSAGE_TOO_LARGE")
            event = decode(bytes(raw), request.headers.get("x-e2ag-signature", ""), secret)
            # Persist the actual normalized input before native dispatch.
            append_record(folder / "http-inputs.jsonl", {"request_sha256": hashlib.sha256(raw).hexdigest(),
                "signature_verified": True, "normalized": event})
            result = await callback(event)
            status = 409 if result["disposition"] == "conflict" else 202 if result["acknowledge"] else 503
        except IngressError as exc:
            result = {"rejected": True, "reason": str(exc)}
            status = 403 if str(exc) in {"INVALID_HTTP_SIGNATURE", "UNCONFIGURED_HTTP_ENDPOINT"} else 400
        captured.update(result)
        append_record(folder / "http-responses.jsonl", {"status": status, "result": result})
        return JSONResponse(result, status_code=status, background=BackgroundTask(finish))

    job = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        deadline = asyncio.get_running_loop().time() + 10
        while not server.started:
            if job.done():
                await job
                raise RuntimeError("HTTP server ended before readiness")
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("HTTP startup timed out")
            await asyncio.sleep(.02)
        marker = folder / ("http-endpoint-" + os.environ["E2AG_HTTP_RUN_ID"] + ".json")
        staging = marker.with_suffix(".tmp")
        save_new(staging, {"port": sock.getsockname()[1], "pid": os.getpid()})
        staging.replace(marker)
        await asyncio.wait_for(job, 35)
        return captured
    finally:
        server.should_exit = True
        if not job.done():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        sock.close()


def invoke_http(command, *, cwd, environment, folder, raw, secret, path="/webhook/fixture", wrong_signature=False):
    """Send an actual HTTP request to a newly launched, single-owner worker."""
    from run_file_recovery import append_record
    environment = {**environment, "E2AG_HTTP_RUN_ID": uuid.uuid4().hex}
    process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        marker = folder / ("http-endpoint-" + environment["E2AG_HTTP_RUN_ID"] + ".json")
        deadline = time.monotonic() + 20
        while not marker.exists():
            if process.poll() is not None:
                out, err = process.communicate()
                return subprocess.CompletedProcess(command, process.returncode, out, err)
            if time.monotonic() > deadline:
                raise TimeoutError("HTTP worker readiness timed out")
            time.sleep(.03)
        port = json.loads(marker.read_text(encoding="utf-8"))["port"]
        headers = {"Content-Type": "application/json", "x-e2ag-signature": signature(raw, "wrong-secret" if wrong_signature else secret)}
        record = {"body_utf8": raw.decode("utf-8"), "request_sha256": hashlib.sha256(raw).hexdigest(), "path": path}
        try:
            response = httpx.post(f"http://127.0.0.1:{port}" + path, content=raw, headers=headers, timeout=30, trust_env=False)
            record.update(status=response.status_code, response=response.json())
        except httpx.TransportError as exc:
            record["transport_error"] = type(exc).__name__
        append_record(folder / "http-client.jsonl", record)
        out, err = process.communicate(timeout=15)
        return subprocess.CompletedProcess(command, process.returncode, out, err)
    finally:
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10)
            out, err = process.communicate(timeout=10)
            append_record(folder / "http-aborted-workers.jsonl", {"stdout": out, "stderr": err, "exit_code": process.returncode})
