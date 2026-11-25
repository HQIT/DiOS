# 协议兼容与首条交付路径

首条完整交付路径采用 Cursor ACP，经 DiOS Worker、E2AG 授权与审批执行受控工具，形成报告并支持取消和故障恢复。DSH、Codex 保留已有适配和证据，后续扩展复用同一契约。多 Harness 联合验证在首条路径交付后推进。

## 固定组合

| 路径 | 协议及依赖 | 已验证组合 | 当前定位 |
| --- | --- | --- | --- |
| Cursor | ACP v1；`agent-client-protocol==1.0.0` | CLI `2026.11.05-86047df`，DiOS SDK `0.1.11` | 首条完整交付与工程验证路径 |
| DeepSeek Harness | 同一 ACP 客户端及 SDK | DSH `0.2.2-rc.5`，ECNU Max | 已有适配；新增实测后置 |
| Codex | 同一 ACP 客户端及 SDK | Codex ACP `2.11.0` / CLI `0.172.0` | 已有适配；新增实测后置 |
| Runtime A2A 入站/出站 | A2A `0.3.0` JSON-RPC；`a2a-sdk==0.3.26` | 官方客户端、真实 HTTP、独立 Worker | 已声明子集的协议契约；不增加首轮远端产品交付 |
| `/api/os/a2a` | 历史兼容接口 | 原 `A2ATask` 与服务记录 | legacy，单独查询与恢复；无标准符合性声明 |

ACP 的 load/resume 差异、原会话关联及未支持能力见[恢复矩阵](protocol-recovery-matrix-56.md)。A2A 入站仅开放非阻塞提交、查询和取消；流式、重订阅、推送、同任务续轮均显式拒绝。官方 SDK 互操作与已声明子集的测试结果不代表所有可选方法均已实现。

## 契约与证据

| 契约 | 验证内容 | 依据 |
| --- | --- | --- |
| 消息与关联 | 相同主体/profile/messageId 返回原 Run；内容或 Context 冲突拒绝；提交后响应丢失保持原回执 | `test_runtime_a2a` 的 concurrent replay、response lost；[恢复证据](evidence/protocol-recovery-closure-20261125.json) |
| 任务与产物 | 原 Task/Attempt/Session 关联；结果 Schema 不完整不成功；成功产物按权限投影和下载，部分输出保留 | `test_result_artifact_permissions_and_terminal_cancel`、`test_invalid_result_is_retained_and_fails_schema` |
| 身份与权限 | 相同消息的不同主体不混用；共享读取不获得取消权；主体撤销后不启动执行 | `test_auth_owner_share_cancel_and_profile_boundaries`、`test_e2ag_revoked_work_never_launches` |
| ACP 权限请求 | 当前 Session、工具别名与参数绑定；仅允许经过网关的单次许可；文件写入、原生终端及未知操作拒绝 | SDK `test_acp_contract`；固定镜像中的已安装 SDK 测试及真实模型[证据](evidence/usage-harnesses-20261125.json) |
| 取消确认 | ACP 保留原 prompt 响应，收到 cancelled 才确认；A2A 等待远端终态，重复取消不产生新执行 | SDK `test_acp_cancellation`；Cursor [真实取消](evidence/acp-cancellation-20261124.json)；A2A [恢复证据](evidence/protocol-recovery-closure-20261125.json) |
| 外发与凭据 | Card 不得切换已注册端点；禁止重定向和越界产物 URI；响应 ID 与长度校验 | `test_card_redirect_and_cross_origin_artifact_never_receive_credentials`、`test_transport_rejects_redirect_oversize_and_wrong_rpc_id` |
| 未支持与格式错误 | 未支持操作不入队；非法 JSON/UTF-8 返回协议错误；配置移除后授权历史仍可读取 | `test_runtime_a2a` 的 strict wire、malformed JSON、historical queries |

本项汇总[工程证据](evidence/protocol-compatibility-20261125.json)。已通过且源码未变化的 SDK/模型证据复用；原生 Shell、文件、网络副作用的实际拒绝资格由 K03-2.01 和 K01-4.03 承接，先完成 Cursor。

## 实现边界

通用 Worker、调度、租约、预算和 E2AG 控制按注册配置、任务身份与授权对象工作，不根据 Harness 品牌决定权限。SDK 中 `cursor.py`、`dsh.py`、`codex.py` 处理各实现的通知字段与结果格式，调用同一 ACP 客户端和工具网关。Cursor 的 MCP 名称转换只允许转发至 DiOS 网关，不授予工具执行权。

`backend/app/runtime/acp_bridge.py` 与 `cursor_acp.py` 是保留的旧桥接实现；当前独立应用使用公开 SDK。它们不继承 SDK 新版本的符合性结论。更换 Harness 不要求修改通用调度与授权状态机。

端点版本或能力声明变化由不可变 Revision 和[兼容门禁](endpoint-compatibility-31.md)处理。新增 Harness 先声明控制范围与差异，再接入同一测试契约。首轮不为其他 Harness 增加专用调度、存储或治理逻辑。

依据：[A2A 0.3.0 规范](https://a2a-protocol.org/v0.3.0/specification/)定义消息、任务、产物及取消；[ACP v1 Tool Calls](https://agentclientprotocol.com/protocol/v1/tool-calls)区分工具通知与权限请求。工具通知本身不证明动作受到强制控制。
