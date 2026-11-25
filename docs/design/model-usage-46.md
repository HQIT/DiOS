# 模型用量与费用观测

DiOS 将 Agent 报告的用量关联到 Run、Attempt 和模型会话，并保留报告序号、接收时间和原始产物引用。重复上报、离线补报和退出恢复沿用同一观测身份，不重复累计。当前记录的证据等级为 `agent_reported`，财务状态为 `unreconciled`；服务商账单核销与预算预留继续在 #45/#46 建设。

## 数据口径

| 来源 | 保存内容 | 展示与聚合 |
| --- | --- | --- |
| ACP `session/update.usage_update` | 上下文 `used/size`，可选会话累计 `cost` | 每 Attempt 取最后一份快照；上下文占用独立展示 |
| ACP `session/prompt` 响应的 `usage` | 输入、输出、总量及可选思考/缓存 Token | 保留报告原值，标记 `unspecified_acp_scope` |
| 未提供用量 | 无观测记录 | 消耗与费用未知 |
| 数值无效 | `quality=invalid`，指标为空 | 显示报告无效，等待来源修正 |

ACP [会话用量说明](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/protocol/v1/prompt-turn.mdx)定义 `used` 为当前上下文占用，`cost` 为累计会话费用；费用单位既可为币种，也可为 credits。不同会话、Attempt 和费用单位分别展示。金额保留十进制字符串，计量不进行浮点累加或汇率换算。

ACP [Usage 类型](https://agentclientprotocol.github.io/typescript-sdk/types/Usage.html)仍标记实验性，其轮次与会话累计语义存在[公开问题记录](https://github.com/agentclientprotocol/agent-client-protocol/issues/1860)。因此 DiOS 保留输入、输出和总量，不由缓存、思考等可能重叠的字段重新计算总量，也不跨 Attempt 求和。服务商原生响应将按明确的请求身份与计量口径另行接入。后续导出可映射 [OpenTelemetry GenAI 用量属性](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)，并固定所用版本。

## 接入与持久化

SDK 0.1.11 的共享 ACP 客户端为 Cursor、Codex、DeepSeek Harness 等包装器捕获可选用量字段，输出绑定当前 Session 的 `usage` 帧。仅接受白名单指标；`_meta`、提示词、响应正文和未知字段不进入用量记录。无效数值保留无效状态，不阻断正常结果输出。

Worker 将用量按最多 16 条一批写入原有持久上报队列，沿用 Worker/Attempt 所有权、E2AG 当前执行授权和远程身份校验。控制面以 `(scope, attempt_id, sequence)` 幂等保存，同一身份的不同内容拒绝为 `USAGE_CONFLICT`。用量观测不授予执行权，不增加模型请求，也不改变结果验收。

退出时，原始 stdout 中的完整用量帧进入相同账本；退出回执恢复同样可补入遗漏数据并关联产物。数据已存在时仅补充证据引用。保留退出事实不等于续租或恢复执行授权。stdout 恢复解析上限为 16 MiB，不读取 stderr 作为用量，不将不完整 JSON 行作为记录。

新增 `runtime_usage_schema` 与 `runtime_usage_observations`，采用独立增量迁移并纳入备份。用量数据与任务历史、产物一起保留。查询仅展示每个 Attempt、每种用量类型的最新快照，最多 100 项并标记截断；全部已接受观测继续保存在账本。

## API、CLI 与 Console

任务详情的 `model_usage` 包含 `evidence_level`、`settlement`、`items` 和 `at_limit`。每项返回 Attempt/Session、来源类型、序号、接收时间、报告值、质量与证据引用。现有任务读取权限覆盖该字段，应用身份无法读取其他任务的用量。CLI 的任务详情查询同步返回此结构。

Console「模型用量」区分有报告、报告无效与没有报告。长会话标识支持换行，中英文和移动布局沿用全局界面规范。列表中的费用为来源报告值；在未接入账单前，不呈现“已核销费用”。

## 部署与验收

先排空并升级控制面和 Worker，再更新 Harness 的 SDK 镜像。SDK 0.1.11 发出的用量帧需要新版 JSONL bridge；旧版 Worker 会拒绝未知帧。保留旧版 Harness 镜像时，任务仍可运行，用量根据来源能力显示为未知。具体 Harness 是否发送字段必须在其固定版本上验证。

测试覆盖 SQLite/PostgreSQL 幂等及冲突、会话隔离、缺失/无效数据、累计快照、元数据剔除、备份、远程 Worker 实时上报、退出回执恢复，以及真实 ACP 子进程协议。UI 使用独立 HTTP API 和受 E2AG 控制的合成 Worker 验证；没有调用付费模型。证据见[用量验证记录](evidence/model-usage-20261125.json)。

后续接入有明确请求身份的模型原生用量和账单来源，完成预算预留、超时未决、幂等核销与差异处理；继续按 #46 推进治理耗时、Trace、吞吐、新鲜度和诊断能力。
