# 两来源 Harness 执行治理资格

Cursor ACP 与 DeepSeek Harness 已完成固定版本的受控执行矩阵。DSH 使用 0.2.2-rc.5、DiOS SDK 0.1.19 和 ECNU Max；模型、业务应用与治理底座保持独立。证据见 [DSH 实测](evidence/dsh-native-controls-20261126.json)及 [Cursor 实测](evidence/cursor-native-controls-20261126.json)。K01-5.01 工程验证完成，K01-5.03 联合评审待指定责任人接受。

## 执行控制

DSH 原生只读模式允许启动 shell，实测曾运行合成脚本后才拒绝写入。静态评审使用官方 `ctx.tools.guard()`，在工具函数运行前拒绝原生读取、编辑、shell 和网页访问；动态插件、替代执行器及原生工具在正式组合中关闭。ACP 必须依赖拦截器服务，拦截器缺失时初始化失败。

已登记的 `mcp__dios__*` 调用通过 DiOS SDK 转交 Worker 工具通道；E2AG 继续检查工作授权、调用参数、审批、委派、租约 epoch 与结果。拦截器只收紧本地能力，不能授予 MCP 权限。Guard 与配置固定在开发镜像 `sha256:39c9ab375eb421ab31aec60aa5a9f45cd6d6e3d752b0636c47ca1e0d56e80151` 中。

Worker 关闭 Docker SDK 的隐式代理配置注入，实际代理由授权的 LaunchSpec 指定。独立内部网络禁止直连，Squid 仅放行指定模型域名的 443 CONNECT；模型凭据经 `worker_secret_refs` 显式挂载，工具凭据留在网关。

## 验收矩阵

| 条款 | Cursor 依据 | DSH 结果 |
| --- | --- | --- |
| 同一任务、尝试、会话和报告契约 | 已有原生控制及恢复证据 | 正常审批、一次检索、来源校验报告通过；固定镜像部署任务通过 |
| 审批重放及参数变化 | 已有过期与重放场景 | 完成派发重放不重复检索；变更参数、过期审批均拒绝，检索 0 次 |
| 祖先撤权、旧 epoch | [真实治理矩阵](evidence/harness-governance-20261125.json) | 两场景均阻止派发，检索 0 次，原尝试及审批记录保留 |
| 原生动作 | 已有文件、构建、WebFetch 控制 | read/edit/bash/web_fetch 均返回执行前拒绝；源码字节不变，构建标记未产生，标记正对照有效 |
| 外发与只读文件系统 | 已有 11 项边界检查 | 11 项通过：非授权域名、伪装域名、私网地址、错误端口、普通 HTTP、直连及写入均拒绝 |
| 配置缺失 | 固定 Cursor CLI 权限配置 | 无模型凭据、无网络条件下验证 guard 存在可初始化、缺失无法初始化 |
| 完整结果与恢复 | [Cursor 恢复与升级](cursor-recovery-upgrade-58.md) | 沿用平台既有结果校验；原生拒绝后的结果帧为 0 |

本批新增 6 次真实模型场景（含固定镜像交付），15 项 Worker/凭据回归通过、1 项条件测试跳过；原生 Docker 代理注入回归 1 项通过。Cursor 已有合格切片复用，未重复调用模型。原生拒绝轨迹与缺少拦截器的启动检查分别保留；增加启动依赖没有改变拒绝判定。

## 运行边界

当前资格覆盖固定的两种 Harness 及合成输入。新增执行器、插件和版本需要相应资格；原生自由执行未向静态评审开放。代理限定目标地址，不检查 TLS 请求内容，允许的模型服务会收到提交的数据。物理跨主机与独立安全评审仍分别由既定 WBS 承接。

## 公开依据

- [DSH tools 官方策略接口](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/tools/README.md)：执行前单调拒绝 guard。
- [DSH 插件配置与启动](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/reference/README.md)：patch 插入与组合检查；同时核对固定发布包的实际接口。
- [Docker SDK 容器接口](https://docker-py.readthedocs.io/en/stable/containers.html)：`use_config_proxy=False` 关闭宿主客户端代理注入。
