# #34 工具调用授权与审批

状态：工程验证已完成，产品验收待用户参与（2026-11-13）；未提交、未推送。证据见 [工程验证记录](runtime-engineering-verification-20261113.md)。

## 执行边界

本批增加持久 Runtime 的工具调用出口：已注册 MCP HTTP 工具 → E2AG 完整元组权限交集 → 参数绑定审批 → 派发事务 → 实际 `tools/call`。工具请求使用已有 Run、Attempt、Worker、Session、租约与授权代次。注册配置、主体、策略与凭据由受信服务端解析。

```mermaid
flowchart LR
  R[工具调用申请] --> P[E2AG 权限交集]
  P --> A[参数和执行身份绑定]
  A --> H{需要审批}
  H -->|是| Q[独立审批人]
  H -->|否| I[等待派发]
  Q --> I
  I --> V[复核策略 租约 会话 撤销]
  V --> T[事务消费审批并记录派发]
  T --> M[MCP tools/call]
  M --> E[校验结果与证据归档]
  M --> U[结果未知 保留原调用待对账]
```

L1 `ToolInvocations` 负责单次调用的权限、绑定、状态与证据；`FileToolCatalog` 提供版本固定的执行配置；`McpHttpTransport` 仅在派发事务提交后调用工具。审批人资格通过 Provider 查询，L2 后续接入企业身份及业务审批规则。页面和 CLI 消费同一调用账本。

本批入口沿用受信本地操作员认证，审批另需独立凭据。远端 Worker 身份、mTLS 与令牌兑换继续由 #33 承接。现有 A2ATask 事件网关与新 Runtime 仍各自保留身份；本批没有改写旧任务。ACP 原生工具权限保持拒绝，尚未自动向 ACP 会话注入新出口。将 ACP 权限提示直接映射为批准不足以证明实际原生工具受控，相关接入继续在 #34/#58 完成。

## 授权、审批和派发

E2AG 复用 `evaluate_work_permission`：调用者、服务、工作负载、资源四层均需包含同一个完整 `(action, resource_kind, resource_id, resource_version, constraint_revision)`。工具配置不得扩大已签发工作权限。

工具版本固定为执行配置（传输、URL、工具名、输入/结果 Schema、审批要求及凭据引用）的规范 JSON SHA256，排除 permission、enabled 字段。注册配置中 permission.resource_version 必须与摘要一致，工作策略再明确允许该版本。地址或审批要求变化会使原授权失效。每次调用另绑定完整注册摘要、参数摘要、策略/权限摘要与原执行身份。

审批有效期最多 600 秒，并受工作与执行授权剩余时间限制。续期不延长已创建的动作审批。审批人由独立令牌哈希映射得到，客户端不能提供 actor；同一工作主体及服务 actor 不能批准自己的请求。审批人资格、策略、工具版本、会话、租约和撤销状态在派发前重新检查。

`(scope, Run, Attempt, request_key)` 是幂等身份。相同输入返回原调用；参数或会话变化产生冲突。审批仅关联一个调用。审批通过后进入 prepared，派发事务原子消费审批、记录审计并置 dispatched。并发请求只能取得一次派发资格。撤销和派发使用同一 Runtime 作用域事务序列，先提交的撤销阻止后续派发；已提交派发的请求可能已产生外部效果，其结果保留为事实，不将 Run 改为成功。

| 状态 | 行为 |
| --- | --- |
| awaiting_approval | 等待有资格的独立审批人；不调用工具 |
| prepared | 已具备审批依据；派发时仍重新核验 |
| dispatched | 已提交调用意图；同一调用不再派发 |
| succeeded / failed | 校验后的工具返回；任务结果另行判定 |
| result_unknown | 网络中断、格式不完整、结果不合格或派发后失去观测；保留身份及可取得的输出 |
| denied | 审批拒绝，保留理由 |

超过派发观测期限可登记 result_unknown，超时本身不证明上游停止。状态查询和重复派发请求都不会重发已派发调用。存在 dispatched/result_unknown 时，Run 成功归档及再次入队被阻止。上游业务结果查询、独立校验器与人工证据对账继续实现；本批没有强制置成功的接口。

## MCP 与凭据

传输支持 MCP 2025-06-18 的 initialize、initialized、tools/call 与 JSON/SSE 响应。SSE 使用增量 UTF-8 解码，响应 ID 校验和 1 MiB 总量限制。初始化最多 30 秒，工具调用最多 60 秒；重定向和自动重发关闭。服务端反向请求、其他协议版本和 stdio 本批返回明确的不支持原因。协议依据：[生命周期](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)、[Streamable HTTP](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)。

