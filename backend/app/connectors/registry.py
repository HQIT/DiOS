"""Connector 注册表。

内建类型通过扫描 `builtin/` 子包自动注册：新增一种 Connector 只需新增目录并暴露
`MANIFEST`，无需修改本文件或任何通用服务。

场景事件命名空间（由 Agent 自行发布、DiOS 不解析的事件）通过声明文件加载，
默认 `backend/config/event-namespaces.json`，可用 DIOS_EVENT_NAMESPACES_FILE 覆盖。
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import pkgutil
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from app.connectors.contracts import (
    CAPABILITY_WEBHOOK,
    ConnectorManifest,
    EventNamespaceDecl,
    EventTypeDecl,
)

logger = logging.getLogger(__name__)

_BUILTIN_PACKAGE = "app.connectors.builtin"
_DEFAULT_NAMESPACES_FILE = Path(__file__).resolve().parents[2] / "config" / "event-namespaces.json"

_manifests: list[ConnectorManifest] = []
_declared_namespaces: list[EventNamespaceDecl] = []
_loaded = False


def register(manifest: ConnectorManifest) -> None:
    """注册一种 Connector 类型，重复 type 以后者覆盖。"""
    global _manifests
    if not manifest.type or manifest.type.strip() != manifest.type:
        raise ValueError("connector manifest type must be a non-empty canonical identifier")
    Draft202012Validator.check_schema(manifest.config_schema or {"type": "object"})
    _manifests = [m for m in _manifests if m.type != manifest.type]
    _manifests.append(manifest)
    _manifests.sort(key=lambda m: (m.order, m.type))


def _load_builtin() -> None:
    package = importlib.import_module(_BUILTIN_PACKAGE)
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda i: i.name):
        module = importlib.import_module(f"{_BUILTIN_PACKAGE}.{info.name}")
        manifest = getattr(module, "MANIFEST", None)
        if manifest is None:
            logger.warning("connector package %s has no MANIFEST", info.name)
            continue
        register(manifest)


def _namespaces_file() -> Path:
    override = os.getenv("DIOS_EVENT_NAMESPACES_FILE", "").strip()
    return Path(override) if override else _DEFAULT_NAMESPACES_FILE


def _load_declared_namespaces() -> None:
    path = _namespaces_file()
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("failed to read event namespaces file %s", path)
        return
    for item in raw.get("namespaces", []):
        pattern = str(item.get("source_pattern", "")).strip()
        if not pattern:
            continue
        _declared_namespaces.append(
            EventNamespaceDecl(
                source_pattern=pattern,
                event_types=tuple(
                    EventTypeDecl(
                        type=str(t.get("type", "")),
                        description=str(t.get("description", "")),
                    )
                    for t in item.get("event_types", [])
                    if t.get("type")
                ),
            )
        )


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    _load_builtin()
    _load_declared_namespaces()


def all_manifests() -> tuple[ConnectorManifest, ...]:
    _ensure_loaded()
    return tuple(_manifests)


def manifest_for(connector_type: str) -> ConnectorManifest | None:
    """按 type 或历史别名查找 manifest。"""
    _ensure_loaded()
    for manifest in _manifests:
        if manifest.matches_type(connector_type):
            return manifest
    return None


def instantiable_types() -> tuple[str, ...]:
    """可用于创建 Connector 实例的类型。"""
    return tuple(m.type for m in all_manifests() if m.instantiable)


def public_manifest(manifest: ConnectorManifest) -> dict:
    """返回可供 API 和 Console 使用的稳定、无可执行对象 manifest。"""
    schema = deepcopy(manifest.config_schema)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for field_name in manifest.secret_fields:
            field_schema = properties.get(field_name)
            if isinstance(field_schema, dict):
                field_schema["writeOnly"] = True
    return {
        "type": manifest.type,
        "label": manifest.label,
        "description": manifest.description,
        "capabilities": list(manifest.capabilities),
        "aliases": list(manifest.aliases),
        "config_schema": schema,
        "secret_fields": list(manifest.secret_fields),
        "event_sources": [
            {"id": source.id, "name": source.name, "description": source.description}
            for source in manifest.event_sources
        ],
        "event_types": [
            {"type": event.type, "description": event.description}
            for event in manifest.event_types
        ],
        "accepted_source_patterns": list(manifest.accepted_source_patterns),
    }


def instantiable_manifests() -> tuple[dict, ...]:
    """返回所有可创建 Connector 类型的公开 manifest。"""
    return tuple(public_manifest(m) for m in all_manifests() if m.instantiable)


def validate_config(
    manifest: ConnectorManifest,
    config: dict,
    *,
    connector_type: str | None = None,
) -> None:
    """依据 Connector 自己声明的 JSON Schema 校验实例配置。"""
    schema = deepcopy(manifest.config_schema or {"type": "object"})
    if connector_type in manifest.aliases:
        # 历史别名的数据结构可能早于当前必填项，只校验已有字段。
        schema.pop("required", None)
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(config),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    path = ".".join(str(part) for part in error.absolute_path)
    location = f"config.{path}" if path else "config"
    raise ValueError(f"{location}: {error.message}")


def webhook_adapters() -> tuple:
    """按 manifest 顺序返回所有 webhook 适配器，兜底类型排在最后。"""
    adapters: list = []
    for manifest in all_manifests():
        if manifest.has(CAPABILITY_WEBHOOK):
            adapters.extend(manifest.webhook_adapters)
    return tuple(adapters)


def event_namespaces() -> tuple[EventNamespaceDecl, ...]:
    """内建声明 + 声明文件中的 source namespace。"""
    _ensure_loaded()
    declared: list[EventNamespaceDecl] = []
    for manifest in _manifests:
        declared.extend(manifest.event_namespaces)
    declared.extend(_declared_namespaces)
    return tuple(declared)


def event_sources() -> tuple[dict, ...]:
    out: list[dict] = []
    seen: set[str] = set()
    for manifest in all_manifests():
        for source in manifest.event_sources:
            if source.id in seen:
                continue
            seen.add(source.id)
            out.append({"id": source.id, "name": source.name, "description": source.description})
    return tuple(out)


def event_type_decls() -> dict[str, EventTypeDecl]:
    """汇总所有事件类型声明，按 type 去重。"""
    out: dict[str, EventTypeDecl] = {}
    for manifest in all_manifests():
        for decl in manifest.event_types:
            out.setdefault(decl.type, decl)
    for namespace in event_namespaces():
        for decl in namespace.event_types:
            out.setdefault(decl.type, decl)
    return out


def reset_for_tests() -> None:
    global _manifests, _declared_namespaces, _loaded
    _manifests = []
    _declared_namespaces = []
    _loaded = False
