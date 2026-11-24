from __future__ import annotations

import asyncio
import ssl
import threading
import unittest
from unittest.mock import ANY, AsyncMock, patch

from app.connectors.builtin.imap.manifest import MANIFEST
from app.connectors.builtin.imap.poll import ImapAdapter, parse_message
from app.connectors.contracts import PollInstance
from app.services.event_normalizer import compute_dedup_hash


class Mailbox:
    def __init__(self, count=12, validity=b"42"):
        self.count, self.validity = count, validity
        self.calls, self.seen = [], []
        self.raw = b"From: sender@example.test\r\nTo: inbox@example.test\r\nSubject: Same subject\r\n\r\nhello"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def socket(self):
        return self

    def settimeout(self, seconds):
        self.timeout = seconds

    def login(self, *_):
        return "OK", []

    def select(self, *_, **__):
        return "OK", [b"12"]

    def response(self, _):
        return "UIDVALIDITY", [self.validity]

    def uid(self, operation, *args):
        self.calls.append((operation, args))
        if operation == "search":
            return "OK", [b" ".join(str(x).encode() for x in range(1, self.count + 1))]
        if operation == "fetch":
            return "OK", [(b"1 BODY[]", self.raw), b")"]
        if operation == "store":
            self.seen.append(args[0])
            return "OK", []
        raise AssertionError(operation)


class ImapAdapterTests(unittest.IsolatedAsyncioTestCase):
    def instance(self):
        return PollInstance(
            "mailbox", {"host": "localhost", "user": "test", "password": "test"}
        )

    async def test_batch_interval_uid_peek_and_ack_after_each_durable_emit(self):
        mailbox, events = Mailbox(), []

        async def emit(event):
            self.assertEqual(len(events), len(mailbox.seen))
            events.append(event)

        with patch(
            "app.connectors.builtin.imap.poll.imaplib.IMAP4_SSL", return_value=mailbox
        ) as factory:
            await ImapAdapter().poll(self.instance(), emit)
        self.assertEqual(60, MANIFEST.poll_interval_seconds)
        self.assertEqual(10, len(events))
        self.assertEqual(10, len(mailbox.seen))
        self.assertEqual(10, len({compute_dedup_hash(event) for event in events}))
        self.assertEqual("imap/mailbox", events[0]["source"])
        self.assertEqual("email.received", events[0]["type"])
        self.assertTrue(
            all(
                args[1] == "(BODY.PEEK[])"
                for op, args in mailbox.calls
                if op == "fetch"
            )
        )
        factory.assert_called_once_with(
            "localhost", port=993, timeout=15, ssl_context=ANY
        )
        context = factory.call_args.kwargs["ssl_context"]
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        self.assertTrue(context.check_hostname)
        self.assertEqual(15, mailbox.timeout)

    async def test_failed_emit_keeps_message_unread_and_continues_batch(self):
        mailbox = Mailbox(2)
        emit = AsyncMock(side_effect=[RuntimeError("persistence unavailable"), None])
        with patch(
            "app.connectors.builtin.imap.poll.imaplib.IMAP4_SSL", return_value=mailbox
        ):
            await ImapAdapter().poll(self.instance(), emit)
        self.assertEqual([b"2"], mailbox.seen)
        self.assertEqual(2, emit.await_count)

    async def test_protocol_error_is_not_a_successful_poll(self):
        mailbox = Mailbox()
        mailbox.select = lambda *a, **k: ("NO", [b"sensitive server reply"])
        with (
            patch(
                "app.connectors.builtin.imap.poll.imaplib.IMAP4_SSL",
                return_value=mailbox,
            ),
            self.assertRaisesRegex(RuntimeError, "IMAP_COMMAND_FAILED"),
        ):
            await ImapAdapter().poll(self.instance(), AsyncMock())
        self.assertEqual([], mailbox.seen)

    async def test_missing_uidvalidity_does_not_ack_ambiguous_deliveries(self):
        with (
            patch(
                "app.connectors.builtin.imap.poll.imaplib.IMAP4_SSL",
                return_value=Mailbox(validity=None),
            ),
            self.assertRaisesRegex(RuntimeError, "UIDVALIDITY"),
        ):
            await ImapAdapter().poll(self.instance(), AsyncMock())

    async def test_repeat_uid_is_stable_and_mailbox_reset_has_new_identity(self):
        events = []

        async def emit(event):
            events.append(event)

        for validity in (b"42", b"42", b"43"):
            with patch(
                "app.connectors.builtin.imap.poll.imaplib.IMAP4_SSL",
                return_value=Mailbox(1, validity),
            ):
                await ImapAdapter().poll(self.instance(), emit)
        hashes = [compute_dedup_hash(e) for e in events]
        self.assertEqual(hashes[0], hashes[1])
        self.assertNotEqual(hashes[1], hashes[2])

    async def test_blocking_io_and_cancellation_drain_without_blocking_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        mailbox = Mailbox(0)

        def login(*_):
            entered.set()
            release.wait(3)
            return "OK", []

        mailbox.login = login
        with patch(
            "app.connectors.builtin.imap.poll.imaplib.IMAP4_SSL", return_value=mailbox
        ):
            task = asyncio.create_task(ImapAdapter().poll(self.instance(), AsyncMock()))
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set():
                        await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_parse_keeps_preview_size_and_multipart_semantics(self):
        raw = b"Subject: test\r\n\r\n" + b"x" * 3000
        self.assertEqual(2000, len(parse_message(raw)["body_preview"]))
        raw = b'Content-Type: multipart/alternative; boundary="b"\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nhello\r\n--b--\r\n'
        self.assertEqual("hello", parse_message(raw)["body_preview"])


if __name__ == "__main__":
    unittest.main()
