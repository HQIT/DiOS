# 工作日报 2026-03-05

## 今日完成

### 1. 多智能体协作方案架构决策

围绕"代码开发协作"场景（Chat 下需求 → 开发 → PR → 评审 → 验收 → 通知），完成了核心架构设计与关键概念澄清。

### 2. 关键架构决策

#### 2.1 DiOS 自行实现 A2A 协议子集

- **不直接集成 Google a2a-python SDK**：保持轻量可控
- **不做 dios-mcp 做 A2A 通信**：非标准，不符合定位
- **DiOS 实现的范围**：`message/send`、`tasks/get`、`tasks/cancel`、`Agent Card` 端点，基于 JSON-RPC 2.0
- **Proxy 模式**：Task 型 Agent 通过"每次 send_message 启动一次性容器"实现 A2A 合规

#### 2.2 A2A Layer 与 Event Gateway 关注点分离

| | Event Gateway | A2A Layer |
|---|---------------|-----------|
| 关注点 | 事件接收与路由 | Agent 通信协议 |
| 数据 | EventLog | A2ATask |
| 对外 | Webhook 端点 | JSON-RPC + Agent Card |

`event_dispatcher` 改造为通过 A2A `send_message` 投递，替代直接 `docker run`。

#### 2.3 GitHub-native 工作流

- 代码开发场景以 GitHub Webhook 作为事件驱动骨架
- 利用 Issue / PR / Review 作为 Agent 之间的状态机与协调中心
- DiOS 主要角色：响应 webhook、路由到对应 Agent

### 3. 关键概念澄清

#### 3.1 Task 型 Agent 与事件分发

严格来说 Task 型 Agent **不是"运行中服务接收事件"**，而是"**每次事件触发一个新实例**"。A2A `send_message` 语义恰好兼容此模型——DiOS 作为 Proxy 代为启动容器，Task 状态机独立管理。

#### 3.2 Task Agent vs DiAgent Sub-agent（互补不冲突）

| | OS 级 Task Agent | DiAgent Sub-agent |
|---|------------------|-------------------|
| 定位 | 业务角色（开发/评审） | Agent 内部能力分工（研究员/写作） |
| 容器 | 独立 | 共享主 agent 容器 |
| A2A 身份 | 独立 Agent Card | 仅主 agent 内可见 |

发现 Gap：DiAgent 的 `TaskConfig` 原生支持 `subagents` 字段，但 DiOS Agent 表缺 `subagents` 字段、console 缺配置 UI。列入 Phase 5（后续）。

### 4. Phase 2A 业界调研

调研了 **Hermes Agent**（Nous Research）和 **OpenClaw** 对"外部事件触发的 Agent 响应投递到用户 Chat"的处理：

- **Hermes**：专门的 `gateway/delivery.py` 投递模块，Agent 通过 `send_message` 工具主动投递，支持 direct reply / home channel / explicit target / cross-platform 四种路径
- **OpenClaw**：`/hooks/agent` webhook 端点带 `deliver` + `channel` + `to` 参数显式指定投递目标
- **结论**：业界印证需要显式 delivery 机制，**A2A artifacts（任务产物）与 Chat 消息（用户会话）应分离**

据此定下 Phase 2A 实现方式：底层 API（`POST /api/os/chat/{id}/messages`，`from=agent`）+ DiAgent skill（`send_chat_message`）两层，Agent 自主决定是否投递、投递什么。

### 5. 其他小决策

- **GitHub MCP 配置**：走 UI 手动添加（不写 seed 脚本），顺便验证 MCP 页面 UX
- **Sub-agent UI**：列为 Phase 5 后续处理，不影响今日场景

### 6. 产出：计划文档

定稿的实施计划：`/home/xu/.cursor/plans/多智能体协作方案_c3ce7f4c.plan.md`

完整阶段划分：
1. Phase 1A–1D：A2A 数据模型、JSON-RPC 端点、Proxy、Event Gateway 桥接
2. Phase 2A：Chat Delivery API + skill
3. Phase 2B：UI 配置 GitHub MCP（用户手动）
4. Phase 3：创建 3 个智能体 + 订阅 + prompt
5. Phase 4：端到端验证
6. Phase 5（后续）：Sub-agent 配置 UI

## 待办 / 下一步计划

1. **Phase 1A 开始实施**：`A2ATask` 表、`TaskStatus` 状态机、`Agent.capabilities` 字段扩展
2. **Phase 1B**：`backend/app/api/os/a2a.py` 新 router，JSON-RPC + Agent Card 端点
3. **Phase 1C**：`backend/app/services/a2a_service.py`，service 模式走 HTTP 转发，task 模式走 Proxy
4. **Phase 1D**：改造 `event_dispatcher.py`，通过 A2A 投递
5. **Phase 2–4**：Chat Delivery、创建 Agent、端到端测试
