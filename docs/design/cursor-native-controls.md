# Cursor ACP 执行控制

首条交付路径使用固定版本 Cursor ACP，复用 Worker、E2AG 工具网关及报告存储。正常任务采用 `ask` 模式；真实原生工具资格使用 `agent` 模式主动尝试禁止动作。[工程证据](evidence/cursor-native-controls-20261126.json)包含三个真实模型场景、11 项容器边界检查及开发环境报告。

| 路径 | 控制点 | 实测结果 |
| --- | --- | --- |
| MCP 检索 | E2AG 绑定工具、参数、Run/Attempt/Session、授权版本；独立审批后派发 | 批准执行一次；重复派发不增加调用；参数替换、旧审批版本拒绝；过期后调用零次 |
| 源码编辑 | Cursor 项目级 `Write(**)` 拒绝；源码只读挂载、非 root | 合成源码字节保持不变 |
| Shell/构建 | Cursor 项目级 `Shell(*)` 拒绝；ACP 终端能力关闭 | 构建标记不存在；同镜像、同用户直接执行合成探针可生成标记，确认观测有效 |
| 原生 WebFetch | Cursor 策略与 ACP 权限回调 | 明确返回 `reject-once`，E2AG 保留 `ACP_NATIVE_OPERATION_DENIED` |
| 网络 | Docker internal + isolated 网络；Squid 精确域名白名单，仅 CONNECT 443 | 模型域名连通；非许可域名、后缀伪装、私网目标、错误端口、普通 HTTP 拒绝；直接外连及宿主桥地址不可达 |
| 成果接收 | SDK 校验实际工具记录与受控调用 | 未验证原生活动返回 `ACP_TOOL_ACTIVITY_UNVERIFIED`，不生成成功报告；正常批准路径产生完整报告 |

Cursor 对本次编辑和 Shell 尝试发送了 `completed` 通知，但没有执行输出；实际文件与构建标记均无变化。通知状态、ACP 决策、E2AG 记录和副作用证据分别保存。Shell 限制依赖固定 Cursor 的工具策略；镜像仍包含运行 Harness 所需的解释器。本项没有获得每个系统调用的内核跟踪证据。

网络代理按目标过滤，不检查 TLS 内容。许可模型服务会接收提交的数据。模型凭据在容器内使用，企业资料准入与更强凭据隔离继续由既定工作项承接。

## 部署

目标环境为 Linux/WSL；本次使用 Docker 29.1.3、Squid 6.13、Cursor CLI `2026.11.05-86047df`、`dios-sdk` 0.1.11。执行镜像和代理镜像均固定摘要。

```sh
docker compose -p dios-cursor-primary -f deploy/runtime-egress/compose.yaml up -d
```

使用 [受控 profile](../../examples/runtime/cursor-governed-tools-profile.json) 注册执行配置；将 [Cursor 权限配置](../../examples/runtime/cursor-cli-permissions.json)只读挂载到 `/work/.cursor/cli.json`，ACP 工作目录设为 `/work`。镜像内的 Cursor 启动命令须与固定发行包一致。容器连接 `dios-controlled-workloads`，通过 `model-egress:3128` 访问模型。源码服务保持在可信 Worker 一侧，Harness 经审批工具通道检索。

网络必须支持 `gateway_mode_ipv4=isolated`。不支持时升级 Docker，不能改成普通 bridge 继续声明同等隔离。部署中不暴露代理端口，不把其他应用加入工作负载网络。修改镜像、挂载或网络后同步注册 profile 摘要、工作权限与审批身份的资源版本；活跃任务排空后再切换。

开发环境已注册更新后的 `cursor-acp-review`，真实任务 `c1d03cff-e5cb-4bdb-af8d-55988805f2d9` 经独立审批完成，产生 Markdown 报告，原有 84 条任务身份保留。API 与其他 Worker 未重启。

## 资格复用

`scripts/qualify_harness_governance.py` 的 `approve`、`approval-expired`、`native-deny` 在隔离数据库和合成资料上执行。原生探针使用 `scripts/fixtures/cursor_native_control_probe.py`，网络和文件边界使用 `scripts/fixtures/runtime_egress_probe.py`。`scripts/check_cursor_control_evidence.py` 核对已保存结果及构建标记的阳性对照，不重复调用模型。资格环境目录和配置文件由操作者提供，凭据使用既有 Secret 引用。

运行 `approval-expired` 会在隔离账本中注入过期状态；不用于生产数据库。真实拒绝结果保持原 Run/Attempt/Session，正常报告和失败输出分别归档。K03-2.01 完成后，K01-4.03 复用本证据核对声明与实际控制点；第二 Harness 的实测继续后置。

## 依据

[Cursor ACP](https://cursor.com/docs/cli/acp)、[Cursor 权限](https://cursor.com/docs/cli/reference/permissions)、[Docker isolated 网络](https://docs.docker.com/engine/network/port-publishing/#gateway-modes)、[Squid ACL](https://www.squid-cache.org/Doc/config/acl/)。
