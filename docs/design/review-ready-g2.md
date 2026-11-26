# 运行与治理 G2 评审

Docker 生命周期、Poll Connector、工具治理和委派安全四项进入责任人评审，共 21 条原验收。各项独立填写接受结论，工程证据与 G2 接受分别记录。

| 项目 | 内容 |
| --- | --- |
| 文档标识 | REVIEW-G2-RUNTIME-GOVERNANCE |
| 状态 | 待指定责任人评审 |
| 提交基线 | `72fac6d3c806b3a712718667520bc5959c541964` |
| 工作项 | K01-1.02、K01-3.01、K03-2.03、K03-3.03 |
| 评审角色 | 架构、模块、测试运维；权限边界另需实现者之外的安全评审人 |
| 接受人、提交和日期 | 待填写 |
| 机器可读索引 | [原条款、定位及证据摘要](evidence/ready-g2-review.json) |

Infrastructure、DiOS、客户无关的数字分身平台和客户应用分层保持不变。本文接受范围为通用运行与治理能力；客户岗位、流程与业务成果的验收由上层交付承接。

## K01-1.02 · #10 Docker 交付评审

通用服务通过 RuntimeAdapter 管理 task/service，第三方 Harness 的协议交互保留在 SDK。固定 Docker 组合已有取消、失败回收、健康复用和路径变化证据。最新代理注入修复另有实际容器与交付 CI 记录。

| 原验收 | 证据定位 | 接受结论 |
| --- | --- | --- |
| K01-1-A01 通用 OS 服务不依赖 Docker 类型分支 | [lifecycle-closure-20261126](evidence/lifecycle-closure-20261126.json) | 待评审 |
| K01-1-A02 service/task 生命周期、取消、故障和资源回收通过回归 | [lifecycle-closure-20261126](evidence/lifecycle-closure-20261126.json)、[managed-lifecycle-20261124](evidence/managed-lifecycle-20261124.json) | 待评审 |
| K01-1-A03 backend 测试和镜像构建通过；后续拓扑不依赖单机路径假设 | [lifecycle-closure-20261126](evidence/lifecycle-closure-20261126.json)、[lifecycle-closure-20261126](evidence/lifecycle-closure-20261126.json) | 待评审 |
| K01-1-A04 至少一种第三方本地 Harness 的启动配置可表达，厂商交互逻辑不进入容器生命周期服务 | [lifecycle-closure-20261126](evidence/lifecycle-closure-20261126.json)、[dsh-native-controls-20261126](evidence/dsh-native-controls-20261126.json) | 待评审 |

适用范围：本地路径必须在所选 Worker 上有效；远程 cgroup/fencing 与物理跨主机资格独立承接。服务协议缺少逐请求取消时，保留不支持声明。

责任人、接受提交与日期、结论及复审条件：待填写。

## K01-3.01 · #9 Poll Connector 结项评审

通过 manifest 注册的 Poll Adapter 使用通用轮询运行时；原生 TLS IMAP、持久化后确认、故障隔离及独立进程锁释放已验证。

| 原验收 | 证据定位 | 接受结论 |
| --- | --- | --- |
| K01-3-A01 测试 Connector 无需修改通用服务即可被调用 | [connector-poll-20261124](evidence/connector-poll-20261124.json) | 待评审 |
| K01-3-A02 通用 Poll Runtime 无类型判断；单实例失败不阻塞其他 Connector | [connector-poll-20261124](evidence/connector-poll-20261124.json)、[connector-poll-20261124](evidence/connector-poll-20261124.json) | 待评审 |
| K01-3-A03 IMAP 回归、后端测试和镜像构建通过 | [connector-poll-20261124](evidence/connector-poll-20261124.json)、[connector-poll-20261124](evidence/connector-poll-20261124.json)、[connector-poll-20261124](evidence/connector-poll-20261124.json) | 待评审 |

适用范围：同一安装共享本地锁目录；跨主机指定唯一采集主机。事件采用时间窗去重与至少一次接收；客户邮箱不在既有合成资格范围。Linux/WSL 是当前交付平台。

责任人、接受提交与日期、结论及复审条件：待填写。

## K03-2.03 · #34 工具治理安全评审

工作授权、参数、审批消费、持久派发意图与结果验收由 Runtime/E2AG 统一控制；固定 Harness、文件和网络控制单独留证。SQLite/PostgreSQL 与内置权限/OPA 四组治理契约及持久撤销后续修复均有记录。

