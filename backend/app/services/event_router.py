"""事件订阅匹配：遍历 Subscription 表，返回匹配的 agent_id 列表。"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

from app.models.tables import Subscription
from app.services.event_normalizer import CloudEvent


def _resolve_path(data: Any, path: str) -> str | None:
    """按点分路径从嵌套 dict 中取值，如 'data.repository.full_name'。"""
    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return str(current) if current is not None else None


def _match_filter(event: CloudEvent, filter_rules: dict[str, str]) -> bool:
    """检查事件是否满足所有 filter 规则。

    filter_rules 格式: {"data.repository.full_name": "HQIT/*"}
    值支持 glob 匹配。
    """
    for path, pattern in filter_rules.items():
        actual = _resolve_path(event, path)
        if actual is None:
            return False
        if not fnmatch(actual, pattern):
            return False
    return True


def match_subscriptions(
    event: CloudEvent,
    subscriptions: list[Subscription],
) -> list[str]:
    """返回所有匹配该事件的 agent_id 列表。"""
    matched: list[str] = []

    event_source = event.get("source", "")
    event_type = event.get("type", "")

    for sub in subscriptions:
        if not sub.enabled:
            continue

        if not fnmatch(event_source, sub.source_pattern):
            continue

        type_matched = any(
            fnmatch(event_type, t) for t in (sub.event_types or [])
        )
        if not type_matched:
            continue

        if sub.filter_rules and not _match_filter(event, sub.filter_rules):
            continue

        matched.append(sub.agent_id)

    return matched
