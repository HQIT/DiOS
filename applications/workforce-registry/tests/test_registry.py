import copy
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

from dios_workforce.registry import Registry, RegistryError


IDENTITY = {"issuer":"test:issuer","scope":"test","id":"agent-a","subject":"subject:a","actor":"actor:a"}


def sample(digest="a"*64):
    return {"name":"技术助手","role":"研发支持","responsibilities":"提供带出处的分析","owner":"负责人",
            "audience":"团队成员","capability_refs":["review"],"knowledge_refs":[],
            "bindings":{"review":{"profile_id":"review","profile_digest":digest,"requires":{"e2ag-work":">=1,<2"}}}}


class Client:
    def __init__(self):
        self.owner=copy.deepcopy(IDENTITY); self.fingerprint="a"*64; self.mode="e2ag"
        self.requests=[]; self.runs={}; self.fail=False
    async def identity(self):
        return {"mode":"principal","principal":self.owner,"profiles":{"review":["run.read","run.submit"]}}
    async def profiles(self):
        return [{"id":"review","profile_digest":self.fingerprint,"governance":self.mode,"policy_configured":True,
                 "execution_kind":"managed-process","revision_ref":"revision-1","deployment_id":None,"implementation":"fixture"}]
    async def runtime_contract(self):
        return {"interfaces":{"e2ag-work":"1.0","profile-precondition":"1.0"},
                "execution_interfaces":{"jsonl-v1":{"confirmed-cancellation":"1.0"}}}
    async def submit(self, **request):
        self.requests.append(copy.deepcopy(request))
        if request["expected_profile_digest"] != self.fingerprint: raise RegistryError("PROFILE_VERSION_CONFLICT")
        result=self.runs.setdefault(request["idempotency_key"], {"id":"run-"+str(len(self.runs))})
        if self.fail: self.fail=False; raise TimeoutError()
        return result


