# SQLite 到共享后端的完整迁移

迁移以已校验的 Runtime 备份包为输入，向同版本、空 PostgreSQL 数据库复制全部表及内容对象。任务、尝试、会话、产物、授权、审批、来源、版本、幂等键和未知业务 JSON 字段保留原值；目标库始终保持 `restore_hold`，源库继续运行。

## 工程选择

SQLite [Online Backup API](https://www.sqlite.org/backup.html)提供数据库一致快照。现有 DiOS 备份同时校验内容对象、逻辑关联、工具回执和显式配置清单，迁移复用这一格式。

[pgloader](https://pgloader.readthedocs.io/en/latest/ref/sqlite.html)支持 SQLite→PostgreSQL，其默认配置会重建目标表，部分模式会移除再恢复约束。DiOS 已有冻结 Schema、校验和与不可变记录触发器，因此使用现有 SQLAlchemy Core 的[类型反射](https://docs.sqlalchemy.org/en/20/core/reflection.html)、参数化批量 INSERT 和 PostgreSQL 原生事务，目标结构仍由 DiOS 的版本迁移创建。

没有新增队列、数据库或迁移协调服务。迁移入口只处理离线副本；持续增量复制和生产切换仍由相应交付项承接。

## 数据与执行边界

| 对象 | 迁移行为 |
|---|---|
| 所有 Runtime 表 | 检查表、列、类型、主键和版本；逐行规范化摘要校验。表、行或字段缺失均失败 |
| Run / Attempt / Session | 原 ID、状态、epoch、远端 handle 和历史事件保留，不转换成新的执行任务 |
| E2AG 工作、审批、权限与 ACL | 保留原记录；目标主体、资源版本和到期条件继续生效 |
| 活跃 Worker 凭据 | 按既有恢复规则撤销，版本加一，撤去旧凭据宽限；不重新签发 |
| 生命周期 | 与数据复制在同一个 PG 事务内设置全局 `restore_hold`；派发继续受既有门禁约束 |
| 内容对象与工具回执 | 先校验、复制并 fsync，再提交引用；相同内容复用。损坏和同名冲突不覆盖 |
| 外部配置、Secret、Provider、远端执行 | 保留原引用与恢复依赖；显式配置随包复制，Secret 明文不由迁移器生成或注入 |
| 旧服务数据库及业务系统 | 使用独立交付与备份流程；本入口仅迁移 Runtime 存储 |

源和目标全表摘要使用类型化数据与稳定 JSON 编码，保留重复行，不依赖两种数据库的文本排序规则。只允许全局恢复状态及活跃 Worker 凭据发生上述确定变换；这两类记录的恢复更新时间允许变化，原表与目标表摘要分别保留，其他字段参与精确比较。

目标全部 Runtime 表在同一事务内锁定，保留原外键与不可变触发器。中断导致整批数据回滚；已校验的内容文件可以复用。重复执行逐表验证已完成结果，返回 `already_transferred`。目标存在其他数据、源备份改变或目标已产生新的审计记录时拒绝合并，不覆盖历史。

备份登记包含 Studio 与技术晋级两组表，并检查其不可变触发器。旧版本备份应先在 SQLite 副本升级到目标版本，再生成新备份；未知扩展列不丢弃，需先明确对应的版本迁移。

## 操作

1. 从源端生成新备份。在线能力评估可以继续让源端运行；正式切换使用排空后的最终快照。

```sh
cd backend
.venv/bin/python -m app.runtime.cli backup \
  --artifacts /opt/dios/data/objects --destination /srv/dios-transfer/source \
  --config-file policy.json=/opt/dios/config/work-policy.json
.venv/bin/python -m app.runtime.cli backup-verify /srv/dios-transfer/source
```

源连接由受保护的 `DIOS_RUNTIME_DATABASE_URL` 提供。源数据库及目录不作为目标目录。

2. 按[共享部署配置](../../deploy/shared/README.md)准备隔离 PostgreSQL 数据库，使用同版本迁移命令初始化空 Schema。目标不启动 Worker、调度或模型进程。用受保护环境文件设置 `DIOS_RUNTIME_TRANSFER_DATABASE_URL` 和 `PGSSLROOTCERT`；目标 URL 使用 `postgresql+asyncpg` 与 `ssl=verify-full`。
3. 在仓库根目录执行计划检查，再执行迁移：

```sh
backend/.venv/bin/python scripts/transfer_runtime_provider.py \
  --bundle /srv/dios-transfer/source --destination /srv/dios-transfer/target \
  --row-limit 300000
backend/.venv/bin/python scripts/transfer_runtime_provider.py \
  --bundle /srv/dios-transfer/source --destination /srv/dios-transfer/target \
  --row-limit 300000 --apply
```

默认每表上限 100,000 行，可显式提高至 1,000,000 行；超限直接拒绝，不截断数据。当前批量校验将受限行集保存在内存，较大历史需据行大小预留内存。本次完整副本包含超过 23 万条远程幂等回执，使用 300,000 行上限。

4. 目标目录的 `objects` 保存内容与回执，`configuration` 保存显式配置，`transfer-*.json` 保存全表摘要、恢复变换与外部依赖。重复执行同一命令验证幂等；恢复前先停止持有目标产物目录的 API/网关进程，维护锁会拒绝活动目录。
5. 通过只读查询核验历史与产物，验证原有许可和拒绝路径。恢复执行前仍需原控制面隔离、远端状态对账、Worker 凭据和当前授权复核。该步骤不会自动释放 `restore_hold`，也不会接管现有 Codeup 评审。

## 验收证据

[D01-2.02 工程报告](evidence/provider-transfer-20261126.json)记录完整开发副本的表数、行数、对象数、每表校验和、重复执行结果及隔离部署检查。原开发数据和执行记录保持连续。

专项覆盖完整身份/历史关系、中文产物、两主体 ACL、旧 Worker 凭据撤销、暂停派发、已有目标拒绝、未知列拒绝、内容损坏、源变更和事务提交前 SIGKILL。原 SQLite 备份恢复回归复用同一恢复门禁。迁移不调用模型。

D01-2.03 继续验证共享部署整体备份在空环境的恢复；物理跨主机故障、容量与发布接受按各自 WBS 保留。
