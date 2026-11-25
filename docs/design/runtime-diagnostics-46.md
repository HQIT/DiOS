# 执行阶段与治理耗时

任务详情提供阶段观测、最近事件、最近有效输出、观测新鲜度，以及 E2AG 授权和 OPA 请求的实测耗时。API、CLI `runtime show` 与 Console 共用查询口径，应用身份沿用任务读取权限；工具诊断另需 `tool.read`，仅共享任务读取不会共享工具审计。

## 时间与证据

| 数据 | 时钟与来源 | 解释 |
| --- | --- | --- |
| 执行阶段 | 数据库接收 `attempt.phase` 的时间 | 准备、创建会话、等待模型、输出和校验的接收区间；包含传输和离线补报延迟 |
| 已领取阶段 | Attempt 的 `started_at` | 领取到第一份阶段报告的区间 |
| 授权检查点 | 服务进程 `perf_counter` | 授权读取、策略刷新、事务等待、授权校验与 Grant 更新、审计前总耗时 |
| OPA 请求 | 同一服务进程的单调时钟 | 适配器调用总耗时，包含传输、服务端求值、读取与响应验证 |
| 最近有效输出 | 已接受输出观测 | 与控制事件和进程观测分别展示；不把心跳计作模型输出 |

[OpenTelemetry Trace](https://opentelemetry.io/docs/concepts/signals/traces/)区分操作区间、时间戳事件和关联上下文；[指标命名约定](https://opentelemetry.io/docs/specs/semconv/general/metrics/)为后续标准导出提供口径。当前接口返回秒单位的持久观测，OTLP 导出、跨进程 Trace Context 与完整父子 Span 仍待接入。

同一 Attempt 的重复阶段报告不重置阶段起点。相邻阶段按持久事件版本确定先后；结束时间取下一阶段起点或 Attempt 结束事实。尚未闭合的阶段返回 `duration_seconds=null`，另给出 `elapsed_seconds`，表示距接收起点的时间。过期观测继续标为 stale，查询不会确认进程死亡、取消任务或重新派发。

数据库时间回拨导致区间为负时，返回 `clock_anomaly=true` 和未知耗时。阶段区间不按 Worker 时钟与服务器时钟相减。等待、检索、审批可能发生在同一 Harness 阶段中，阶段耗时不推断模型内部首 Token 延迟、纯生成时间或检索时间。

## E2AG 检查点

执行 Grant 创建、会话绑定、续期、结果接收和持久拒绝沿用既有审计行保存 `evidence.timing`。正常心跳没有新增授权审计时，也不额外创建计时行。计时与原审计事务共同提交，审计失败继续阻止相应授权变更。

授权检查耗时包括事务内校验、数据库访问和 Grant 更新；`before_audit_seconds` 是四个阶段之和。审计写入与事务提交没有包含在内，`coverage.persistence_commit_seconds=null` 明确标记缺口。该值不用于证明纯策略求值耗时或数据库持久化耗时。

OPA 成功、拒绝、不可用和超时分别保留耗时与原错误码。请求在建立 Invocation 之前被拒绝时，审计中的 Run/Attempt 关联仍能检索；不会创建虚假的已执行调用。审批、E2AG 复核、回执和幂等派发规则保持原有语义。

历史记录没有计时证据时返回 `timing=null`。查询只输出白名单数值、检查点身份和原因码；提示词、工具参数、凭据和完整策略输入不进入诊断视图。

## 接口与开销

任务详情新增 `diagnostics`，包括 `time_basis`、`observed_at`、`last_event_at`、`last_output_at`、`freshness`、`stages`、`governance` 和覆盖说明。阶段最多显示最近 20 次尝试，治理记录最多 100 条，分别标记更多记录。Console 提供可展开的中英文诊断区和移动布局。

数据复用 `runtime_events`、Attempt 和既有工作/工具审计表，无新增 schema。阶段在数据库内按任务聚合；长时间运行任务的历史扫描成本需随容量压测评估。内部 `RuntimeStore.get` 默认不查询诊断，CLI 显式请求诊断，避免执行、续租和工具配置路径承担时间线查询开销。

## 验证与后续

[验证记录](evidence/runtime-diagnostics-20261125.json)覆盖 SQLite/PostgreSQL、重复报告、时钟回拨、未闭合阶段、输出与观测分离、允许/拒绝计时、历史未知值、OPA 超时和请求前拒绝关联，以及对象权限。真实 OPA 容器与 MCP HTTP 检索验证策略版本变化、服务中断、超时和重复派发。API、CLI、中文/英文桌面和手机页面同步验证。

后续建设持久小时/日吞吐、调度器与 Worker 新鲜度诊断、治理转发和提交耗时、OTLP 导出及告警。Agent 内部阶段须由来源提供明确事件或原生观测；物理跨主机时钟、网络和容量验收继续保留。