| 原验收 | 证据定位 | 接受结论 |
| --- | --- | --- |
| K03-2-A01 未授予工具及审批前副作用在执行边界被阻止 | [approval-qualification-20261124](evidence/approval-qualification-20261124.json)、[dsh-native-controls-20261126](evidence/dsh-native-controls-20261126.json) | 待评审 |
| K03-2-A02 参数变化、审批过期和结果未知有明确处理 | [dsh-native-controls-20261126](evidence/dsh-native-controls-20261126.json)、[governance-matrix-20261126](evidence/governance-matrix-20261126.json) | 待评审 |
| K03-2-A03 受管工作负载必须声明治理配置；兼容路径显式标记；API/CLI/Console 回归通过 | [protocol-control-mapping-20261126](evidence/protocol-control-mapping-20261126.json)、[approval-qualification-20261124](evidence/approval-qualification-20261124.json) | 待评审 |
| K03-2-A04 工具通知与强制执行证据分开记录；能力不足的端点被拒绝承接相应风险工作 | [protocol-control-mapping-20261126](evidence/protocol-control-mapping-20261126.json)、[dsh-native-controls-20261126](evidence/dsh-native-controls-20261126.json) | 待评审 |
| K03-2-A05 真实 Agent 无法写源码、运行业务构建/脚本或越权外发；拒绝事件和实际文件/网络证据一致 | [cursor-native-controls-20261126](evidence/cursor-native-controls-20261126.json)、[dsh-native-controls-20261126](evidence/dsh-native-controls-20261126.json)、[dsh-native-controls-20261126](evidence/dsh-native-controls-20261126.json) | 待评审 |
| K03-2-A06 缺少治理声明的受管任务拒绝准入；MCP/原生 Shell/文件/网络分别验证实际拒绝，兼容路径明确标识 | [protocol-control-mapping-20261126](evidence/protocol-control-mapping-20261126.json)、[dsh-native-controls-20261126](evidence/dsh-native-controls-20261126.json) | 待评审 |
| K03-2-A07 审批单次消费、撤销/派发事务竞争、原生工具拒绝和授权/审计故障暂停通过；恢复保留原任务身份 | [governance-matrix-20261126](evidence/governance-matrix-20261126.json)、[revocation-durable-20261126](evidence/revocation-durable-20261126.json) | 待评审 |
| K03-2-A08 采用不同策略及存储机制时保持授权结果、审批单次消费、意图先持久化与撤销顺序；不降低治理保证获取性能 | [governance-matrix-20261126](evidence/governance-matrix-20261126.json)、[revocation-durable-20261126](evidence/revocation-durable-20261126.json) | 待评审 |

适用范围：撤销前已提交派发的动作可能产生副作用，保留在途结果并拒绝新动作。外部策略来源与 Runtime 不是单一事务；未知结果不自动重放。固定版本原生控制不扩展到任意插件和远端内部实现。

责任人、接受提交与日期、结论及复审条件：待填写。

## K03-3.03 · #35 委派安全评审

三层委派逐级收窄，来源主体停用使后续受限动作拒绝；审批人资格在批准和派发时复核，自审批被拒绝。来源事件重复、乱序、并发派发及会话替换均有专项证据。

| 原验收 | 证据定位 | 接受结论 |
| --- | --- | --- |
| K03-3-A01 跨部门与跨工作越权、委派提权被拒绝 | [delegation-combinations-20261126](evidence/delegation-combinations-20261126.json) | 待评审 |
| K03-3-A02 停用身份后新的受限动作无法执行，在途结果可解释 | [delegation-combinations-20261126](evidence/delegation-combinations-20261126.json) | 待评审 |
| K03-3-A03 状态事件重复/乱序不破坏上层工作状态 | [delegation-combinations-20261126](evidence/delegation-combinations-20261126.json) | 待评审 |
| K03-3-A04 撤销时限与可控制范围可追踪，界面和 API 不把取消请求等同于已停止 | [delegation-combinations-20261126](evidence/delegation-combinations-20261126.json) | 待评审 |
| K03-3-A05 参数变化、会话替换、委派和并发不能扩大权限；不足有版本化扩展及回归证据 | [delegation-combinations-20261126](evidence/delegation-combinations-20261126.json) | 待评审 |
| K03-3-A06 三层委托收窄、上游主体撤销传播、审批人资格变化及自审批拒绝通过 | [delegation-combinations-20261126](evidence/delegation-combinations-20261126.json) | 待评审 |

适用范围：传播从来源事件到达本地后计量，尚未送达的外部撤销不能由缓存推断。停止请求与进程退出分别记录；实际传播目标按部署测量。跨物理主机和等待子任务的容量语义沿用各自工作项。

责任人、接受提交与日期、结论及复审条件：待填写。

## 版本与证据使用

索引保留每份报告的 SHA-256、声明过的源码摘要与当前源码差异。无源码摘要的报告按其固定镜像及场景解释；历史工程报告不自动授予最新源码的全部组合资格。

Docker Adapter 后续关闭宿主代理隐式注入，实际容器回归及 DSH 固定镜像验证见 [DSH 控制证据](evidence/dsh-native-controls-20261126.json)。[30cb9cd 交付 CI](https://github.com/HQIT/DiOS/actions/runs/45848821833)已完成安装、受控任务、升级和回滚；其中通用故障场景的未完成条款仍由原 WBS 承接。

治理原四组契约之后增加“拒绝事务回滚仍保留撤销”修复，采用其后续证据作为对应接受输入。协议、工具通道、子任务和入口后续变更分别列在索引中，评审应根据影响接受适用范围或提出具体修订。

## 联合接受与下游

每项填写通过、有条件通过或需修订，并记录接受的版本、残余问题、责任人与复审条件。#58 的 [Harness 联合评审](review-harness-58.md)与本文共享证据，结论独立保留。评审通过后才将对应 WBS 标为完成并按原范围关闭 Issue。

当前五项就绪评审材料共覆盖 27 条原验收；整体完成数仍为 54/79。其余评审项依赖尚未完成的工程资格，物理跨主机、源端恢复隔离和父子等待容量仍按既定任务推进。
