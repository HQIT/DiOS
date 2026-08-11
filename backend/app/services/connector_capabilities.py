"""Connector 能力解析：统一生成可订阅的 source pattern。

具体类型的 pattern 与事件类型由各自 manifest 声明，本模块不做类型判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.connectors import registry
from app.models.tables import Connector
from app.services.event_normalizer import get_event_catalog


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
    manifest = registry.manifest_for(conn.type)
    return manifest.patterns_for(conn) if manifest else []


def _event_types_by_category() -> dict[str, list[str]]:
    catalog = get_event_catalog()
    grouped: dict[str, list[str]] = {}
    for item in catalog["event_types"]:
        category = item["category"]
        grouped.setdefault(category, []).append(item["type"])
    return grouped


def _connector_event_types(conn: Connector, grouped_event_types: dict[str, list[str]]) -> list[str]:
    manifest = registry.manifest_for(conn.type)
    if not manifest:
        return []
    types: list[str] = []
    for category in manifest.categories():
        types.extend(grouped_event_types.get(category, []))
    return types


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
            SourcePatternItem(
                source_pattern=ns.source_pattern,
                label=f"Internal ({ns.source_pattern})",
                event_types=[decl.type for decl in ns.event_types],
                kind="internal",
            )
            for ns in registry.event_namespaces()
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
