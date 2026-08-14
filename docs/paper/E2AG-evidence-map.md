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
| 七类单因素变异下的机制互补性 | `run_mutation_experiment.py` | 固定种子 700 例 `mutation_summary.json` | 可写合成压力测试；禁止外推真实分布 |
| Contract×Policy 独立贡献 | `run_frozen_ablation.py` | 60 例冻结矩阵，四模式 `frozen_ablation_summary.json` | 30/30 正常通过；阻断率 0/33.33/66.67/100%；独立标签复核待完成 |
| 两个 PEP 阻止真实上游副作用 | `run_e2e_chain_experiment.py` | 480 次持久化执行，`e2e_chain_summary.json`/CSV | 确定性 Agent/MCP 替身；可写系统强制链，不可写真实 LLM 红队 |
| 因果阶段定位 | `run_causal_audit_experiment.py` | 5 类故障×20 条 trace | 100/100 定位、链有效、阶段完整；仅显式治理阶段，不是通用根因分析 |
| 相同事件 replay 不产生第二条日志 | `EventDedupClaim` + dispatcher | 修复前后 `concurrency_summary*.json` | 修复前 8 并发 100/100 轮违规；修复后 SQLite 8/32 并发各 100 轮零违规 |
| 审批单次终态 | conditional update | SQLite 8/32 并发各 100 轮 | 零双重终态/重复 Task；禁止外推其他数据库 |
| FastAPI+文件 SQLite 请求延迟 | `run_http_benchmark.py` | `http_benchmark.json`，每模式 300 次 | 三模式差异未超出顺序噪声；不声称负开销 |
| 阻止任意提示词注入 | 无 | 无 | 禁止主张 |
| 覆盖所有工具调用路径 | 仅 remote streamable HTTP task-mode | 无 service/Skill/stdio/SSE 全覆盖 | 禁止主张全覆盖 |
| 强身份 HITL 审批 | Access Token 可选；actor 为客户端声明 | 无独立 IdP/RBAC | 禁止主张强身份或职责分离 |
| 外部不可变审计/不可抵赖 | 无 | 尾截断负结果 | 禁止主张 |
| 跨 AIOS 通用性 | 仅抽象设计 | 无第二系统 | 只能作为设计目标/限制 |
