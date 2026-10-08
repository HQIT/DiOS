"""Real file read + native admission; task execution is stubbed, not E2E."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments/e2ag/heterogeneity"))
from file_ingress import IngressError, read_ready_event, admit_ready_file
from app.connectors import registry
from app.connectors.contracts import ConnectorManifest, EventTypeDecl
from app.config import settings
from app.models.tables import Agent, A2ATask, Subscription, E2AGApproval
from app.services import a2a_service
from app.services.e2ag import verify_audit_chain
from app.services.event_normalizer import compute_dedup_hash
from sqlalchemy import select
import test_e2ag_dispatcher as base_tests


class FileEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, **changes):
        envelope = {"id": "event-1", "subject": "same subject", "data": {"requested_action": "report.create"}}
        envelope.update(changes)
        (self.root / "input.ready.json").write_text(json.dumps(envelope), encoding="utf-8")
        return envelope

    def test_source_bound_and_payload_preserved(self):
        original = self.write(data={"source": "github/trusted", "requested_action": "report.create"})
        event = read_ready_event(self.root, "input.ready.json", "fixture")
        self.assertEqual("file/fixture", event["source"])
        self.assertEqual(original["data"], event["data"])
        self.assertEqual(original["subject"], event["deliverysubject"])

    def test_unpublished_skipped(self):
        (self.root / "input.tmp").write_bytes(b'{"id":')
        self.assertIsNone(read_ready_event(self.root, "input.tmp", "fixture"))

    def test_publish_makes_visible(self):
        self.write()
        path = self.root / "input.ready.json"
        path.rename(self.root / "input.tmp")
        self.assertIsNone(read_ready_event(self.root, "input.tmp", "fixture"))
        (self.root / "input.tmp").rename(path)
        self.assertEqual("event-1", read_ready_event(self.root, path.name, "fixture")["id"])

    def test_paths_rejected(self):
        for name in ("../x.ready.json", "a/x.ready.json", "C:x.ready.json", "a\\x.ready.json"):
            with self.subTest(name=name), self.assertRaises(IngressError):
                read_ready_event(self.root, name, "fixture")

    def test_reserved_envelope_fields_rejected(self):
        self.write(source="github/trusted")
        with self.assertRaisesRegex(IngressError, "INVALID_ENVELOPE_FIELDS"):
            read_ready_event(self.root, "input.ready.json", "fixture")

    def test_malformed_missing_duplicate_and_nonfinite_rejected(self):
        for raw in (b"{", b"{}", b'{"id":"a","id":"b"}',
                    b'{"id":"a","subject":"s","data":{"x":NaN}}'):
            with self.subTest(raw=raw):
                (self.root / "input.ready.json").write_bytes(raw)
                with self.assertRaises(IngressError):
                    read_ready_event(self.root, "input.ready.json", "fixture")

    def test_bounded_size(self):
        (self.root / "input.ready.json").write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaisesRegex(IngressError, "MESSAGE_TOO_LARGE"):
            read_ready_event(self.root, "input.ready.json", "fixture")

    def test_directory_and_hardlink_rejected(self):
        (self.root / "directory.ready.json").mkdir()
        with self.assertRaises(IngressError):
            read_ready_event(self.root, "directory.ready.json", "fixture")
        self.write()
        os.link(self.root / "input.ready.json", self.root / "alias.ready.json")
        with self.assertRaises(IngressError):
            read_ready_event(self.root, "alias.ready.json", "fixture")

    def test_overflow_and_surrogate_rejected(self):
        for raw in (b'{"id":"a","subject":"s","data":{"x":1e999}}',
                    b'{"id":"a","subject":"s","data":{"x":"\\ud800"}}'):
            (self.root / "input.ready.json").write_bytes(raw)
            with self.assertRaises(IngressError):
                read_ready_event(self.root, "input.ready.json", "fixture")

    def test_stable_identity_and_content_digest(self):
        self.write()
        a = read_ready_event(self.root, "input.ready.json", "fixture")
        self.assertEqual(a, read_ready_event(self.root, "input.ready.json", "fixture"))
        self.write(id="event-2")
        b = read_ready_event(self.root, "input.ready.json", "fixture")
        self.assertNotEqual(compute_dedup_hash(a), compute_dedup_hash(b))
        self.write(data={"requested_action": "secret.read"})
        c = read_ready_event(self.root, "input.ready.json", "fixture")
        self.assertEqual(compute_dedup_hash(a), compute_dedup_hash(c))
        self.assertNotEqual(a["deliverydigest"], c["deliverydigest"])


class FileAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base_tests.DispatcherGovernanceTests.asyncSetUp(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        registry.all_manifests()
        self.saved_manifests = list(registry._manifests)
        registry.register(ConnectorManifest(type="file-qualification", label="File qualification",
            accepted_source_patterns=("file/fixture",),
            event_types=(EventTypeDecl("file.received"),)))

    async def asyncTearDown(self):
        registry._manifests = self.saved_manifests
        self.tmp.cleanup()
        await base_tests.DispatcherGovernanceTests.asyncTearDown(self)

    async def send(self, *, db, agent_id, message, context_id, trace_id):
        return await a2a_service.create_task(db, agent_id=agent_id, message=message,
                                            context_id=context_id, trace_id=trace_id)

    async def prepare(self, db, *, allowed_source="file/fixture", action="report.create"):
        db.add(Agent(id="file-agent", name="file-agent", mode="task", capabilities={"governance": {
            "allowed_event_sources": [allowed_source], "allowed_event_types": ["file.received"],
            "allowed_actions": ["report.create", "secret.read"]}}))
        db.add(Subscription(agent_id="file-agent", source_pattern="file/*", event_types=["file.received"]))
        await db.commit()
        envelope = {"id": "event-1", "subject": "review", "data": {
            "requested_action": action, "environment": "production"}}
        (self.root / "input.ready.json").write_text(json.dumps(envelope), encoding="utf-8")

    async def run_entry(self, db):
        with patch("app.services.event_dispatcher.a2a_service.send_message", self.send):
            return await admit_ready_file(self.root, "input.ready.json", "fixture", db)

    async def test_allowed_file_creates_linked_task(self):
        async with self.sessions() as db:
            await self.prepare(db)
            log, error = await self.run_entry(db)
            self.assertIsNone(error)
            task = (await db.execute(select(A2ATask))).scalar_one()
            self.assertEqual(log.id, task.context_id)
            self.assertEqual(log.trace_id, task.trace_id)
            self.assertTrue(verify_audit_chain(log.audit_chain))

    async def test_target_denial_creates_no_task(self):
        async with self.sessions() as db:
            await self.prepare(db, allowed_source="file/other")
            log, _ = await self.run_entry(db)
            self.assertEqual("denied", log.status)
            self.assertEqual([], (await db.execute(select(A2ATask))).scalars().all())
            self.assertTrue(verify_audit_chain(log.audit_chain))

    async def test_approval_holds_task(self):
        async with self.sessions() as db:
            await self.prepare(db, action="secret.read")
            log, _ = await self.run_entry(db)
            self.assertEqual("approval_required", log.status)
            self.assertEqual("pending", (await db.execute(select(E2AGApproval))).scalar_one().status)
            self.assertEqual([], (await db.execute(select(A2ATask))).scalars().all())

    async def test_repeat_scan_does_not_create_second_task(self):
        settings.event_dedup_enabled = True
        async with self.sessions() as db:
            await self.prepare(db)
            first, _ = await self.run_entry(db)
        async with self.sessions() as db:
            second, error = await self.run_entry(db)
            self.assertIsNone(second)
            self.assertIn(first.id, error)
            self.assertEqual(1, len((await db.execute(select(A2ATask))).scalars().all()))


if __name__ == "__main__":
    unittest.main()
