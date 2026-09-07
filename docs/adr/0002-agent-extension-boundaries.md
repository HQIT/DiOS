# ADR 0002：Agent 扩展边界与开放协议映射

- 状态：已接受
- 日期：2026-09-07
- 相关：ROADMAP §2、§3、§7，ADR 0001，E2AG 工具授权

## 背景

“Agent 插件”可能同时指工具、外部事件接入、Agent 服务、提示词包、运行时适配器或交互界面。把这些对象都作为可复制进 Agent 工作目录的代码，会让业务协议进入 OS 核心，也会绕过注册、授权、审计和生命周期管理。

行业已有分层协议，但没有一个成熟规范覆盖插件安装、运行、权限、升级和卸载全过程：

- MCP 规范工具、资源和 Prompt 的发现与调用；DiOS 以稳定版 `2025-11-25` 为兼容基线。
- A2A `1.0` 规范独立 Agent 的发现、任务和结果交换。
- Agent Skills 规范指令与资源目录，不作为运行时安全边界。
- Open Plugins 提供组合打包格式，当前仅作为兼容方向，不作为 DiOS 核心依赖。

## 决策

### 1. 不建立含义模糊的统一 Plugin 运行时

DiOS 按职责管理五类扩展对象：

| 对象 | 责任 | 对外契约 |
|---|---|---|
| Connector | 将外部输入标准化为平台事件 | Connector Manifest + CloudEvents |
| MCPServer | 向 Agent 暴露工具、资源和 Prompt | MCP |
| AgentRuntime | 提供独立 Agent 服务与任务执行 | A2A |
| SkillPackage | 提供可移植的操作指令和附属资源 | Agent Skills |
| RuntimeAdapter | 部署、启动、停止和观察工作负载 | DiOS 内部 Adapter 契约 |

交互式工具界面后续可采用 MCP Apps，但不进入当前单机 Phase 0 的实现范围。

### 2. OS 管理能力，业务实现留在扩展中

DiOS 核心只保存扩展的身份、版本、配置 Schema、入口、状态和权限声明。群聊、文件上传、代码仓库或客户流程等语义由 Connector、MCP Server、Skill 或业务 App 承担。

禁止以下集成方式：

- 把调用方私有字段无契约透传到 Agent Runtime。
- 把外部代码目录自动挂载或复制给所有 Agent。
- 仅凭目录存在即发现并执行代码。
- 由业务 App 修改 OS 通用路由才能注册一种扩展。

### 3. 所有可执行能力先注册，再授权

扩展进入数据面前必须经过：

```text
发现 → manifest 校验 → 注册 → 显式启用 → 按 Agent/任务授权 → 调用 → 审计
```

MCP 工具通过 E2AG 按任务签发短期授权并过滤 `tools/list` / `tools/call`。Skill 内容不能扩大工具权限。Connector 只能产生 manifest 声明的事件来源和类型。Runtime Adapter 只能注入控制面明确分配的资源。

### 4. 当前只实现进程内 Connector 注册表

Phase 0 使用仓库内置 Connector 包验证契约：每种类型独立提供 manifest、配置 JSON Schema、能力、事件声明和 Adapter。通用 API、事件目录和 Secret 聚合只读取注册表。

外部包安装、签名、供应链准入和跨仓库分发放在 Phase 3。未来引入安装清单时，优先兼容 Open Plugins 的身份和组件组织方式，DiOS 扩展字段使用独立命名空间。

## 结果

- 新增 Connector 不修改通用 CRUD、事件目录或 Webhook Secret 逻辑。
- MCP、A2A、Skill 和 Connector 保持独立生命周期，不用私有目录约定互相穿透。
- DiOS 的差异化能力集中在注册、部署、授权、恢复和审计，而不是重新定义工具调用协议。
- 单机模式仍执行最小权限；“单一可信操作者”不等于“所有 Agent 相互信任”。

## 验证

- 契约测试动态注册一个测试 Connector，并通过未修改的通用 API 创建实例。
- 非法类型、非实例化类型和不符合 JSON Schema 的配置必须被 API 拒绝。
- API 暴露的 manifest 不包含 Python callable，敏感字段只暴露名称和 `writeOnly` 标记。
- 事件目录状态和 Webhook Secret 聚合不包含具体 Connector 类型判断。

## 参考

- MCP：<https://modelcontextprotocol.io/specification/2025-11-25>
- A2A：<https://a2a-protocol.org/v1.0.0/specification>
- Agent Skills：<https://agentskills.io/specification>
- Open Plugins：<https://open-plugins.com/>
