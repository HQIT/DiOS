"""Qualification file adapter, not an installed/background DiOS Connector.

Assumptions: administrator-owned inbox configuration and immutable ready files
published by atomic rename on one filesystem. This is not a sandbox for hostile
concurrent filesystem mutation. Reading does not acknowledge or remove a file.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

MAX_BYTES = 1024 * 1024


class IngressError(ValueError):
    pass


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400  # Windows reparse point
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IngressError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _invalid_constant(value):
    raise IngressError("NON_FINITE_JSON_NUMBER")


def read_ready_event(inbox: Path, name: str, connector_id: str) -> dict | None:
    """Return a source-bound event; skip unpublished files; fail on bad input.

    Delivery ID becomes the normalized subject used by DiOS generic dedup.
    The original human subject is preserved as deliverysubject. Neither the
    filename nor message data can choose the source or event type.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", connector_id):
        raise IngressError("INVALID_CONNECTOR_ID")
    if not name or name in {".", ".."} or any(c in name for c in "/\\:"):
        raise IngressError("INVALID_FILENAME")
    if not name.endswith(".ready.json"):
        return None
    root = Path(os.path.abspath(inbox))
    for ancestor in (root, *root.parents):
        if _linked(ancestor.lstat()):
            raise IngressError("LINKED_INBOX")
    if not root.is_dir():
        raise IngressError("INBOX_NOT_DIRECTORY")
    path = root / name
    before = path.lstat()
    if _linked(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise IngressError("NOT_SINGLE_REGULAR_FILE")
    if before.st_size > MAX_BYTES:
        raise IngressError("MESSAGE_TOO_LARGE")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise IngressError("FILE_CHANGED")
        raw = handle.read(MAX_BYTES + 1)
        after = os.fstat(handle.fileno())
    if len(raw) > MAX_BYTES:
        raise IngressError("MESSAGE_TOO_LARGE")
    if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise IngressError("FILE_CHANGED")
    return normalize_envelope(raw, connector_id, "file")


def normalize_envelope(raw: bytes, connector_id: str, mechanism: str) -> dict:
    """Shared envelope contract; mechanism/id come from trusted configuration."""
    if mechanism not in {"file", "amqp", "http"}:
        raise IngressError("UNSUPPORTED_MECHANISM")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", connector_id):
        raise IngressError("INVALID_CONNECTOR_ID")
    if len(raw) > MAX_BYTES:
        raise IngressError("MESSAGE_TOO_LARGE")
    try:
        envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise IngressError("INVALID_JSON") from exc
    if not isinstance(envelope, dict) or set(envelope) != {"id", "subject", "data"}:
        raise IngressError("INVALID_ENVELOPE_FIELDS")
    external_id = envelope["id"]
    if not isinstance(external_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", external_id):
        raise IngressError("INVALID_EVENT_ID")
    if not isinstance(envelope["subject"], str) or len(envelope["subject"]) > 256:
        raise IngressError("INVALID_SUBJECT")
    if not isinstance(envelope["data"], dict):
        raise IngressError("INVALID_DATA")
    try:
        canonical = json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (ValueError, RecursionError) as exc:
        raise IngressError("INVALID_JSON_VALUE") from exc
    return {"specversion": "1.0", "id": external_id,
            "source": f"{mechanism}/{connector_id}", "type": f"{mechanism}.received",
            "subject": external_id, "data": envelope["data"],
            "deliverysubject": envelope["subject"], "deliverydigest": digest}


async def admit_ready_file(inbox, name, connector_id, db):
    """Read a real file and call the native router/dispatcher; do not ack it.

    The caller must configure a source/type declaration and subscriptions.
    Durable conflict handling and delivery completion are separate qualification
    work; this entrypoint alone is not a recoverable production consumer.
    """
    from sqlalchemy import select
    from app.models.tables import Subscription
    from app.services.event_router import match_subscriptions
    from app.services.event_dispatcher import dispatch_event

    event = read_ready_event(inbox, name, connector_id)
    if event is None:
        return None, "UNPUBLISHED"
    rows = await db.execute(select(Subscription).where(Subscription.enabled == True))
    targets = match_subscriptions(event, list(rows.scalars().all()))
    return await dispatch_event(event, targets, db)
