"""事件标准化入口：把外部输入转成 CloudEvents。

平台识别与解析逻辑由 Connector 插件提供（见 app/connectors/），本模块只负责
遍历注册表、校验签名并汇总事件目录，不感知具体平台。
"""

from __future__ import annotations

import hashlib

from app.connectors import registry
from app.connectors.contracts import BaseWebhookAdapter, CloudEvent
from app.connectors.events import make_event

# 兼容旧引用
_make_event = make_event
BaseNormalizer = BaseWebhookAdapter

__all__ = [
    "CloudEvent",
    "BaseNormalizer",
    "compute_dedup_hash",
    "detect_and_normalize",
    "get_event_catalog",
]


def compute_dedup_hash(event: CloudEvent) -> str:
    """基于事件特征生成去重哈希。
    
    不同类型事件使用不同的特征：
    - Git 事件：仓库 + PR/Issue 编号 + 动作
    - 邮件事件：Message-ID（如有）
    - 通用事件：source + type + subject
    """
    event_type = event.get("type", "")
    data = event.get("data", {})
    
    # Git 类事件：使用仓库 + PR/Issue 编号 + 动作
    if event_type.startswith("git."):
        repo = data.get("repository", {})
        repo_name = repo.get("full_name", "") if isinstance(repo, dict) else ""
        # GitHub/GitLab/Gitea 事件的编号字段位置不一致，统一抽取
        number = (
            data.get("number")
            or (data.get("issue", {}) or {}).get("number")
            or (data.get("pull_request", {}) or {}).get("number")
            or (data.get("object_attributes", {}) or {}).get("iid")
            or ""
        )
        key_parts = [
            event.get("source", ""),
            event_type,
            repo_name,
            str(number),  # PR/Issue 编号
            str(data.get("action", "")),
            event.get("subject", ""),
        ]
        # push 无 PR 号：必须纳入 commit SHA，否则同分支多次 push 会被误去重
        if event_type == "git.push":
            key_parts.extend([
                str(data.get("before", "")),
                str(data.get("after", "")),
            ])
    # 邮件事件：使用 Message-ID
    elif event_type.startswith("email."):
        key_parts = [
            event.get("source", ""),
            event_type,
            str(data.get("message_id", "")),
            str(data.get("subject", "")),
        ]
    # 通用事件：使用 source + type + subject
    else:
        key_parts = [
            event.get("source", ""),
            event_type,
            event.get("subject", ""),
        ]
    
    content = "|".join(str(p) for p in key_parts if p)
    return hashlib.sha256(content.encode()).hexdigest()


def get_event_catalog() -> dict:
    """返回系统支持的所有事件源和事件类型。"""
    decls = registry.event_type_decls()
    event_types = [
        {
            "type": t,
            "category": t.split(".")[0],
            "description": decls[t].description or t,
        }
        for t in sorted(decls)
    ]
    return {"sources": list(registry.event_sources()), "event_types": event_types}


def _adapter_platform(adapter) -> str:
    platform = getattr(adapter, "platform", "")
    if platform:
        return platform
    return adapter.__class__.__name__.replace("Normalizer", "").lower()


def detect_and_normalize(
    headers: dict[str, str],
    payload: dict,
    body: bytes,
    secrets: dict[str, str],
) -> CloudEvent:
    """自动检测平台并标准化事件。

    Args:
        headers: HTTP 请求 headers（key 已小写）
        payload: 解析后的 JSON body
        body: 原始 request body（用于签名验证）
        secrets: 按平台名存储的 webhook secret，如 {"github": "xxx"}

    Returns:
        CloudEvent 字典

    Raises:
        ValueError: 签名验证失败
    """
    headers_lower = {k.lower(): v for k, v in headers.items()}

    for adapter in registry.webhook_adapters():
        if not adapter.detect(headers_lower):
            continue

        platform = _adapter_platform(adapter)
        secret = secrets.get(platform, "")

        if not adapter.verify_signature(headers_lower, body, secret):
            raise ValueError(f"Webhook signature verification failed for {platform}")

        return adapter.normalize(headers_lower, payload)

    raise ValueError("No registered webhook connector accepted the request")
