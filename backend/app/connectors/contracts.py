"""Connector 插件契约。

Connector 描述「一个外部系统如何把输入送进 DiOS」。每种类型由一份 manifest
声明配置、可订阅的 source namespace、产出的事件类型和具备的能力；具体解析逻辑
由可选的 adapter 实现。通用服务只依赖本模块，不感知具体类型。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

# 能力标识：不同 Connector 只实现自己需要的部分
CAPABILITY_WEBHOOK = "webhook"  # 被动接收 HTTP 回调
CAPABILITY_POLL = "poll"  # 主动轮询外部系统
CAPABILITY_DECLARE_ONLY = "declare_only"  # 只声明事件命名空间，由外部发布者写入

CloudEvent = dict[str, Any]


@dataclass(frozen=True)
class EventTypeDecl:
    """一个事件类型及其展示信息。"""

    type: str
    description: str = ""

    @property
    def category(self) -> str:
        return self.type.split(".")[0]


@dataclass(frozen=True)
class EventSourceDecl:
    """事件目录中的一个来源分类（Console 用于分组展示）。"""

    id: str
    name: str
    description: str = ""


@dataclass(frozen=True)
class EventNamespaceDecl:
    """一个可订阅的 source namespace 及其事件类型。

    用于不由 Connector 实例产生的事件：内建的 cron/manual，以及由 Agent 自行
    发布的场景事件（通过 event-namespaces.json 声明）。
    """

    source_pattern: str
    event_types: tuple[EventTypeDecl, ...] = ()


@runtime_checkable
class WebhookAdapter(Protocol):
    """处理入站 webhook 的适配器。"""

    def detect(self, headers: dict[str, str]) -> bool:
        """根据 HTTP header 判断是否属于本类型。"""
        ...

    def normalize(self, headers: dict[str, str], payload: dict) -> CloudEvent:
        """将原始 payload 转换为 CloudEvent。"""
        ...

    def verify_signature(self, headers: dict[str, str], body: bytes, secret: str) -> bool:
        """校验签名，无签名机制时返回 True。"""
        ...


class BaseWebhookAdapter(ABC):
    """WebhookAdapter 的默认实现基类。"""

    @abstractmethod
    def detect(self, headers: dict[str, str]) -> bool: ...

    @abstractmethod
    def normalize(self, headers: dict[str, str], payload: dict) -> CloudEvent: ...

    def verify_signature(self, headers: dict[str, str], body: bytes, secret: str) -> bool:
        return True


@dataclass(frozen=True)
class ConnectorManifest:
    """一种 Connector 类型的完整声明。

    Attributes:
        type: 类型标识，等于 Connector.type
        label: 展示名
        description: 说明文字
        capabilities: 具备的能力，取 CAPABILITY_* 常量
        aliases: 兼容的历史 type 值（旧数据不迁移即可继续工作）
        instantiable: 是否允许创建 Connector 实例；纯声明类型为 False
        order: 注册排序权重，同时决定 webhook detect 顺序，兜底类型取大值
        config_schema: JSON Schema 片段，供校验与前端渲染
        secret_fields: config 中的敏感字段，不应回显或写日志
        event_sources: 事件目录中的来源分类
        event_types: 本类型可能产出的事件类型
        accepted_source_patterns: 运行时接入契约允许的 CloudEvent source 模式
        subscribable_categories: 允许订阅的事件类型分类，留空则取 event_types 的分类
        event_namespaces: 不依赖实例的可订阅 source namespace
        source_patterns: 由实例配置推导可订阅的 source namespace
        webhook_adapters: 入站 webhook 适配器，按顺序参与 detect
        webhook_secrets: 从实例配置提取 webhook secret，按平台名归集
    """

    type: str
    label: str
    description: str = ""
    capabilities: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    instantiable: bool = True
    order: int = 100
    config_schema: dict[str, Any] = field(default_factory=dict)
    secret_fields: tuple[str, ...] = ()
    event_sources: tuple[EventSourceDecl, ...] = ()
    event_types: tuple[EventTypeDecl, ...] = ()
    accepted_source_patterns: tuple[str, ...] = ()
    subscribable_categories: tuple[str, ...] = ()
    event_namespaces: tuple[EventNamespaceDecl, ...] = ()
    source_patterns: Callable[[Any], list[str]] | None = None
    webhook_adapters: tuple[Any, ...] = ()
    webhook_secrets: Callable[[Any], dict[str, str]] | None = None

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def categories(self) -> tuple[str, ...]:
        if self.subscribable_categories:
            return self.subscribable_categories
        seen: list[str] = []
        for decl in self.event_types:
            if decl.category not in seen:
                seen.append(decl.category)
        return tuple(seen)

    def matches_type(self, value: str) -> bool:
        value = (value or "").strip()
        return value == self.type or value in self.aliases

    def patterns_for(self, instance: Any) -> list[str]:
        if self.source_patterns is None:
            return []
        return self.source_patterns(instance)

    def secrets_for(self, instance: Any) -> dict[str, str]:
        if self.webhook_secrets is None:
            return {}
        return self.webhook_secrets(instance)
