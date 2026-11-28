"""Local trusted-operator CLI; employees use separate scoped API credentials."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile

from dios_sdk import TaskClient, WorkloadError
from .registry import Registry, RegistryError, check, encoded


def load(path, *, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if private: check(info.st_uid == os.getuid() and not info.st_mode & 0o077, "PRIVATE_FILE_REQUIRED")
        raw = stream.read(262145)
    check(len(raw) <= 262144, "INPUT_TOO_LARGE")
    return raw


class Client:
    def __init__(self, config):
        token = load(config["token_file"], private=True).decode().strip()
        self.sdk = TaskClient(config["base_url"], token, required_contract="1")
    async def identity(self): return await asyncio.to_thread(self.sdk.identity)
    async def profiles(self): return await asyncio.to_thread(self.sdk.profiles)
    async def runtime_contract(self): return await asyncio.to_thread(self.sdk.runtime_contract)
    async def submit(self, profile_id, sample_id, goal, **kwargs):
        return await asyncio.to_thread(self.sdk.submit, profile_id, sample_id, goal, **kwargs)
    async def get(self, run_id): return await asyncio.to_thread(self.sdk.get, run_id)
    async def artifact(self, run_id, artifact_id): return await asyncio.to_thread(self.sdk.artifact, run_id, artifact_id)


class Admin:
    def __init__(self, config, scope, registry):
        command = config["command"]
        check(isinstance(command, list) and command and all(isinstance(a,str) for a in command)
              and Path(command[0]).is_absolute(), "INVALID_ADMIN_COMMAND")
        self.command, self.catalog, self.registry = command + ["--scope", scope], config["catalog"], registry

    async def call(self, *args):
        process = await asyncio.create_subprocess_exec(*self.command, *args, pass_fds=(self.registry.lock,),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            async with asyncio.timeout(30):
                raw = bytearray()
                while chunk := await process.stdout.read(65536):
                    raw.extend(chunk)
                    check(len(raw) <= 1048576, "ADMIN_RESPONSE_TOO_LARGE")
                await process.wait()
            value = json.loads(raw)
            check(process.returncode == 0, "ADMIN_UPDATE_UNCONFIRMED")
            return value
        finally:
            if process.returncode is None: process.kill(); await process.wait()

    async def projection(self, pid):
        return await self.call("identity-policy", "--catalog", self.catalog, "--principal", pid)

    async def apply(self, patch):
        fd, path = tempfile.mkstemp(dir=self.registry.root, prefix="suspend-", suffix=".json")
        try:
            with os.fdopen(fd,"w") as stream: stream.write(encoded(patch["changes"]))
            return await self.call("identity-update", "--catalog", self.catalog, "--principal", patch["principal_id"],
                                   "--revision", str(patch["expected_revision"]), "--file", path)
        finally: Path(path).unlink(missing_ok=True)


async def run(args):
    registry = Registry(args.state_dir)
    try:
        if args.action == "template":
            registry.template(args.template, args.template_version, json.loads(load(args.file)))
            return {"template":args.template,"version":args.template_version}
        if args.action == "draft":
            value = json.loads(load(args.file))
            return registry.revise(args.employee, args.template, args.template_version,
                value.get("overrides", {}), expected_version=args.expected_version, identity=value.get("identity"))
        if args.action == "show": return registry.history(args.employee)
        config = json.loads(load(args.config, private=True))
        if args.action == "disable":
            identity = json.loads(registry.get(args.employee)["identity"])
            return await registry.disable(args.employee, Admin(config["admin"], identity["scope"], registry), expected_version=args.expected_version)
        client = Client(config["employees"][args.employee])
        if args.action == "publish": return await registry.publish(args.employee, client, expected_version=args.expected_version)
        return await registry.submit(args.employee, args.work_type, args.sample, args.goal, args.key, client)
    finally: registry.close()


def main():
    parser = argparse.ArgumentParser(description="Independent workforce registry administration")
    parser.add_argument("--state-dir", required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("template", "draft", "publish", "disable", "submit", "show"):
        command = commands.add_parser(name)
        if name != "template": command.add_argument("--employee", required=True)
        if name in ("template", "draft"):
            command.add_argument("--template", required=True); command.add_argument("--template-version", type=int, required=True)
            command.add_argument("--file", required=True)
        if name in ("draft", "publish", "disable"): command.add_argument("--expected-version", type=int, required=True)
        if name in ("publish", "disable", "submit"): command.add_argument("--config", required=True)
        if name == "submit":
            for flag in ("work-type", "sample", "goal", "key"): command.add_argument("--"+flag, required=True)
    args = parser.parse_args()
    try: print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2)); return 0
    except (OSError, ValueError, KeyError, TypeError, TimeoutError, WorkloadError) as exc:
        code = str(exc) if isinstance(exc, RegistryError) else getattr(exc,"code","WORKFORCE_OPERATION_UNCONFIRMED")
        print(json.dumps({"error":code})); return 2


if __name__ == "__main__": raise SystemExit(main())
