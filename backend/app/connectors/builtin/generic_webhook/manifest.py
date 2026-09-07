"""generic Connector 声明：未识别 webhook 的兜底通道。"""

from __future__ import annotations

from typing import Any

from app.connectors.contracts import (
    CAPABILITY_WEBHOOK,
    BaseWebhookAdapter,
    CloudEvent,
    ConnectorManifest,
    EventSourceDecl,
    EventTypeDecl,
)
from app.connectors.events import make_event


class GenericNormalizer(BaseWebhookAdapter):
    platform = "generic"

    def detect(self, headers: dict[str, str]) -> bool:
        return True

    def normalize(self, headers: dict[str, str], payload: dict) -> CloudEvent:
        return make_event(
            source="webhook/generic",
            event_type="webhook.received",
            data=payload,
        )


def _source_patterns(instance: Any) -> list[str]:
    return ["webhook/*"]


MANIFEST = ConnectorManifest(
    type="generic",
    label="通用 Webhook",
    description="未识别平台的 HTTP Webhook 兜底接入",
    capabilities=(CAPABILITY_WEBHOOK,),
    # 兜底 detect 恒为真，必须排在所有类型之后
    order=1000,
    config_schema={"type": "object", "properties": {}},
    event_sources=(
        EventSourceDecl(id="webhook", name="通用 Webhook", description="其他 HTTP Webhook"),
    ),
    event_types=(EventTypeDecl(type="webhook.received", description="通用 Webhook"),),
    subscribable_categories=("webhook",),
    source_patterns=_source_patterns,
    webhook_adapters=(GenericNormalizer(),),
)
