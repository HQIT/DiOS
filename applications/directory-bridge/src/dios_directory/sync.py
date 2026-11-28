"""Online directory lifecycle sync through DiOS' public trusted CLI."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from .journal import Journal
from .mapping import LIST, canonical, load, plan, profiles, require, MappingError, validate_mapping, decode
from .scim import ScimClient, SyncError, private_file


class RuntimeCLI:
    def __init__(self, config, scope, state_dir, *, owner_fd=None):
        command = config["runtime_command"]
        require(isinstance(command, list) and 0 < len(command) <= 32
                and all(isinstance(x, str) and 0 < len(x) <= 4096 for x in command)
                and Path(command[0]).is_absolute(), "INVALID_RUNTIME_COMMAND")
        require(Path(config["catalog"]).is_absolute(), "ABSOLUTE_CATALOG_PATH_REQUIRED")
        self.command = command + ["--scope", scope]
        self.catalog, self.directory = config["catalog"], state_dir
        self.owner_fd = owner_fd

    async def call(self, *args):
        process = await asyncio.create_subprocess_exec(*self.command, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            pass_fds=() if self.owner_fd is None else (self.owner_fd,))
        try:
            async with asyncio.timeout(30):
                raw = bytearray()
                while chunk := await process.stdout.read(65536):
                    raw.extend(chunk)
                    if len(raw) > 1048576: raise SyncError("RUNTIME_RESPONSE_TOO_LARGE")
                await process.wait()
            value = json.loads(raw)
            require(isinstance(value, dict), "INVALID_RUNTIME_RESPONSE")
            if process.returncode:
                raise SyncError("IDENTITY_VERSION_CONFLICT" if value.get("error") == "VERSION_CONFLICT"
                                else "RUNTIME_UPDATE_FAILED")
            return value
        except TimeoutError:
            raise SyncError("RUNTIME_RESPONSE_UNCERTAIN") from None
        except (json.JSONDecodeError, UnicodeError):
            raise SyncError("INVALID_RUNTIME_RESPONSE") from None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def projection(self, principal):
        return await self.call("identity-policy", "--catalog", self.catalog, "--principal", principal)

    async def apply(self, patch):
        fd, name = tempfile.mkstemp(prefix="patch-", suffix=".json", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(canonical(patch["changes"]))
            return await self.call("identity-update", "--catalog", self.catalog,
                "--principal", patch["principal_id"], "--revision", str(patch["expected_revision"]), "--file", name)
        finally:
            Path(name).unlink(missing_ok=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


async def sync_once(config, mapping, state_dir, *, runtime=None, scim=None, clock=time.time):
    validate_mapping(mapping)
    try:
        journal = Journal(state_dir)
    except SyncError as exc:
        if str(exc) != "SYNC_ALREADY_RUNNING": raise
        return {"state": "busy", "last_error": str(exc), "next_retry_at": clock() + 5}
    owned_client = None
    try:
        namespace = digest({"source": mapping["source"], "endpoint": config["base_url"].rstrip("/"),
            "issuer": mapping["issuer"], "scope": mapping["scope"], "subjects": mapping["subjects"],
            "command": config["runtime_command"], "catalog": config["catalog"]})
        previous = journal.get("namespace")
        require(previous in (None, namespace), "SYNC_NAMESPACE_CHANGED")
        if previous is None: journal.set("namespace", namespace)
        if clock() < journal.get("next_retry_at", 0):
            return {"state": "cooldown", **journal.status()}
        if scim is None:
            owned_client = scim = ScimClient(config)
        runtime = runtime or RuntimeCLI(config, mapping["scope"], state_dir, owner_fd=journal.lock)
        observed_at, cache = clock(), {}
        max_age = config.get("max_age_seconds", 300)
        require(type(max_age) is int and 1 <= max_age <= 3600, "INVALID_MAX_AGE")

        def fresh():
            require(0 <= clock() - observed_at <= max_age, "STALE_DIRECTORY_SNAPSHOT")

        # Known resource GETs avoid treating unstable pagination as deletion.
        for kind, ids in (("Users", mapping["subjects"]), ("Groups", mapping["roles"])):
            require(isinstance(ids, dict) and len(ids) <= 256, "INVALID_MAPPING")
            for resource_id in sorted(ids):
                fresh()
                key = kind + "/" + resource_id
                cache[key] = await scim.read(kind, resource_id, journal.cached(key))
        users = [v["value"] for k, v in cache.items() if k.startswith("Users/") and v["value"] is not None]
        present = {v["id"] for v in users}
        groups = [dict(v["value"], members=[m for m in v["value"]["members"] if m["value"] in present])
                  for k, v in cache.items() if k.startswith("Groups/") and v["value"] is not None]
        snapshot = {"version": 1, "source": mapping["source"], "observed_at": observed_at,
            "users": {"schemas": [LIST], "totalResults": len(users), "Resources": users},
            "groups": {"schemas": [LIST], "totalResults": len(groups), "Resources": groups}}
        current = [await runtime.projection(pid) for pid in sorted(mapping["subjects"].values())]
        document = plan(snapshot, mapping, current, now=clock(), max_age=max_age)
        new = {p["principal_id"]: p for p in document["patches"]}
        states = {p["principal_id"]: p for p in current}
        chosen, superseded = [], []
        for patch_id, old in journal.pending():
            pid = old["principal_id"]
            row = states[pid]
            value = {"enabled": row["enabled"], "profiles": profiles(row["profiles"])}
            desired = new[pid]["changes"] if pid in new else value
            # Reconcile a possibly committed command only when fresh source data
            # still requests that exact authority. Never replay stale grants.
            retryable = row["revision"] == old["expected_revision"] or (
                row["revision"] == old["expected_revision"] + 1 and value == old["changes"])
            if retryable and desired == old["changes"]:
                chosen.append((patch_id, old)); new.pop(pid, None)
            else:
                superseded.append(patch_id)
        chosen.extend((None, patch) for patch in new.values())
        document["resumed_patch_ids"] = [pid for pid, _ in chosen if pid is not None]
        document["superseded_patch_ids"] = superseded
        cycle = journal.prepare(document, cache, chosen, superseded)
        for patch_id, patch in journal.pending():
            fresh()
            receipt = await runtime.apply(patch)
            require(receipt.get("principal_id") == patch["principal_id"]
                    and receipt.get("revision") == patch["expected_revision"] + 1
                    and receipt.get("enabled") == patch["changes"]["enabled"], "INVALID_RUNTIME_RECEIPT")
            journal.receipt(patch_id, receipt)
        journal.complete(cycle, clock())
        return {"state": "synchronized", "applied": len(chosen), **journal.status()}
    except (MappingError, OSError) as exc:
        failures = journal.get("failures", 0) + 1
        delay = max(min(300, 5 * 2 ** min(failures-1, 6)), getattr(exc, "retry_after", 0))
        journal.set("failures", failures)
        journal.set("last_error", str(exc) if isinstance(exc, MappingError) else "SYNC_IO_UNAVAILABLE")
        journal.set("next_retry_at", clock() + delay)
        return {"state": "retry_wait", **journal.status()}
    finally:
        if owned_client is not None: await owned_client.close()
        journal.close()


async def run(args):
    if args.status:
        journal = Journal(args.state_dir)
        try: print(json.dumps(journal.status(), ensure_ascii=False)); return 0
        finally: journal.close()
    require(args.config and args.mapping, "CONFIG_AND_MAPPING_REQUIRED")
    while True:
        config = decode(private_file(args.config))
        mapping = load(args.mapping)
        result = await sync_once(config, mapping, args.state_dir)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if not args.watch: return 0 if result["state"] == "synchronized" else 2
        await asyncio.sleep(max(args.interval, result.get("next_retry_at", 0) - time.time()))


def main():
    parser = argparse.ArgumentParser(description="Sync configured directory identities; no background service is installed")
    parser.add_argument("--config")
    parser.add_argument("--mapping")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    try:
        require(5 <= args.interval <= 3600, "INVALID_INTERVAL")
        return asyncio.run(run(args))
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, MappingError) else "INVALID_SYNC_INPUT"}))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__": raise SystemExit(main())
