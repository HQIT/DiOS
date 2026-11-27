# 安装恢复的研究依据与实现约束

DiOS 的恢复沿用一致检查点、资源端隔离和持久完成记录三类成熟机制。K02-3.02 必须同时证明历史关联完整、旧执行权失效和外部结果可对账；仅验证恢复文件可读不足以解锁调度。

## 研究依据

| 已发表研究 | 与本任务有关的结论 | DiOS 适用条件与取舍 |
| --- | --- | --- |
| Chandy 与 Lamport，*Distributed Snapshots: Determining Global States of Distributed Systems*，ACM TOCS 1985，§2–3 | 全局状态包含进程状态和通信通道中的消息；原算法假定消息可靠且按发送顺序交付 | 数据库分别快照与引用校验提供组件完整性；远端 spool、未确认回执和已发出调用还需纳入恢复点。当前采用既有排空机制形成可验证的检查点；开放 Harness 通道未满足原算法假设，暂不直接实现 marker 协议。[论文](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/12/Determining-Global-States-of-a-Distributed-System.pdf) |
| Burrows，*The Chubby lock service for loosely-coupled distributed systems*，OSDI 2006，§2.1、§2.4 | 锁持有者携带 sequencer，资源服务检查其有效性，拒绝迟到或失效请求 | 复用 DiOS 的 Worker 身份、Attempt epoch 和工具派发检查。恢复库中的旧 epoch 不足以隔离仍运行的源控制面；启用前须在实际接收端证明旧凭据/旧所有者被拒绝。仅在副本中设置 hold 或撤销记录不覆盖源端。[论文](https://www.usenix.org/legacy/event/osdi06/tech/full_papers/burrows/burrows.pdf) |
| Lee 等，*Implementing Linearizability at Large Scale and Low Latency*（RIFL），SOSP 2015，§4–5 | 持久完成记录使重复 RPC 返回原结果；完成记录随对象迁移。RAMCloud 实现将完成记录与对象变化原子写入日志 | 复用请求身份、远程回执、工具回执和结果产物。同一数据库事务中的去重可沿用；外部模型或工具若不提供幂等/结果查询能力，DiOS 本地事务无法覆盖外部副作用，未知结果必须保持待对账。[论文](https://web.stanford.edu/~ouster/cgi-bin/papers/rifl.pdf) |

以上是对研究结果的工程应用判断。DiOS 当前采用 SQLite 原生快照获取单库一致副本；SQLite 官方说明其 Online Backup API 可在并发访问期间生成快照。[官方实现依据](https://www.sqlite.org/backup.html)

## 现有能力与缺口

| 恢复要求 | 当前实现依据 | K02-3.02 的工作 |
| --- | --- | --- |
| 完整复制与引用核验 | `installation_backup.py`、`installation_catalog.py`、Runtime 格式 3 恢复包 | 恢复到独占新目录，保留 Run/Attempt、会话、版本、授权、回执、产物身份；工作目录重新定位与不可变 Revision 分开处理 |
| 入口冻结 | `runtime/lifecycle.py` 的全局 `restore_hold`；`managed_admission.py` 的作用域排空 | 兼容服务也须覆盖全部 scope；历史任务、后台恢复、取消/停止和旧工具入口均不得借恢复副本直接操作源执行器 |
| 旧所有者隔离 | `remote_control.py` 的认证与 binding；`worker_credentials.py` 的持久撤销 | 保留身份与历史，关闭旧执行权；新凭据必须经过现有身份配置、当前 E2AG 策略及原任务对账。验证对象包含实际接收端 |
| 重复请求与完整结果 | `remote_control.py` 的持久 receipts；`reconciliation.py` 的退出回执与结果校验 | 已完成调用优先读取原结果；保留 spool，重新认证和对账前不自动发送。不以查询超时判死、不因恢复重建 Attempt |
| 共同恢复点 | `managed_upgrade.py` 的双层排空；各组件快照 | 计划内恢复须证明入口停领、活跃与未知执行已处理、消息/补报水位已核验。灾难恢复中的未知项保持冻结，继续按原身份对账 |

安装级恢复与双层冻结已有[工程证据](evidence/installation-restore-hold-20261125.json)。K02-3.01 的证据覆盖组件快照和引用完整性，跨库共同恢复点与远端执行权由 K02-3.02/.03 的联合场景验证。

## 实施与验收顺序

1. 完成新目录恢复和双层冻结，验证原实例、备份及历史身份不被改写；失败不发布完整恢复凭据。
2. 复用既有 mTLS 身份及 E2AG 工作授权链路，演练旧证书/旧 Token/旧 epoch 拒绝、新身份重新认证和原任务结果查询。
3. 对恢复点后的完成记录、退出回执、工具结果和补报逐一对账；不完整证据保持未知，完整结果验证后归入原任务。
4. 在原控制面已隔离、恢复对象关联完整、未知副作用已解决的条件下验证恢复调度；重复触发不得产生第二次外部动作。
5. K02-3.03 复用这些证据，补升级失败与恢复耗时场景。实际跨主机环境资格仍按 W4 的既定任务推进。

新协议、无法由已有接收端实现的隔离保证，或缺少幂等/结果查询的外部动作，先列明已调研方案、未满足的前提及影响，再与用户讨论选择。未找到可适配方案时，不以临时状态字段或自动重试代替设计结论。

## 恢复期间的受控结果通道

`runtime.restore.*` 复用现有 Worker mTLS 传输、身份允许列表、E2AG `accept_result` 和持久回执。它仅在全局 `restore_hold` 下可用，允许 `prepare`、`get`、结果授权、受限分块上传和 `resolve_exit`。认领、续租、执行授权、工具派发和进程操作仍由原入口拒绝。

`prepare` 使用预期 Run 版本，将原活跃 Attempt 归入待对账并推进 Run epoch，保留原 Attempt/Worker/会话绑定。此变化表示旧所有者在恢复副本中失去执行权，不作为源进程已退出的证据。完整结果提交仍须满足已确认退出、原进程/会话一致、内容校验、当前 E2AG 结果权限和工具调用已结清。

证书轮换后，新连接必须在当前允许列表中绑定相同的持久 machine ID、scope 和 profile，且原 Attempt 的持久 peer 记录一致。machine ID 是持续身份，部署管理不得将其复用于另一台不相关机器。恢复通道不修改原 peer、不重发 Worker 执行凭据、不续期执行 Grant。

`prepare` 和结果提交的持久回执按机器身份、profile 与请求 ID 定位，记录请求摘要；客户端进程或证书变更后仍可重试同一请求。已提交结果返回原回执；已完成上传仅报告原对象的完成状态。新结果接受仍重新校验 E2AG，历史回执读取不会重做结果写入或外部动作。

真实 mTLS 和独立 CLI 进程验证了旧证书拒绝、证书轮换后的原任务恢复及重复提交去重。执行器使用合成 Adapter，模型调用和物理跨主机资格复用或由原 WBS 项承接。恢复通道验证完成后，仍须核对源控制面隔离与恢复点之外的未决副作用，才能解除全局冻结。


## 管理员确认的离线启用路径

源端隔离采用成熟的基础设施 fencing 与管理员确认职责分工。管理员核验源节点隔离、检查点后的副作用、远端再认证与资源路径后，DiOS 通过离线 CLI 检查本地完整性并同时解除双层冻结。跨库更新使用 SQLite ATTACH 的非 WAL 原子事务，崩溃重试沿用持久意图及控制版本。协议、操作和依据见[离线启用设计](installation-activation-11.md)。自动源端 fencing 继续由 Infrastructure 提供；当前接口不执行证据中的命令。
