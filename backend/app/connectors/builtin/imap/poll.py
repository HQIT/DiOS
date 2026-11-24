"""Bounded IMAP collection with UID addressing and durable-event acknowledgement."""

from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import ssl

from app.connectors.contracts import EventSink, PollInstance
from app.connectors.events import make_event

logger = logging.getLogger(__name__)


def parse_message(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw)
    body = ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if not msg.is_multipart() or part.get_content_type() == "text/plain":
            body = (part.get_payload(decode=True) or b"").decode(
                "utf-8", errors="replace"
            )[:2000]
            break
    return {
        "from": msg.get("From", ""),
        "to": msg.get("To", ""),
        "subject": msg.get("Subject", ""),
        "body_preview": body,
    }


def _ok(response):
    status, data = response
    if status != "OK":
        raise RuntimeError("IMAP_COMMAND_FAILED")
    return data


class ImapAdapter:
    async def poll(self, instance: PollInstance, emit: EventSink) -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(
            asyncio.to_thread(self._collect, instance, emit, loop)
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Native I/O must drain before the caller releases instance ownership.
            await task
            raise

    def _collect(self, instance, emit, loop):
        config = instance.config
        host = (config.get("host") or "").strip()
        user = (config.get("user") or "").strip()
        password = (config.get("password") or "").strip()
        if not host or not user or not password:
            raise ValueError("IMAP_CONFIG_INCOMPLETE")
        mailbox = config.get("mailbox", "INBOX")
        with imaplib.IMAP4_SSL(
            host,
            port=int(config.get("port") or 993),
            timeout=15,
            ssl_context=ssl.create_default_context(),
        ) as client:
            client.socket().settimeout(15)
            _ok(client.login(user, password))
            _ok(client.select(mailbox, readonly=False))
            _, validity_data = client.response("UIDVALIDITY")
            if not validity_data or not validity_data[0]:
                raise RuntimeError("IMAP_UIDVALIDITY_MISSING")
            validity = validity_data[0].decode("ascii")
            data = _ok(client.uid("search", None, "UNSEEN"))
            for uid in (data[0] or b"").split()[:10]:
                try:
                    parts = _ok(client.uid("fetch", uid, "(BODY.PEEK[])"))
                    raw = next(
                        part[1]
                        for part in parts
                        if isinstance(part, tuple) and isinstance(part[1], bytes)
                    )
                    parsed = parse_message(raw)
                    parsed["message_id"] = f"{mailbox}:{validity}:{uid.decode('ascii')}"
                    event = make_event(
                        source=f"imap/{instance.id}",
                        event_type="email.received",
                        subject=parsed["subject"],
                        data=parsed,
                    )
                    asyncio.run_coroutine_threadsafe(emit(event), loop).result()
                    _ok(client.uid("store", uid, "+FLAGS", "\\Seen"))
                except Exception as exc:  # noqa: BLE001 - isolate plugin failures; redact external replies
                    logger.warning(
                        "IMAP connector %s message failed: %s",
                        instance.id,
                        type(exc).__name__,
                    )
