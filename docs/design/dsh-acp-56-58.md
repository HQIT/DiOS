# DeepSeek Harness 接入与 E2AG 执行资格

同一个独立静态评审应用已通过 Cursor 和 DeepSeek Harness 两种 ACP Harness 执行。DSH 使用 ECNU Max 的 OpenAI 兼容服务；模型、Harness、业务应用和 DiOS 治理分别配置。SDK 0.1.3 的 DSH 适配只转换工具元数据，执行授权沿用 Runtime/E2AG。

## 官方接口与分层

固定发布包为 `@deepseek-ai/dsh@0.2.2-rc.5`，Node 24，ACP 官方 Python SDK `agent-client-protocol==1.0.0`。依据官方 [ACP 接口](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/acp/acp)和 [pi-ai 模型适配器](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/llm/llm-pi-ai)，并以已安装发布包的实际行为验证。

| 组件 | 职责 |
| --- | --- |
| DSH ACP / 官方 pi-ai 适配器 | 模型循环、模型路由、会话持久化、工具意图与权限回调 |
| DiOS SDK | 会话绑定、有界协议传输、DSH 工具名称/参数转换、取消与关闭、结果收集 |
| Runtime / E2AG | 任务、尝试、Worker、进程证据、版本化凭据、审批、真实工具派发、撤销与产物验收 |
| 独立 static-review 应用 | 合成源码输入、证据与行号校验、报告和业务复核状态 |

`session/request_permission` 可能只携带工具调用 ID。SDK 从同一会话的先前工具通知补齐 `title` 和 `rawInput`，仅将 `mcp__dios__*` 映射给既有受控工具代理。代理仍检查注册别名、参数、工作授权和审批；ACP 许可仅允许转交代理。

## 运行配置

[应用示例](../../applications/static-review/examples/dsh/README.md)提供 ACP 配置与官方 plugin patch。DSH 原生文件、终端、联网、插件管理、skills、自主任务和子智能体工具关闭；模型凭据通过现有 Secret 引用注入。容器以非 root、只读根文件系统和资源限额运行，工具检索由 Worker 调用注册 MCP 服务。

原生模块加载器提供的 `NARB_DISABLE_NATIVE_CACHE=1` 允许直接使用已安装动态库，保留 `/tmp` 的 `noexec`。ECNU Max 配置从已有开发服务引用取得；直连探测返回 HTTP 200，本次网关响应的模型标识为 `deepseek-flash`。企业网络准入仍需固定服务域名、网络出口和凭据生命周期。

## 实测结果

证据：[固定镜像、Run/Attempt/Session、退出回执与测试记录](evidence/dsh-acp-20261124.json)。开发资格部署使用独立数据库、合成源码、独立 Worker/容器和注册检索工具。

| 场景 | 结果 |
| --- | --- |
| 模型连通 | DSH 通过 ECNU Max 返回预期文本，`end_turn`，退出码 0 |
| 审批通过 | 审批前检索 0 次，通过后 1 次；报告经过来源校验，Run succeeded |
| 审批拒绝 | 检索 0 次、成功结果帧 0，证据不满足的任务失败并保留记录 |
| 审批等待中取消 | 原 prompt 确认 cancelled；关闭会话并退出，Run cancelled，工具 0 次 |
| 审批等待中撤权 | 旧授权心跳被拒；原 prompt 确认 cancelled，Run AUTH_REVOKED，工具 0 次 |
| 关闭与恢复协议 | `new/list/close`、跨进程 `resume` 通过；`session/load` 被拒；resume 不重放历史 |
| SDK 与应用镜像 | 安装环境和无网络镜像各 39 项测试通过；核心 SDK 无 ACP/后端依赖，9 项通过 |
| Runtime/E2AG 回归 | Worker、工具通道、授权、恢复与应用契约 80 项通过，43 项按数据库类型跳过 |

SDK 在 peer 声明 `sessionCapabilities.close` 后，于取消处理和进程退出之间发出有界 `session/close`。关闭确认与进程退出分别保留；超时继续清理进程，实际退出前不提交成功结果。

## 后续验收

#56/#58 保持进行中。下一段为 Codex Adapter 与 A2A 互操作；活动会话断连后重接、跨物理主机故障、双来源续期/委派/旧 epoch 矩阵、企业网络准入与 G2 独立权限评审继续沿用原任务。跨进程恢复控制面实测与活动任务恢复分别验收。
