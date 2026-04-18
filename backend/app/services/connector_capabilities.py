"""Connector 能力解析：统一生成可订阅的 source pattern。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.models.tables import Connector
from app.services.event_normalizer import get_event_catalog

_LEGACY_GIT_TYPES = {"github", "gitlab", "gitea"}
_INTERNAL_SOURCE_PATTERNS = ("cron/*", "manual/*", "ai4r/*")


@dataclass
class SourcePatternItem:
    source_pattern: str
    label: str
    event_types: list[str]
    connector_id: str | None = None
    connector_name: str = ""
    connector_type: str = ""
    kind: str = "connector"  # connector | internal


def _connector_patterns(conn: Connector) -> list[str]:
    """从单个 Connector 提取可订阅 source namespace。"""
    ctype = (conn.type or "").strip()
    cfg = conn.config or {}

    if ctype == "git_webhook":
        platform = str(cfg.get("platform", "")).strip()
        return [f"{platform}/*"] if platform else []
    if ctype in _LEGACY_GIT_TYPES:
        return [f"{ctype}/*"]
    if ctype == "imap":
        return [f"imap/{conn.id}", "imap/*"]
    if ctype == "generic":
        return ["webhook/*"]
    return []


def _event_types_by_category() -> dict[str, list[str]]:
    catalog = get_event_catalog()
    grouped: dict[str, list[str]] = {}
    for item in catalog["event_types"]:
        category = item["category"]
        grouped.setdefault(category, []).append(item["type"])
    return grouped


def _connector_event_types(conn: Connector, grouped_event_types: dict[str, list[str]]) -> list[str]:
    ctype = (conn.type or "").strip()
    if ctype == "git_webhook" or ctype in _LEGACY_GIT_TYPES:
        return grouped_event_types.get("git", [])
    if ctype == "imap":
        return grouped_event_types.get("email", [])
    if ctype == "generic":
        return grouped_event_types.get("webhook", [])
    return []


def build_source_pattern_items(
    connectors: Iterable[Connector],
    *,
    enabled_only: bool = True,
    include_internal: bool = True,
) -> list[SourcePatternItem]:
    """构造所有可用于订阅的 source pattern 项。"""
    items: list[SourcePatternItem] = []
    grouped_event_types = _event_types_by_category()

    for conn in connectors:
        if enabled_only and not conn.enabled:
            continue
        event_types = _connector_event_types(conn, grouped_event_types)
        for sp in _connector_patterns(conn):
            items.append(
                SourcePatternItem(
                    source_pattern=sp,
                    label=f"{conn.name} ({sp})",
                    event_types=list(event_types),
                    connector_id=conn.id,
                    connector_name=conn.name,
                    connector_type=conn.type,
                    kind="connector",
                )
            )

    if include_internal:
        items.extend(
            [
                SourcePatternItem(
                    source_pattern=sp,
                    label=f"Internal ({sp})",
                    event_types=(
                        ["cron.tick"]
                        if sp.startswith("cron/")
                        else (
                            ["manual.trigger"]
                            if sp.startswith("manual/")
                            else grouped_event_types.get("ai4r", [])
                        )
                    ),
                    kind="internal",
                )
                for sp in _INTERNAL_SOURCE_PATTERNS
            ]
        )

    # 去重（同 pattern 仅保留首个）
    seen: set[str] = set()
    deduped: list[SourcePatternItem] = []
    for item in items:
        if item.source_pattern in seen:
            continue
        seen.add(item.source_pattern)
        deduped.append(item)
    return deduped

