# ADR 0001：Connector 插件契约与注册表

- 状态：已接受
- 日期：2026-08-11
- 相关：ROADMAP §7.1、Phase 0 退出门槛、首批 Issues #6 / #7

## 背景

Connector 负责把外部系统的输入送进 DiOS。此前三种内建类型（`git_webhook`、`imap`、`generic`）的信息散落在多个通用文件里：

- `api/os/connectors.py` 的类型白名单与 `git_webhook` 专属配置校验
- `services/connector_capabilities.py` 按 `type` 的 if/elif 推导 source pattern 与事件类型
- `services/event_normalizer.py` 集中放置三家 Git 平台的解析、签名校验和全量事件目录
- `api/os/events.py` 按 type 聚合 webhook secret、判定 catalog 配置状态
- `frontend` 的 `PRESETS` 与分类型表单

结果是新增一种事件源必须改动 5 个以上通用位置，违反 ROADMAP Phase 0 退出门槛「新增 Connector 不需要修改通用路由中的类型白名单或条件分支」。

同时 `ai4r.*` 事件类型被硬编码进 OS 核心的事件目录，而这些事件实际由 Agent 通过 `publish_event` 自行发布，属于场景配置，违反 ROADMAP §2.5「不在 OS 核心承载客户专属业务流程」。

## 决策

### 1. 每种 Connector 由一份 manifest 声明

`app/connectors/contracts.py` 定义 `ConnectorManifest`，声明类型标识、历史别名、配置 JSON Schema、敏感字段、事件来源分类、事件类型、可订阅 source namespace 推导函数、webhook 适配器与 secret 提取方式。

### 2. 能力模型而非统一接口

以 `capabilities` 声明具备的能力，不要求所有 Connector 都实现同一套方法：

- `webhook`：被动接收 HTTP 回调，提供 `detect` / `normalize` / `verify_signature`
- `poll`：主动轮询外部系统
- `declare_only`：只声明事件命名空间，由外部发布者写入

### 3. 注册表自动发现内建实现

`app/connectors/registry.py` 扫描 `builtin/` 子包并读取各自的 `MANIFEST`。新增一种 Connector 只需新增目录，无需修改注册表或通用服务。`order` 字段决定注册与 webhook `detect` 顺序，兜底类型 `generic` 取大值排在最后。

### 4. 通用服务只依赖注册表

`event_normalizer` 与 `connector_capabilities` 退化为薄委托层，对外函数签名不变。

### 5. 场景事件命名空间用数据声明

由 Agent 自行发布、DiOS 不解析的事件，通过 `backend/config/event-namespaces.json` 声明（可用 `DIOS_EVENT_NAMESPACES_FILE` 覆盖）。`ai4r.*` 从代码迁至该文件；删除条目即卸载对应场景，文件缺失时优雅降级。

## 目录结构

```text
backend/app/connectors/
├── contracts.py
├── events.py
├── registry.py
└── builtin/
    ├── git_webhook/      # webhook：GitHub / GitLab / Gitea
    ├── generic_webhook/  # webhook：兜底
    ├── imap/             # poll
    └── internal/         # declare_only：cron / manual
backend/config/event-namespaces.json
```

## 取舍

- **只做进程内注册表**，不做外部包安装、entry points 或 sidecar。扩展包的分发、签名与准入属于 ROADMAP Phase 4。
- **不改数据库 schema**：`connectors` 表仍是 `type` + `config` JSON，零迁移。
- **保留历史 type 别名**：`github` / `gitlab` / `gitea` 旧数据由 manifest 的 `aliases` 承接，不做数据迁移。
- **入站归 Connector，出站归 MCP**：Connector 暂不实现对外投递能力，避免与 MCP 职责重叠。
- **轮询实现暂不迁移**：`imap` manifest 只声明能力，`services/imap_poller.py` 仍是专用服务。

## 影响

已完成（本 ADR 对应改动）：

- 新增契约、注册表与四个内建声明
- `event_normalizer` / `connector_capabilities` 改为委托注册表，对外行为零变化（事件目录 25 项、source pattern 列表、webhook 解析结果与去重哈希均逐字节一致）
- `ai4r.*` 移出 OS 核心代码

尚未完成（后续接线）：

- `api/os/connectors.py` 白名单改用 `registry.instantiable_types()`，配置校验改用 manifest schema，新增 `GET /connectors/types`
- `api/os/events.py` 的 secret 聚合与 catalog 状态改为遍历 manifest
- `imap_poller` 改为通用轮询运行时，驱动所有具备 `poll` 能力的 manifest
- Console 表单按 manifest schema 动态渲染，移除 `PRESETS`

在上述接线完成前，Phase 0 退出门槛尚未真正达成：新增 Connector 目录虽然能被注册表发现，但仍需在 API 白名单与前端表单处放行。

## 验证

仓库暂无测试目录。本次以「行为快照对比」作为回归手段：改动前后分别导出事件目录、source pattern 列表、代表性 webhook 的标准化结果与去重哈希，逐字节比对一致，并在容器镜像内完成真实导入检查。后续应把该快照固化为契约测试。
