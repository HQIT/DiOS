# E2AG 论文证据映射

本文档用于防止论文主张超出当前实现和实验。每次改论文结论时，应同步检查本表。

## 官方征文范围映射

| 官方方向 | E2AG 对应内容 | 定位 |
|---|---|---|
| 智能体协同安全与调度防护机制 | A2A Task 创建前契约与策略 PEP | 主选题 |
| 系统安全度量与评估方法 | 攻击集、消融、延迟和审计篡改实验 | 核心评估支撑 |
| 多智能体协同场景下的软件安全机制 | 目标 Agent 能力约束与 trace 传播 | 次级覆盖 |
| 软件异常行为检测与根因分析 | 因果证据链；根因定位实验待补 | 次级覆盖，暂不作已完成主张 |
| 系统安全架构设计与形式化验证 | 控制面架构与安全不变量 | 只覆盖架构设计，不声称形式化验证 |

| 主张 | 代码证据 | 测试/数据证据 | 当前可写程度 |
|---|---|---|---|
| source–type 契约可执行且默认拒绝 | `backend/app/services/e2ag.py`, connector manifests | `test_e2ag.py`; `attack_cases.jsonl` A01–A05 | 已实现并有首批消融 |
| 目标 Agent 的 source/type/tool/action allow-list | `evaluate_policy` 与 `capabilities.governance` | A06–A10；相应单测 | 已实现并有构造集证据 |
| 高风险生产动作进入单次审批状态机 | policy + `e2ag_approval.py` | A11–A14；3 个 approval integration tests | 批准/拒绝/过期/单次消费已实现；身份强度依赖 API 门禁 |
| deny/approval 不创建 A2ATask | `event_dispatcher.dispatch_event` | `test_e2ag_dispatcher.py` | 已验证 |
| trace 贯通 EventLog/A2ATask/A2A message/MCP PEP | tables, dispatcher, a2a_service, tool gateway | A2ATask 与 gateway 集成测试 | EventLog/A2A/MCP PEP 已验证；模型/Artifact 尚未贯通 |
| 任务作用域 ToolGrant 绑定 trace/task/agent/server | `e2ag_tool_gateway.py`, `E2AGToolGrant` | grant hash/scope/expiry/revoke tests | 已实现于 event→task-mode remote MCP 路径 |
| MCP 工具调用时强制与发现裁剪 | `api/internal/e2ag_mcp.py` | 7 个 gateway tests；`tool_gateway_summary.json` | streamable HTTP 已验证；其他传输禁止外推 |
| 审计链可检测篡改 | `append_audit_entry`, `verify_audit_chain` | `audit_summary.json` | 6 类链内篡改可检；尾截断不可检 |
| 纯判定开销 | `run_experiment.py` | `results/summary.json` | P50/P95/P99 可写，需注明非端到端 |
| dispatcher+SQLite 控制面增量 | `run_dispatch_benchmark.py` | `results/dispatch_benchmark.json` | 可写为内存 SQLite 微基准 |
| 阻止任意提示词注入 | 无 | 无 | 禁止主张 |
| 覆盖所有工具调用路径 | 仅 remote streamable HTTP task-mode | 无 service/Skill/stdio/SSE 全覆盖 | 禁止主张全覆盖 |
| 强身份 HITL 审批 | Access Token 可选；actor 为客户端声明 | 无独立 IdP/RBAC | 禁止主张强身份或职责分离 |
| 外部不可变审计/不可抵赖 | 无 | 尾截断负结果 | 禁止主张 |
| 跨 AIOS 通用性 | 仅抽象设计 | 无第二系统 | 只能作为设计目标/限制 |
