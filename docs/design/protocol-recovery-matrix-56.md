# ACP / A2A 恢复能力矩阵

DiOS 根据固定协议版本和实际协商能力恢复原会话或查询原任务。执行权仍由当前工作授权、原 Run/Attempt 绑定和所有权校验决定。历史重放、继续观测、重新执行分别处理。

## 已接入路径

| 接入路径 | 可用恢复方式 | 原标识与结果处理 | 未支持或受限项 |
| --- | --- | --- | --- |
| Cursor CLI `2026.11.05-86047df` / ACP v1 | 声明 `loadSession`，独立客户端以 `session/load` 检查原会话 | 保留 Session ID；完成场景返回原问答，中断场景可能没有历史 | 未验证原 prompt 续接；不自动发送新 prompt |
| DSH `0.2.2-rc.5` / ACP v1 | 声明 `sessionCapabilities.resume`，以 `session/resume` 检查原会话 | 原 Session ID 可恢复；实测历史通知为零 | 未声明 load；resume 不证明历史重放或原 prompt 继续执行 |
| Codex ACP `2.11.0` / CLI `0.172.0` / ACP v1 | 声明 load 与 resume；检查优先使用 load | 保留 Session ID；中断场景重放原提问但没有最终答案 | 中断后的历史不作为完整成功结果 |
| DiOS Runtime A2A `0.3.0` JSON-RPC 入站 | 原 Task 的 `tasks/get` 与 `tasks/cancel`；重复 `messageId` 保持原任务 | 认证主体、profile、Context 和 Run 绑定；取消等待退出证据 | streaming、resubscribe、任务续轮与阻塞发送未准入，返回 `-32004` |
| A2A `0.3.0` JSON-RPC 出站 Worker | 持久保存远端 Task/Context；重连后查询原 Task，优先恢复完整响应回执 | 保留原 Attempt；成果校验及当前 E2AG `accept_result` 通过后归档 | 已发送且远端 ID 未知时保持待对账，不再次 `message/send` |
| 既有 DiAgent / A2A 兼容服务 | 按既有托管执行对象和原进程身份恢复，由兼容恢复服务处理 | 保留兼容任务与原生执行对象关联 | 单独登记为 legacy；不沿用 Runtime A2A 0.3 的符合性结论 |

当前 SDK 为 `0.1.11`，ACP 依赖固定 `agent-client-protocol==1.0.0`，A2A 依赖固定 `a2a-sdk==0.3.26`。Harness 版本以表中已实测组合为准。

## 恢复与授权约束

ACP 检查前必须完成 initialize，并核对 load/resume 声明。无支持的方法返回 `ACP_SESSION_RESTORE_UNSUPPORTED`；协议版本不符返回 `ACP_VERSION_UNSUPPORTED`，原 Session ID 改变返回 `SESSION_CHANGED`。检查客户端禁止 prompt、新会话、工具授权、文件读取和终端创建，固定传递空 MCP 列表。

历史中的旧答案、工具记录和用量记录保存在有界检查结果中，不写成当前执行输出、当前工具成功或当前用量。检查超时保留部分记录，后到通知继续按历史处理；同一检查客户端不重试恢复请求。ACP v1 的 load/resume 成功不授予原 prompt 的活动续接权。

A2A 查询超时、限流或额度故障进入持久冷却与单路试恢复。Worker 重启后继续处理同一远端 Task。取消请求只有得到远端终态才能结清；当前工作撤权阻止接受成功结果。配置、身份或任务绑定变化使原恢复操作被拒绝。缺少远端 ID 返回 `A2A_REMOTE_ID_UNKNOWN` 并保持待对账。

## 验收对应

| K01-4.01 完成条件 | 代码与实际依据 |
| --- | --- |
| 按已验证协议恢复 | `AcpSessionInspector` 能力协商；A2A Worker 原任务查询及回执对账；三种 Harness 跨进程实测、真实 HTTP/独立 Worker 恢复 |
| DSH 无历史重放等差异明确 | SDK 分别识别 load/resume；DSH 实测原会话恢复且历史通知为零 |
| 未支持返回稳定错误 | SDK 不支持方法不发送 RPC；A2A 不支持的入站方法返回协议错误且不入队 |
| 未知动作不自动重放 | ACP 检查不发送 prompt；A2A 提交响应丢失无 Task ID 时保持一次提交；查询超时恢复原 Task |
| E2AG 与原身份保持 | 撤权后拒收结果、取消确认、配置变化拒绝；历史通知不构成新的执行证据 |

工程证据：[本项收口](evidence/protocol-recovery-closure-20261125.json)、[三种 Harness 会话实测](evidence/acp-sessions-20261125.json)、[A2A 入站](evidence/a2a-runtime-20261125.json)、[出站恢复](evidence/a2a-outbound-20261125.json)、[故障冷却恢复](evidence/a2a-recovery-20261125.json)、[未派发预检恢复](evidence/a2a-preflight-20261125.json)。物理跨主机、供应商内部执行终止及独立安全评审仍由对应工作项承接。

协议依据：[ACP v1 Session Setup](https://agentclientprotocol.com/protocol/v1/session-setup) 分别定义带历史 load 与不重放历史的 resume；[A2A 0.3 规范](https://a2a-protocol.org/v0.3.0/specification/) 定义 Task 查询和取消。DiOS 使用这些既有协议语义；未加入自定义活动请求续接协议。