class Admin:
    def __init__(self): self.version=1; self.enabled=True; self.fail=False; self.calls=[]
    async def projection(self,pid): return {"issuer":"test:issuer","scope":"test","revision":self.version}
    async def apply(self, patch):
        self.calls.append(copy.deepcopy(patch)); self.enabled=False
        if self.version == patch["expected_revision"]: self.version+=1
        if self.fail: self.fail=False; raise TimeoutError()
        return {"principal_id":patch["principal_id"],"revision":self.version,"enabled":False}


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)/"registry"
        self.registry=Registry(self.path); self.client=Client()
        self.registry.template("assistant",1,sample())
        self.registry.revise("employee", "assistant",1,{},expected_version=0,identity=IDENTITY)
    def tearDown(self): self.registry.close(); self.temp.cleanup()

    def test_cli_reads_durable_history_and_utf8_after_writer_exits(self):
        self.registry.close()
        try:
            result=subprocess.run([sys.executable,'-m','dios_workforce.cli','--state-dir',str(self.path),
                'show','--employee','employee'],capture_output=True,text=True,check=True)
            view=json.loads(result.stdout)
            self.assertEqual(json.loads(view['revisions'][0]['definition'])['name'],'技术助手')
            self.assertEqual(view['employee']['id'],'employee')
        finally: self.registry=Registry(self.path)

    async def test_template_snapshot_and_history_survive_restart(self):
        await self.registry.publish("employee",self.client,expected_version=1)
        self.registry.template("assistant",2,sample("b"*64))
        self.registry.revise("employee","assistant",2,{"name":"新名称"},expected_version=2)
        self.registry.close(); self.registry=Registry(self.path)
        view=self.registry.history("employee")
        self.assertEqual(view["employee"]["active"],1)
        self.assertEqual(self.registry.revision("employee",1)["name"],"技术助手")
        self.assertEqual(self.registry.revision("employee",2)["name"],"新名称")
        with self.assertRaisesRegex(RegistryError,"IMMUTABLE_TEMPLATE_CONFLICT"):
            self.registry.template("assistant",1,sample("c"*64))

    async def test_unsupported_requirements_cannot_replace_publication(self):
        await self.registry.publish("employee",self.client,expected_version=1)
        for requirement in ({"unknown-feature":">=1"},{"e2ag-work":">=2"},{"profile-precondition":">=2"}):
            body=sample(); body["bindings"]["review"]["requires"]=requirement
            revision=self.registry.get("employee")["draft"]+1
            self.registry.template("assistant",revision,body)
            self.registry.revise("employee","assistant",revision,{},expected_version=self.registry.get("employee")["version"])
            with self.assertRaisesRegex(RegistryError,"REQUIRED_CAPABILITY_UNSUPPORTED"):
                await self.registry.publish("employee",self.client,expected_version=self.registry.get("employee")["version"])
            self.assertEqual(self.registry.get("employee")["active"],1)

    async def test_no_e2ag_or_changed_identity_or_profile_rejects(self):
        self.client.mode="none"
        with self.assertRaisesRegex(RegistryError,"E2AG_REQUIRED"):
            await self.registry.publish("employee",self.client,expected_version=1)
        self.client.mode="e2ag"; self.client.fingerprint="b"*64
        with self.assertRaisesRegex(RegistryError,"PROFILE_VERSION_CONFLICT"):
            await self.registry.publish("employee",self.client,expected_version=1)
        self.client.owner["id"]="other"
        with self.assertRaisesRegex(RegistryError,"EMPLOYEE_IDENTITY_MISMATCH"):
            await self.registry.publish("employee",self.client,expected_version=1)

    async def test_lost_submission_receipt_resumes_original_task(self):
        await self.registry.publish("employee",self.client,expected_version=1)
        self.client.fail=True
        with self.assertRaises(TimeoutError): await self.registry.submit("employee","review","first-item","goal","key",self.client)
        self.registry.close(); self.registry=Registry(self.path)
        first=await self.registry.submit("employee","review","first-item","goal","key",self.client)
        second=await self.registry.submit("employee","review","first-item","goal","key",self.client)
        self.assertEqual(first["run_id"],second["run_id"])
        self.assertEqual(self.client.requests[0],self.client.requests[1]); self.assertEqual(len(self.client.requests),2)
        with self.assertRaisesRegex(RegistryError,"IDEMPOTENCY_CONFLICT"):
            await self.registry.submit("employee","review","first-item","other","key",self.client)

    async def test_disable_uncertainty_blocks_new_work_then_reconciles(self):
        await self.registry.publish("employee",self.client,expected_version=1)
        admin=Admin(); admin.fail=True
        with self.assertRaises(TimeoutError): await self.registry.disable("employee",admin,expected_version=2)
        self.assertEqual(self.registry.get("employee")["state"],"disabling")
        with self.assertRaisesRegex(RegistryError,"EMPLOYEE_NOT_ACTIVE"):
            await self.registry.submit("employee","review","first-item","goal","key",self.client)
        self.registry.close(); self.registry=Registry(self.path)
        self.assertEqual((await self.registry.disable("employee",admin,expected_version=2))["state"],"disabled")
        self.assertEqual(admin.calls[0],admin.calls[1]); self.assertEqual(admin.version,2)

    async def test_principal_exclusivity_and_optimistic_version(self):
        with self.assertRaisesRegex(RegistryError,"PRINCIPAL_ALREADY_ASSIGNED"):
            self.registry.revise("another","assistant",1,{},expected_version=0,identity=dict(IDENTITY,actor="different"))
        with self.assertRaisesRegex(RegistryError,"EMPLOYEE_VERSION_CONFLICT"):
            self.registry.revise("employee","assistant",1,{},expected_version=0)
        with self.assertRaises(BlockingIOError): Registry(self.path)

    async def test_changed_runtime_keeps_uncertain_request_and_original_revision(self):
        await self.registry.publish("employee",self.client,expected_version=1)
        self.client.fingerprint="b"*64
        with self.assertRaisesRegex(RegistryError,"PROFILE_VERSION_CONFLICT"):
            await self.registry.submit("employee","review","first-item","goal","key",self.client)
        self.assertEqual(self.registry.history("employee")["tasks"][0]["state"],"pending")
        self.assertEqual(len(self.client.runs),0)


if __name__ == "__main__": unittest.main()
