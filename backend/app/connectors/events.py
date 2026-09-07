"""CloudEvent 构造工具，供 Connector 适配器与内部发布方共用。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.connectors.contracts import CloudEvent


def make_event(
    *,
    source: str,
    event_type: str,
    subject: str = "",
    data: dict,
) -> CloudEvent:
    return {
        "specversion": "1.0",
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "source": source,
        "type": event_type,
        "subject": subject,
        "time": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
