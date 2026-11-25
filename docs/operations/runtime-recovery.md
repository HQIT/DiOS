# Runtime 备份、恢复与排空

格式 3 支持显式配置文件、逐表摘要、逻辑引用校验和外部恢复依赖。命令与覆盖范围见[一致性备份清单](../design/backup-inventory-11.md)。

包含兼容服务库、工作目录和 Worker spool 的实例使用 `runtime installation-backup` / `installation-restore`。安装级恢复将两层入口共同冻结，撤销副本内旧 Worker 凭据与工具 Grant；spool 保留待对账。详见[安装级干净恢复](../design/backup-inventory-11.md#安装级干净恢复)。

[历史归档](../design/managed-history-10-30-32.md)登记的内容对象随恢复包校验、复制和恢复。缺失归档内容阻止发布完整备份；使用该组件后保留支持 runtime_history_archive_schema 的备份工具。

含兼容 A2A、聊天共享服务与 Worker 的安装，使用[统一升级排空](../design/managed-upgrade-10-30.md)关闭全部执行入口并核对两侧状态。本文的 Runtime 命令只控制运行内核。

状态：开发分支基线，待 G2 联合评审。责任人：Codex；独立评审人：待认领。
日期：2026-11-12。关联：#11 / K02-3、#30、#32；依赖：ADR 0010、Runtime/History Schema v1。

## 当前交付

SQLite 状态库和本地内容寻址产物组成一致恢复点。作用域排空适用于 SQLite 与 PostgreSQL，恢复包命令当前支持 SQLite / LocalArtifacts。原始数据库、Run/Attempt 身份、事件、历史映射及关联保留；恢复副本以 `restore_hold` 模式发布。

这些控制点用于当前 RuntimeStore 的调用者。既有 cron/评审执行器及旧版本客户端尚未消费新状态，部署切换须先隔离旧调度入口；真实 Worker、跨主机认证和完整 E2AG 工作授权由 #30/#33/#34 联合接入。本轮演练只使用临时库与合成数据。

## 作用域排空

`runtime_lifecycle` 持久保存作用域模式和修订号，与 claim / submit 共用作用域事务排序。`draining` 拒绝新提交和新认领；同幂等身份重试仍返回原 Run。新注册 Worker 同样受限制。活跃 Attempt 的心跳、阶段报告和结果提交继续生效，排空本身不终止进程。

`upgrade status` 返回活跃 Attempt 数、待对账数、队列数、事件数和观测时间。只有 draining 且活跃及待对账数均为零时，`ready_for_cutover` 为真。事件数是快照统计，不作为全局事件消费游标；下游仍按独立事件身份确认。

等待超时返回 `DRAIN_TIMEOUT`，查询耗时超过等待预算返回 `DRAIN_OBSERVATION_TIMEOUT`。两者均保持排空模式，不判定任务死亡、不重派、不强制结束。租约过期进入待对账后继续阻止切换。回退升级计划时，运维沿用当前修订号显式 resume；旧修订号被拒绝。

```sh
export DIOS_RUNTIME_DATABASE_URL=sqlite+aiosqlite:////path/to/runtime.db
backend/.venv/bin/python cli/dios runtime --scope local upgrade drain
backend/.venv/bin/python cli/dios runtime --scope local upgrade status
backend/.venv/bin/python cli/dios runtime --scope local upgrade wait --timeout 30
# 取消本次切换计划时，填入 status 返回的 revision。
backend/.venv/bin/python cli/dios runtime --scope local upgrade resume --revision 1
```

以上操作仅排空指定 scope。整控制服务升级需要冻结入口并排空全部受影响 scope，停止旧消费者，保存最终对象和消费确认；进程/会话实际退出证据与离线报告排空由 Worker 生命周期负责。当前账本判据不替代该部署级检查。

## 恢复包

`backup` 使用 SQLite backup API 获取已提交的一致快照，包含 WAL 已提交内容。快照转为独立数据库文件，按快照内引用复制不可变对象，校验每个对象的字节数与 SHA-256。之后新增的数据库记录与产物留在后续恢复点。备份期间应暂停对象 GC，保证快照引用的产物可读；缺失或损坏对象使备份失败。

包内包含 `state.sqlite`、`objects/<sha256>` 和最后写入的 `manifest.json`。清单保存格式版本、各组件版本/校验值、数据库摘要、对象清单、记录数和时间；核验同时检查 SQLite 完整性与外键。摘要用于完整性核验，来源真实性由备份存储权限或后续签名机制保障。

目标目录必须不存在。目录以 POSIX 0700、文件以 0600 创建，Windows ACL、离机加密和访问审计由部署环境配置。数据库可能含敏感信息，备份目录应使用受保护存储。失败时保留不完整目录供诊断；缺少清单的目录不接受恢复。超时参数控制快照复制与对象传输预算，完整性检查耗时也需纳入部署窗口。

```sh
backend/.venv/bin/python cli/dios runtime backup --artifacts /path/to/objects --destination /path/to/new-backup
backend/.venv/bin/python cli/dios runtime backup-verify /path/to/new-backup
```

恢复集范围：整个指定 SQLite 文件、该快照内已登记的本地产物、所有已保存的外部引用。未引用的对象不复制。外置配置文件、workspace 文件、Worker 各自的 spool、Secret 服务及远端 Agent 自有数据需要独立的备份与重认证安排；#11 持续跟踪这些交付项。PostgreSQL 备份返回明确的 Provider 不支持错误，后续接入原生备份工具。

## 干净目录恢复

```sh
backend/.venv/bin/python cli/dios runtime restore /path/to/new-backup /path/to/new-instance
```

恢复前验证清单、数据库和全部引用对象。复制时再次校验字节摘要，目标目录和文件独占创建。数据库先写入 `restore.pending.sqlite`，完成组件迁移与全局调度关闭后，才发布为 `state.sqlite`；迁移失败不发布可启动文件。原实例和备份不被改写。

恢复副本允许读取、核验原身份及事件，拒绝所有 scope 的新任务认领和新提交，也拒绝旧 Worker 的心跳、报告和结果提交。返回的 `restore.json` 记录恢复耗时、来源与恢复后数据库摘要及核验要求。原 scope 的 resume 无法解除全局 `restore_hold`。

恢复副本启用前必须隔离源控制面、重新认证外部引用，并核对恢复点之后已发生的任务与副作用。即使备份中某任务仍显示 queued，也可能已在原实例执行，需先对账才能再次调度。全局冻结保留到源端隔离与未决事项核对完成。

### 重新认证与原结果对账

恢复实例开启既有 mTLS gateway 后，显式允许的机器可通过 `runtime.restore.*` 对账。当前机器证书、scope/profile 和原 Attempt 的 machine ID 必须一致；证书轮换使用当前身份配置。通道仅在 `restore_hold` 下接受证据，E2AG 按当前策略校验 `accept_result`，不续租或启动工作。

```sh
backend/.venv/bin/python cli/dios runtime --scope local restore-reconcile \
  --control-file /srv/worker/recovery-control.json \
  --profile-file /srv/worker/profile.json \
  --receipt-file /srv/worker/original-exit-receipt.json \
  --artifacts /srv/worker/objects --expected-version 12
```

`expected-version` 来自恢复实例中的 Run。版本绑定的任务同时指定 `--revision-ref`。输入为原 Worker 保存的退出回执和内容对象；CLI 校验完整结果后分块上传，按原 Run/Attempt/进程/会话关联提交。丢失响应后使用相同参数重试，可读取原回执。截断输出、未确认退出或含失败标记的结果保持待核对；普通 Worker 循环不能借该通道重新执行。

对账成功保留全局冻结。后续启用须另外证明源控制面已隔离及未决外部动作已处理；旧副本本地的 hold、撤销记录和 epoch 不代替源端隔离证据。详见[研究依据与通道约束](../design/recovery-research-11.md)。

## 验收记录

[完整后端回归](../design/evidence/recovery-drain-regression-20261111.json)：新增 18 项检查，合计 154 项通过、0 跳过。作用域排空同时覆盖 SQLite / 隔离 PostgreSQL，恢复包覆盖 SQLite。包括领取/排空竞争、新 Worker、自然完成、重启、待对账与观测超时、WAL 一致性、内容损坏、目标防覆盖、失败不发布、恢复旧所有者拒绝和 CLI。

[恢复演练](../design/evidence/recovery-drill-20261111.json)：合成库 139264 字节、产物 19000 字节，包含 2 个 Run、1 个 Attempt、8 条事件和历史关联。备份 0.028 秒、恢复 0.064 秒，恢复并查询核验 0.097 秒。事件摘要、产物字节及身份关联一致。该小数据演练提供可复现功能证据，企业 RTO/RPO 需按部署规模验证。

复跑：`backend/.venv/bin/python scripts/check_runtime_recovery.py --output docs/design/evidence/recovery-drill-20261111.json`。全后端回归沿用 `scripts/check_runtime_foundation.py`，设置隔离 `DIOS_TEST_POSTGRES_URL` 后可覆盖 PostgreSQL 排空路径。

下一主项回到 #30 真实 Worker 的持久调度、进程/会话观测与对账，结合 #33 凭据和 #34 E2AG 授权；#11 的完整交付集与多节点恢复按这些前置能力继续完善。
## 恢复就绪观察

```bash
cli/dios runtime installation-readiness /path/to/restored-installation --sample-limit 20
```

命令只读观察 Runtime、兼容服务和捕获的 Worker spool，覆盖全部 scope，输出冻结状态、未结任务/尝试、未决工具调用、未应用结果、旧凭据和待补报数量。样例上限不改变总数；输出不含密钥、消息正文和模型结果。

`local_execution_settled` 表示双层冻结仍生效且本地待处理项为零。`ready_to_activate` 始终为 `false`：源控制面和执行器隔离、恢复点之后的副作用、远端重新认证及资源路径仍需核验。该命令不解除冻结、不操作源端进程、不补发消息。各组件使用独立读事务，观察不构成跨库一致检查点。
