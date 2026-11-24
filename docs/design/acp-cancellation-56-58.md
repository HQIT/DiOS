# ACP 取消确认与受控执行

DiOS SDK 0.1.2 使用 ACP 官方 Python SDK 1.0.0 管理 JSON-RPC 请求关联、并发请求和连接断开。ACP 是可选依赖；业务输入与报告规则属于独立 static-review 0.1.1 应用。Runtime、E2AG 和任务 API 沿用通用契约。

## 选型与责任

[ACP 官方 SDK](https://agentclientprotocol.github.io/python-sdk/)提供消息级 Transport 接口和 Connection，适合接入既有 Worker 工具通道。其默认 NDJSON 传输会累积超过流读取限制的分块，并为收到的请求创建独立任务。DiOS 的薄传输层据此限定单帧 256 KiB、最多 16 个未响应请求，并按接收顺序校验会话及工具元数据；RPC 关联和任务调度交给官方组件。版本固定为 1.0.0，升级时运行相同协议和供应商资格测试。

| 层 | 责任 |
| --- | --- |
| 官方 ACP Connection | 请求与响应关联、并发请求处理、连接断开后的等待者收敛 |
| DiOS SDK ACP 适配 | 有界传输、会话绑定、权限转交、取消等待和输出保留 |
| Worker / E2AG | 当前 Run、Attempt、租约和授权版本校验；审批、真实工具派发、进程退出与产物证据 |
| 独立应用 | 输入规则、报告完整性和业务复核 |

Cursor 元数据映射位于 SDK 的 `cursor` 模块。新独立应用使用 `dios-cursor-review`；旧镜像中的后端适配副本保持原版本，升级需重新构建应用镜像并切换 profile。

## 取消语义

依据 [ACP Prompt Turn 取消规范](https://agentclientprotocol.com/protocol/v1/prompt-turn)，取消会话后仍接收更新，待处理权限返回 cancelled，原 `session/prompt` 的 `stopReason=cancelled` 才构成协议确认。

1. Worker 接到取消或发现授权撤销，停止执行容器；SDK 捕获终止信号，停止接受新工具请求并取消待处理权限。
2. SDK 发出 `session/cancel`，保留原 prompt 请求关联，最多等待 2 秒。期间输出继续归档。
3. 收到 cancelled 时记录 `ACP_CANCEL_ACKNOWLEDGED`；正常完成与取消竞争时记录 `ACP_CANCEL_COMPLETION_RACE`；断连或超时记录 `ACP_CANCEL_UNCONFIRMED`。
4. 关闭输入并等待进程退出；超时后终止或杀死进程。Worker 依据实际退出凭据写入取消终态，保留原 Run、Attempt、Session 和部分输出。被取消的 SDK 调用不产生成功结果帧。

查询超时保留原 prompt，随后使用同一关联执行取消。旧授权心跳、工具派发和结果提交继续由 E2AG/Runtime 拒绝。协议确认、进程退出和远端外部副作用分别记录；缺少外部执行结果时沿用对账机制。

## 固定版本验证

Cursor CLI `2026.11.05-86047df`，Linux/WSL Docker，只读根文件系统、独立进程、固定合成源码和注册 MCP 检索；模型网络经过域名许可代理。证据见 [运行与测试记录](evidence/acp-cancellation-20261124.json)。

| 场景 | 已验证结果 |
| --- | --- |
| 审批等待中取消 | 原 prompt 收到 cancelled；容器退出；Run cancelled；工具调用 0，成功结果帧 0 |
| 审批等待中撤销授权 | 原 prompt 收到 cancelled；容器退出；Run failed / AUTH_REVOKED；旧授权心跳被拒，工具调用 0 |
| 审批通过 | 审批前工具调用 0，批准后 1；报告通过来源与结果校验 |
| SDK 协议及进程回归 | 33 项通过：阻塞权限/文件请求、迟到输出、断连、取消超时、超大帧、请求上限、不同类型请求 ID、SIGTERM 实际子进程退出、超大输出后的退出收敛 |
| Worker / E2AG / 恢复 / 应用契约 | 80 项通过，43 项因数据库类型跳过 |

SDK 安装环境与无网络应用镜像各自运行 33 项测试。应用 bundle 包含完整依赖，核心 HTTP 客户端保持无第三方运行时依赖。

## 接续与升级

应用升级以固定镜像 digest 和对应 E2AG profile 版本进行：停止目标 Worker 领取、排空在途任务和工具调用、切换并恢复领取。回退时执行同样的排空流程。开发资格环境与现有开发 API、知识应用分别部署；生产评审不参与切换。

#56/#58 继续推进第二来源 Agent 与 A2A 互操作、跨物理主机故障、Cursor 活跃会话重连/原生 resume、未经过 ACP 回调的内置工具资格。双来源续期、委派和旧 epoch 的完整场景，以及 G2 独立权限评审和用户产品验收仍按原计划完成。
