# 受控工具派发预算

每个 Run 可以限制经 DiOS 工具网关提交的工具调用次数。预算与 E2AG 的 Work、会话、审批、策略及撤权检查共同生效，适用于本地与远程 Worker 工具通道以及 ACP 的受控 MCP 代理。

## 配置与绑定

共享额度管理接口的 `budget.max_tool_calls` 为单任务工具派发上限，范围为 0–100000，null 表示不限制。0 允许任务运行，但禁止工具派发。控制域、主体、运行配置组沿用现有选择器；多条匹配规则取最小值。

```json
{
  "id": "review-tools",
  "selector": {"kind": "profiles", "profiles": ["review"]},
  "max_concurrent": 3,
  "budget": {"max_tool_calls": 20},
  "expected_revision": 0
}
```

API `PUT /api/os/runtime/capacity`、CLI `dios runtime capacity set/status` 与 Console 使用相同配置。修改接口要求操作员身份、当前 revision，并保留变更审计。省略整个 `budget` 保留配置；传入 budget 时按完整预算对象替换，未指定的可选字段清空。修改其他预算时应带上需要保留的字段。

Run 首次领取时绑定上限及配置 revision。重试、进程重启、增加上限、停用配置均不改变已有绑定。升级前尚无绑定的活动 Run 在首次受控派发时绑定，并计入原有已提交调用。父子 Run 分别绑定；跨父子树的共享调用预算仍需独立设计。

## 派发与用量

| 阶段 | 计量规则 |
| --- | --- |
| 请求、等待审批、审批通过 | 不扣减 |
| 会话、授权、策略、撤权检查拒绝 | 不扣减 |
| 最终派发提交 | 在同一事务内检查总量、消费审批并记录派发与预算证据 |
| 调用成功或工具返回错误 | 保留一次用量，分别计入成功或失败 |
| 超时、进程中断、结果无效或未知 | 保留一次用量，进入待确认 |
| 同一调用重试查询、回执恢复 | 沿用原调用身份，不增加用量 |
| 派发提交前事务失败 | 审批与派发更新一起回滚，不扣减 |

计量单位为 `dispatch_admission`。`runtime_tool_invocations.dispatch_deadline` 在派发提交时写入，后续回执和状态变更保留该字段。统计使用这一持久事实，避免维护可与调用账本脱节的独立计数器。任务终止时关闭的未派发调用没有该标记，不计入用量。

派发事务提交后、上游请求发出前发生进程故障，也保留一次已提交用量。该状态的外部执行结果可能未知，继续走原有对账流程。派发次数不等于成功次数、模型 HTTP 请求数、Token 或费用。Agent 自行调用的外部服务只在经过受控网关时受此限制；远端原生能力按适配器声明和现有隔离策略约束。

准入检查与记录必须原子完成，重试使用稳定请求身份；参考 [AWS 幂等 API 实践](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)。集中门禁与用量观测参考 [Envoy 全局限流](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/rate_limit_filter.html)。DiOS 复用现有事务与工具账本，不新增独立限流服务。

## 查询与界面

任务详情的 `tool_budget` 返回 `maximum`、`committed`、`succeeded`、`failed`、`unresolved`、`remaining` 和 `exhausted`。任务所有者只获得自己可读任务的用量；匹配规则标识和其他主体的用量不进入任务响应。`remaining` 为 null 表示无上限。

Console 提供中英文预算编辑、清除与任务用量展示。额度耗尽时工具派发返回 `TOOL_BUDGET_EXHAUSTED`，原审批保留，未向上游发出工具请求；管理员可取消任务或按新配置创建任务。原 Run 的固定预算不因配置修改而增加。

## 升级与验证

新增独立的 `runtime_tool_budget_schema`、配置表和 Run 绑定表，纳入显式迁移与备份校验。升级采用停止领取、排空、备份、迁移和切换全部执行器的现有流程。旧版本不具备该门禁，启用预算前必须完成执行器切换。历史调用账本与预算绑定需一并保留。

`backend/tests/test_runtime_tool_budgets.py` 覆盖 SQLite/PostgreSQL 独立连接竞争、E2AG 拒绝不扣减、审计失败回滚、重复请求、未知结果、原调用回执恢复、跨 Attempt 继承和备份。真实 MCP HTTP 测试以六个独立连接竞争两次额度，核对上游收到的调用；Unix 工具通道验证 ACP 代理转交后同样经过门禁。测试使用合成工具，无模型调用。

实测结果见[工具预算验证记录](evidence/tool-dispatch-budgets-20261125.json)。后续继续模型请求预算、Token/金额预留与可靠核销、父子树共享预算，以及物理跨主机验收。
