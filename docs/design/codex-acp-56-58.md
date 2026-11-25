# Codex ACP 接入与模型服务资格

Codex 已通过独立 static-review 应用执行受控评审，复用 Cursor、DSH 的 Runtime/E2AG 链路。固定组合为 ACP Adapter 2.11.0、Codex CLI 0.172.0、DiOS SDK 0.1.4、static-review 0.1.3；真实治理验证使用已配置的官方账号与 `gpt-6-astra`。

## 接口与职责

[Codex ACP Adapter](https://github.com/agentclientprotocol/codex-acp/tree/v2.11.0)连接官方 Codex App Server，映射 ACP 会话、MCP 工具与权限请求。[Codex 固定版本配置](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/core/config.schema.json)提供模型 Provider、只读权限和工具功能开关。DiOS 使用这些既有接口。

SDK 的 `codex` 模块校验工具元数据中的 `server/tool/arguments`，只把 `server=dios` 的调用映射到注册代理。E2AG 继续负责授权版本、参数、审批、派发与结果验收；独立应用负责输入、代码引用与报告。平台调度和授权不增加 Codex 专属分支。

部署配置见 [Codex profile](../../applications/static-review/examples/codex/README.md)。模型服务、账号和 Harness 分开登记；凭据通过现有版本化 Secret 引用注入。容器使用独立可写状态目录、非 root、只读根文件系统、资源限额与受控 MCP 代理。

## 实测结果

[运行与测试证据](evidence/codex-acp-20261124.json)保留固定镜像、Run/Attempt/Session、工具计数、诊断码和退出回执摘要。每个场景采用独立数据库、Worker 和执行容器，输入为固定合成源码。

| 场景 | 结果 |
| --- | --- |
| 审批通过 | 审批前检索 0 次，通过后 1 次；报告通过行号和来源校验，Run succeeded |
| 审批拒绝 | 检索 0 次、成功结果帧 0；任务失败并保留输出与拒绝记录 |
| 审批等待中取消 | 原 prompt 确认 cancelled，session/close 确认，容器退出；Run cancelled、工具 0 次 |
| 审批等待中撤销 | 旧授权心跳被拒；原 prompt 确认 cancelled，容器退出；Run AUTH_REVOKED、工具 0 次 |
| SDK 安装和离线镜像 | 各 42 项通过；无 ACP/后端依赖的核心 SDK 9 项通过 |
| Runtime/E2AG 回归 | Worker、工具通道、工作授权、恢复和应用契约 80 项通过，43 项按数据库类型跳过 |

该 Adapter 在 session/close 确认后仍需有界终止进程，正常报告场景的应用退出码为 2，并记录 `ACP_CHILD_EXIT_NONZERO`。Runtime 依据显式 `accept_complete_on_error` 策略接受已完成校验的报告，保留退出证据，不重复调用模型。取消和撤销始终没有成功结果帧。生产资格继续跟踪 Adapter 的优雅退出能力。

## 模型服务兼容性

ECNU Max 的普通 Responses 文本请求成功。受控工具验证中，Codex 正常启动 MCP 服务并发送 `namespace` 工具定义，但模型未取得检索工具，Run 按证据不足失败。进一步使用相同工具参数和强制工具调用做独立对照：

| ECNU 请求形态 | HTTP / 结果 | 准入判断 |
| --- | --- | --- |
| 普通 function 工具 | 200，返回 lookup 调用及参数 | 此请求形态通过 |
| namespace 包含 function | 500 | 此组合不满足 Codex 工具协议要求 |

因此，HTTP 连通、文本回答和 `/responses` 地址存在各自记录；Harness 的实际请求形态、工具执行、审批与取消也要分别验证。ECNU Max 继续用于已验证的 DSH 组合。Codex 使用官方账号通过本次完整工具切片，后续网关按相同对照取得准入证据。

## 后续验收

#56/#58 保持进行中。Cursor、DSH、Codex 与标准 A2A 均已有实际接入证据；三种 ACP Harness 的跨进程检查见 [会话恢复契约](acp-session-recovery-56-58.md)。下一段补齐跨来源续期、委派与旧 epoch 联合验证。活动请求续接、跨物理主机、原生工具额外路径、企业网络与凭据、G2 独立评审及用户验收继续沿用既定门槛。
