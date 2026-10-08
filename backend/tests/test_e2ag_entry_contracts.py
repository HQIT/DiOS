"""Native admission, approval and Connector checks; isolated DB, no containers."""
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select
from fastapi import HTTPException

from app.api.os.events import receive_webhook
from app.models.tables import Agent, A2ATask, E2AGApproval, EventLog
from app.services import a2a_service
from app.services.e2ag_approval import decide_approval
from app.services.event_dispatcher import dispatch_event
from app.services.event_normalizer import detect_and_normalize
import test_e2ag_dispatcher as base_tests
from starlette.requests import Request


class EntryContractTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base_tests.DispatcherGovernanceTests.asyncSetUp
    asyncTearDown = base_tests.DispatcherGovernanceTests.asyncTearDown

    async def agents(self, db, mixed=False):
        for name in ("a", "b"):
            db.add(Agent(id=name, name=name, mode="task", capabilities={"governance": {
                "allowed_event_sources": ["github/acme/*"], "allowed_event_types": ["git.push"],
                "allowed_actions": ["other"] if mixed and name == "b" else ["report.create", "secret.read"],
            }}))
        await db.commit()

    async def send(self, *, db, agent_id, message, context_id, trace_id):
        return await a2a_service.create_task(db, agent_id=agent_id, message=message,
                                           context_id=context_id, trace_id=trace_id)

    async def test_all_targets_allow(self):
        async with self.sessions() as db:
            await self.agents(db)
            with patch("app.services.event_dispatcher.a2a_service.send_message", self.send):
                log, error = await dispatch_event(base_tests.event("github/acme/repo", "git.push", {"requested_action": "report.create"}), ["a", "b"], db)
            tasks = (await db.execute(select(A2ATask))).scalars().all()
            self.assertIsNone(error)
            self.assertEqual(2, len(tasks))
            self.assertEqual({"a", "b"}, {t.agent_id for t in tasks})
            self.assertTrue(all(t.context_id == log.id and t.trace_id == log.trace_id for t in tasks))

    async def test_one_target_denies_whole_event_before_approval(self):
        async with self.sessions() as db:
            await self.agents(db, mixed=True)
            with patch("app.services.event_dispatcher.a2a_service.send_message", self.send):
                log, _ = await dispatch_event(base_tests.event("github/acme/repo", "git.push", {"requested_action": "secret.read", "environment": "production"}), ["a", "b"], db)
            self.assertEqual("denied", log.status)
            self.assertEqual([], (await db.execute(select(A2ATask))).scalars().all())
            self.assertEqual([], (await db.execute(select(E2AGApproval))).scalars().all())

    async def pending(self, db):
        await self.agents(db)
        log, _ = await dispatch_event(base_tests.event("github/acme/repo", "git.push", {"requested_action": "secret.read", "environment": "production"}), ["a", "b"], db)
        return log, (await db.execute(select(E2AGApproval))).scalar_one()

    async def test_one_event_approval_holds_both_targets(self):
        async with self.sessions() as db:
            log, approval = await self.pending(db)
            self.assertEqual("pending", approval.status)
            self.assertEqual(log.id, approval.event_log_id)
            self.assertEqual(["a", "b"], log.matched_agent_ids)
            self.assertEqual([], (await db.execute(select(A2ATask))).scalars().all())

    async def test_approval_resumes_saved_targets(self):
        async with self.sessions() as db:
            log, approval = await self.pending(db)
            with patch("app.services.event_dispatcher.a2a_service.send_message", self.send):
                await decide_approval(db, approval.id, decision="approved", actor="test-reviewer")
                with self.assertRaisesRegex(ValueError, "APPROVAL_ALREADY_DECIDED"):
                    await decide_approval(db, approval.id, decision="approved", actor="test-reviewer")
            tasks = (await db.execute(select(A2ATask))).scalars().all()
            self.assertEqual(2, len(tasks))
            self.assertTrue(all(t.context_id == log.id and t.trace_id == log.trace_id for t in tasks))


class ConnectorEntryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base_tests.DispatcherGovernanceTests.asyncSetUp
    asyncTearDown = base_tests.DispatcherGovernanceTests.asyncTearDown

    def case(self, platform, wrong=False):
        secret = "local-regression-secret-not-a-credential"
        payload = {"ref": "refs/heads/test", "repository": {"full_name": "acme/example"},
                   "project": {"path_with_namespace": "acme/example"}}
        body = json.dumps(payload, sort_keys=True).encode()
        if platform == "gitlab":
            headers = {"x-gitlab-event": "Push Hook", "x-gitlab-token": "wrong" if wrong else secret}
        else:
            digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            headers = {f"x-{platform}-event": "push"}
            key = "x-hub-signature-256" if platform == "github" else "x-gitea-signature"
            headers[key] = ("sha256=" if platform == "github" else "") + ("0" * 64 if wrong else digest)
        return body, payload, headers, {platform: secret}

    async def invoke(self, db, platform, wrong=False):
        body, _, headers, secrets = self.case(platform, wrong)

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request({"type": "http", "method": "POST", "path": "/webhook/test",
                           "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}, receive)

        async def configured(_db):
            return secrets

        with patch("app.api.os.events._webhook_secrets", configured):
            return await receive_webhook("test", request, db)

    async def test_three_native_adapters_admit_signed_push(self):
        async with self.sessions() as db:
            for platform in ("github", "gitlab", "gitea"):
                with self.subTest(platform=platform):
                    result = await self.invoke(db, platform)
                    self.assertEqual("git.push", result["type"])
                    self.assertEqual(f"{platform}/acme/example", result["source"])
                    self.assertEqual("allow", result["contract_decision"]["decision"])
            self.assertEqual(3, len((await db.execute(select(EventLog))).scalars().all()))

    async def test_bad_credentials_stop_before_event_creation(self):
        async with self.sessions() as db:
            for platform in ("github", "gitlab", "gitea"):
                with self.subTest(platform=platform):
                    with self.assertRaises(HTTPException) as caught:
                        await self.invoke(db, platform, wrong=True)
                    self.assertEqual(403, caught.exception.status_code)
            self.assertEqual([], (await db.execute(select(EventLog))).scalars().all())

    async def test_hmac_binds_body_but_gitlab_token_does_not(self):
        for platform in ("github", "gitea"):
            body, payload, headers, secrets = self.case(platform)
            with self.assertRaises(ValueError):
                detect_and_normalize(headers, payload, body + b" ", secrets)
        body, payload, headers, secrets = self.case("gitlab")
        self.assertEqual("git.push", detect_and_normalize(headers, payload, body + b" ", secrets)["type"])

    async def test_empty_secret_is_development_bypass_not_authentication(self):
        for platform in ("github", "gitlab", "gitea"):
            body, payload, headers, _ = self.case(platform, wrong=True)
            self.assertEqual("git.push", detect_and_normalize(headers, payload, body, {})["type"])


if __name__ == "__main__":
    unittest.main()
