"""内建事件源声明：定时与手动触发。

这些事件由 DiOS 自身产生，没有 Connector 实例，因此 instantiable=False。
"""

from __future__ import annotations

from app.connectors.contracts import (
    CAPABILITY_DECLARE_ONLY,
    ConnectorManifest,
    EventNamespaceDecl,
    EventSourceDecl,
    EventTypeDecl,
)

MANIFEST = ConnectorManifest(
    type="internal",
    label="内建事件源",
    description="CRON 定时与手动触发",
    capabilities=(CAPABILITY_DECLARE_ONLY,),
    instantiable=False,
    order=30,
    event_sources=(
        EventSourceDecl(id="manual", name="手动触发", description="手动模拟事件"),
        EventSourceDecl(id="cron", name="定时任务", description="CRON 定时事件"),
    ),
    event_namespaces=(
        EventNamespaceDecl(
            source_pattern="cron/*",
            event_types=(EventTypeDecl(type="cron.tick", description="定时触发"),),
        ),
        EventNamespaceDecl(
            source_pattern="manual/*",
            event_types=(EventTypeDecl(type="manual.trigger", description="手动触发"),),
        ),
    ),
)