工具地址由操作员注册，调用请求不能指定 URL、请求头或进程命令。可选 credential_ref 必须已包含在工作授权凭据引用中，经现有 Secret Provider 验证作用域、profile、版本与内容摘要后生成上游 Bearer 头。配置变化在每次派发前复核；配置文件更新与本地文件挂载仍依赖受信执行环境。

原始结果与可取得的部分响应写入 Artifact Provider，参数与结果只向已认证操作员提供。终态和结果引用同事务提交；若持久化故障发生在派发后，调用保持待对账。本地完整/部分回执及恢复已接线，跨主机结果恢复仍需 Provider 接线。企业网络隔离、原生工具、组织 ACL 和远端发送方身份尚待联合验收。

## 操作入口

启用配置：`DIOS_RUNTIME_TOOLS_FILE`、`DIOS_RUNTIME_APPROVERS_FILE`，以及既有工作策略、数据库、产物与可选凭据 Provider 配置。默认均为空。示例工具默认停用，示例哈希为占位符。

HTTP 入口在 `/api/os/runtime/runs/{run_id}/tools`：POST 创建请求，GET 查看调用；`/{id}` 查看证据，`/{id}/approval` 批准/拒绝，`/{id}/dispatch` 派发，`/{id}/reconcile` 将观测超期的派发登记为待对账。写操作使用调用自身的 expected_version，审批同时要求 `X-DiOS-Approver-Token`。这些是当前本地受信入口，远端契约端点保持原规划。

CLI：`dios task tool-request`、`tool-list`、`tool-show`、`tool-approval`、`tool-dispatch`、`tool-reconcile`、`tool-recover`。请求参数从 JSON 文件读取；审批凭据从 `DIOS_APPROVER_TOKEN` 读取，操作员凭据仍使用 `DIOS_ACCESS_TOKEN`。

Console 的任务详情增加“工具调用与审批”：参数核对、独立审批凭据、批准/拒绝、派发、过期和结果待对账状态。批准与派发分开，网络异常后通过原调用查询。审批凭据仅在当前输入中短暂保留，操作后清空。

## 持久化与验收

新增独立 tool-schema v1，包含调用/审批绑定及调用证据表，显式迁移并纳入 SQLite 备份检查。原 runtime v1 与 work-auth v1 保持独立。

回归场景：

| 场景 | 验收条件 |
| --- | --- |
| 未审批、自审批、越权工具、旧会话 | 实际 tools/call 次数为 0 |
| 审批过期、策略/工具/凭据变化、审批人停用 | 派发被拒绝并可追溯原因 |
| 重复请求、并发派发、参数变化 | 原调用复用，工具至多派发一次，参数冲突被拒绝 |
| 撤销发生于 MCP 初始化期间 | 派发事务复核后拒绝实际调用 |
| 派发审计写入失败 | 审批消费和派发状态回滚，工具调用次数为 0 |
| 超时、断网、派发进程退出 | 结果未知；查询与重复请求不重新执行 |
| 未决工具结果与任务成功/重试 | 拒绝提前计成功或重新执行 |
| UTF-8 跨字节、JSON/SSE、错误 ID、超大响应 | 中文完整，错误响应不计成功 |
| API、CLI、页面及备份恢复 | 同一身份、版本、审批与证据；恢复副本保持调度关闭 |

自动化用例已运行通过。#34 保持 In Progress；Worker 受限工具通道、ACP 客户端控制和本地回执已形成待测代码；跨主机 Provider 继续推进，#58 负责真实端点联合验收。

#34 Worker 接线开发稿：[独立工具通道与 ACP 控制点](worker-tool-channel-34.md)。本地通道、会话绑定、审批后自动派发和受控只读客户端操作已形成代码，工程测试已通过，产品验收待用户参与；原生工具旁路资格及跨主机 Provider 继续推进；本地回执接线见下文。

#30/#34 [工具结果回执与恢复对账](tool-receipts-34.md)：完整/部分响应先持久化，恢复核对原身份、契约和 E2AG 派发证据；Worker 周期维护、Console/API/CLI、排空与回执备份同步接线。代码和回归用例待统一测试验收，下一步衔接 #33 跨主机身份与通道 Provider，#58 验证真实端点控制能力。

工程构建、自动化测试和开发部署以 [2026-11-13 验证记录](runtime-engineering-verification-20261113.md) 为当前依据；产品验收和真实第三方端点资格分别跟踪。
