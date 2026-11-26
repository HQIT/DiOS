# 协议能力与执行控制

首轮资格使用 Cursor ACP。DiOS 的 Runtime 负责执行生命周期与授权身份，SDK 协议适配负责厂商消息转换，工具网关负责动作授权，沙箱负责文件和网络边界。上层应用提供任务与成果契约。

| 声明或请求 | 实际控制点 | 首轮允许范围与拒绝行为 |
| --- | --- | --- |
| ACP `protocolVersion` | SDK `session_capabilities` | 版本 1；不兼容响应在创建任务会话前失败 |
| ACP 客户端文件能力 | SDK `initialize`、`AcpClient.request` | 默认不声明文件能力；只有登记 `client_read_tool` 才声明读取，并经工具网关。写入始终未开放 |
| ACP 终端能力 | SDK `AcpClient.request` | 不声明；create/output/wait/kill/release 均拒绝，Worker 记录原生操作拒绝 |
| ACP 权限请求 | Cursor `proxy_permission` → Worker `permit_proxy` | 只识别已登记的 DiOS MCP 工具与有效参数。`allow_once` 仅进入代理，实际调用仍等待独立审批 |
| Agent 工具完成通知 | SDK 活动归一化 → Worker `verify_activity` / `verify_required` | 按成功调用、参数及会话核对；通知自身不生成执行权或成功成果 |
| Harness 内部原生动作 | 固定 Cursor CLI 策略、Docker 文件边界、Squid 出站控制 | 文件/命令/WebFetch 限制及真实副作用见 [Cursor 控制证据](cursor-native-controls.md) |
| 严格本地执行配置 | Profile `isolation`、Worker、Docker inspect | 原生网络关闭、工作区只读、排除子挂载；工具通过 E2AG Unix 通道并受预算约束，见[组合策略](execution-isolation-45.md)。需要直接模型网络的配置沿用其专用控制资格 |
| API 选择执行配置 | `TaskConsole` 注册目录、E2AG `FileWorkPolicy.admission` | 请求只能选已启用配置；未登记 ID 拒绝。镜像、命令、网络或根文件系统控制变化导致摘要不匹配，重新准入前不入队 |
| API 自报能力与治理模式 | `TaskInput` 严格字段校验 | 请求体不能追加能力、治理或启动参数以扩大权限 |
| 兼容执行入口 | 任务输入契约 `requires_e2ag` | 明确要求治理的任务不能交给 `trusted-local` 配置 |
| 远程 A2A | Agent Card 兼容门禁与远程配置准入 | 声明远端控制范围，不能继承本地 Docker/原生工具资格；已授权的远端任务按原身份查询和对账 |

ACP 的客户端文件能力描述客户端提供的 RPC 接口。Harness 自己的文件、Shell 和网络实现仍由其固定配置与运行沙箱约束。[ACP 文件规范](https://agentclientprotocol.com/protocol/v1/file-system)要求未声明的接口不可调用；DiOS 同时拒绝违反声明的请求，保留实际控制证据。

## 资格与注册

运维负责启用经过资格验证的执行配置；E2AG 将执行权限绑定到该配置的摘要。现有准入校验不自动阅读测试报告，也不将厂商自报的能力当成资格。新增 Harness 的适配代码可以保留，风险动作只有在相应工具、配置版本和权限明确登记后才可进入受控通道。

固定 Cursor 路径与 DSH 第二来源分别已有资格，见[DSH 控制矩阵](dsh-native-controls-58.md)。Codex 保留原有实验登记与能力限制；联合接受由 [K01-5.03](review-harness-58.md) 承接。A2A 适配无法证明远端服务内部沙箱，要求原生控制的任务使用已验证的本地执行配置。管理者可更改受信任配置文件；配置内容、代理服务及宿主管理员是运维信任边界，不纳入模型权限。

## 工程证据

[资格记录](evidence/protocol-control-mapping-20261126.json)复用 K03-2.01 的真实 Cursor、11 项边界检查和开发任务报告，新增五项组合测试：SDK 经过真实 Worker Unix Socket 拒绝七种文件/终端操作、伪造工具声明无权执行、四类控制配置变化拒绝准入、未知配置及请求自报能力不能入队、兼容入口不能接受受治理任务。

通用调度、Worker、E2AG、Docker Adapter 中不按 Cursor/DSH/Codex 名称分支；厂商元数据转换保留在 SDK 适配器。原有生命周期与恢复证据沿用 K01-4.01/.02，本项不重复启动模型任务。

协议依据：[ACP 初始化](https://agentclientprotocol.com/protocol/v1/initialization)、[Cursor 权限配置](https://cursor.com/docs/cli/reference/permissions)。
