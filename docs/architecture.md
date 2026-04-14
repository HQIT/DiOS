# NANA-OS 架构

> NANA = Network Attached Native Agent

## 分层架构

```
App 层    ──  Chat App / Console App / CLI / Pipeline / ...
               │ (通过 OS API 创建、调用 Agent)
               ▼
Agent 层  ──  DiAgent 实例 (常驻 / 一次性)
               │ (从 OS 获取资源)
               ▼
OS 层     ──  NANA-OS
               ├── 调度：Agent 生命周期管理、事件路由
               ├── 资源：LLM 端点、MCP Server、Skills
               ├── 运行时：容器隔离、执行环境（常驻服务 + 一次性任务）
               └── 基础设施：存储、日志、权限、配额
```

## 各层职责

### OS 层（NANA-OS）

管资源、管调度、不干活。类比操作系统内核。

- **调度**：Agent 生命周期（创建、启动、停止、销毁）、事件路由（外部事件 → 匹配 Agent）
- **资源管理**：LLM 端点池、MCP Server、Skills —— 统一注册，按需分配给 Agent
- **运行时**：为 Agent 提供隔离的容器执行环境
  - **常驻服务**（Agent Runtime Manager）：按需启动 DiAgent HTTP 服务容器，供 App 层实时交互
  - **一次性任务**（Docker Runner）：事件触发后 spawn 容器执行单次任务
- **基础设施**：持久化存储、执行日志、权限控制、资源配额
- **API**：统一对外暴露 `/api/os/*`，供所有 App 调用

### Agent 层（DiAgent）

跑在 OS 之上的一等公民，实际干活的执行单元。类比操作系统中的进程。

两种模式（通过 `mode` 字段区分）：

- **`service`（常驻型）**：长期运行的 HTTP 服务，提供 OpenAI 兼容的 `/v1/chat/completions` 接口，支持多轮会话。Chat App 只列出此类 Agent。
- **`task`（一次性）**：执行单次任务后结束生命周期，边界清晰、可重试、可并行。由事件触发，不支持交互式聊天。

Agent 通过 OS 获取所需资源（LLM、Skills、MCP Tools），不自行管理这些资源。

### App 层

面向用户的交互入口。类比操作系统中的 shell / GUI。

App 通过 OS API（`/api/os/*`）操作 Agent 和 OS 资源，App 特有的后端逻辑通过 `/api/apps/{app}/*` 暴露。OS 不绑定任何特定交互形态。

内置 App：

- **Console App**：OS 管理界面（Agent、Model、Connector、MCP、Event 等管理）
- **Chat App**：与 Agent 的对话式交互界面，支持 SSE 流式响应

可扩展 App（未来）：

- CLI、Slack Bot、Pipeline 编排器、监控仪表盘等

## 工程结构

```
backend/
  main.py
  app/
    api/
      os/                # OS 级 API（Agent/Model/Event/MCP 等管理）
        agents.py
        models.py
        events.py
        ...
      apps/              # App 级 API（各 App 的后端逻辑）
        chat.py          # Chat App: proxy to DiAgent, SSE streaming
    services/
      docker_runner.py   # 一次性任务容器管理
      agent_runtime.py   # 常驻 DiAgent 服务实例管理
      ...

frontend/
  src/
    App.tsx              # App Shell（仅负责 App 切换）
    api/
      os.ts              # OS API client
      chat.ts            # Chat App API client
    apps/
      console/           # Console App（现有管理页面）
      chat/              # Chat App（聊天界面）
    components/          # 公共组件
```

## 操作系统类比

| OS 概念 | NANA-OS 对应 |
|---------|-------------|
| 内核 + 系统调用 | NANA-OS 核心（调度 + 资源管理），API: `/api/os/*` |
| CPU / GPU | LLM 端点 |
| 文件系统、网络、设备 | Skills、MCP Server |
| 进程 | Agent（DiAgent 实例） |
| Shell / GUI | App（Chat App、Console App 等） |

## Agent Runtime Manager

OS 层核心服务，为每个 `mode=service` 的 Agent 按需启动独立的 DiAgent 容器。

**核心流程：**

1. Chat App 发起聊天请求
2. Chat API 调用 `ensure_running(agent_id)` —— 如果容器已 running 直接返回 URL，否则启动新容器
3. Runtime Manager 为 Agent 生成 `models.yaml`（从 OS 的 LLM 模型库同步）、`mcp_servers.json`
4. 通过 Docker SDK 启动独立 DiAgent 容器，注入环境变量（`AGENT_SYSTEM_PROMPT`、配置路径等）
5. 等待健康检查通过后记录到 `agent_runtimes` 表
6. 每个 Agent 有独立容器，互不干扰

**数据表 `agent_runtimes`：** `agent_id` (PK) / `container_id` / `url` / `status` / `started_at`

**配置：** `NANAOS_DIAGENT_SERVICE_IMAGE` 指定服务模式镜像（默认 `nana-os-diagent:latest`）

**Docker Compose：** 固定的 `diagent` 服务已移除，改为 `diagent-build` profile 仅构建镜像，Runtime Manager 动态管理容器。

## Chat App 消息持久化

Chat App 管理自己的会话和消息历史，DiAgent 保持无状态。

**数据表：**

- `chat_sessions`：`id` / `agent_id` / `title` / `created_at` / `updated_at`
- `chat_messages`：`id` / `session_id` / `role` / `content` / `created_at`

**API 流程：**

1. 前端只发当前 user message + `session_id`（可选）
2. 后端从 DB 加载历史消息，拼接 system_prompt + history + user message
3. 通过 Runtime Manager 获取 DiAgent URL，转发完整 messages
4. 流式返回的同时收集 assistant 回复，完成后存入 DB
5. Response Header `X-Session-Id` 返回 session ID（新建时前端据此绑定）

**前端交互：**

- 左侧 sidebar：Agent 列表 + 选中 Agent 的会话列表
- 切换会话时从 API 加载历史消息回显
- 新建会话：首次发消息时自动创建
- 支持删除会话

## 关键设计决策

- **会话状态**：放在 App 层，Chat App 自行管理 session/message 持久化，每次给 DiAgent 的是完整 messages 列表。后续如需多 App 共享会话，再下沉到 OS 层。
- **Agent 运行时隔离**：每个 service Agent 有独立的 DiAgent 容器，通过 Runtime Manager 按需启动和管理，互不干扰。
- **Agent 运行时统一**：常驻和一次性 Agent 底层使用同一套 DiAgent 运行时，区别仅在调度策略（长驻 worker vs 单次 job）。
- **App 可扩展性**：Backend 通过 `/api/apps/{app}/*` 为每个 App 提供独立命名空间；Frontend 通过 `apps/` 目录约定，每个 App 是独立模块，App Shell 只负责切换。新增 App 只需加一个 backend router + 一个 frontend 目录，不改 OS 核心。
