"""imap Connector 声明。

轮询实现目前仍在 app/services/imap_poller.py，本 manifest 只声明能力与事件。
"""

from __future__ import annotations

from typing import Any

from app.connectors.contracts import (
    CAPABILITY_POLL,
    ConnectorManifest,
    EventSourceDecl,
    EventTypeDecl,
)


def _source_patterns(instance: Any) -> list[str]:
    return [f"imap/{getattr(instance, 'id', '')}", "imap/*"]


MANIFEST = ConnectorManifest(
    type="imap",
    label="IMAP 邮箱",
    description="轮询邮箱并产出 email.received 事件",
    capabilities=(CAPABILITY_POLL,),
    order=20,
    config_schema={
        "type": "object",
        "required": ["host", "user", "password"],
        "properties": {
            "host": {"type": "string", "title": "IMAP 主机"},
            "port": {"type": "integer", "title": "端口", "default": 993},
            "user": {"type": "string", "title": "账号"},
            "password": {"type": "string", "title": "密码"},
            "mailbox": {"type": "string", "title": "邮箱文件夹", "default": "INBOX"},
        },
    },
    secret_fields=("password",),
    event_sources=(EventSourceDecl(id="email", name="邮件", description="IMAP 收取邮件"),),
    event_types=(EventTypeDecl(type="email.received", description="邮件收取（IMAP）"),),
    subscribable_categories=("email",),
    source_patterns=_source_patterns,
)
