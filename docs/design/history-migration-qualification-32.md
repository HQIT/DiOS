# 数据迁移与历史导入完整性

DiOS 的存储升级采用独立版本化组件和事务迁移；历史导入保存来源身份、原始字段及显式关联。历史中的成功状态保留为 `legacy_unverified`，执行队列和新版任务验收状态独立管理。

SQLite 迁移利用[原生事务原子性](https://www.sqlite.org/atomiccommit.html)，一致副本利用[Online Backup API](https://www.sqlite.org/backup.html)。迁移失败后重新打开原副本并重试；导入先校验内容对象，再以单个任务关联组提交身份与关系。事务回滚后已复制的内容对象可供重试复用。

## 验收映射

| 原验收 | 工程证据 |
| --- | --- |
| A01 旧数据与关联可用 | 有原任务、Attempt、Session、Artifact 和导入记录的 Runtime v1 副本迁移成功；RuntimeStore 与 HistoryQueries 返回原身份、关系和产物摘要；CLI 导出、检查、导入和查询通过 |
| A02 不兼容拒绝与失败恢复 | 不兼容版本、错误校验和、缺表及未登记组件被拒绝；子进程写入迁移版本记录后被 SIGKILL，重开副本与迁移前逻辑快照完全一致，随后迁移及重复迁移成功 |
| A03 分层迁移与路径独立 | Runtime v1 和 History 组件分别登记；旧库复制至新的临时路径后升级，原库不变；完整历史内容随备份恢复至新根目录 |
| A04 托管及远端引用保留 | 原生 Attempt 的远端引用、空协议版本、缺失能力字段保持原状；托管执行原始记录新增的未知列及不透明 runtime handle 完整归档 |
| A06 来源关联、旧成功与零执行 | 两个 Attempt 共享 Session/Artifact 的所有来源保留；导入在首次关系写入后被 SIGKILL，身份与关系整体回滚，重试及重复导入保持相同映射；无原生 Run 和执行事件，托管 Adapter 启动次数为零 |

[可复核证据](evidence/history-migration-qualification-20261126.json)记录 27 项全部通过、零跳过的专项测试结果、进程中断场景、身份及源文件摘要。既有专项还覆盖大段中文、WAL 已提交数据、内容损坏、来源变更冲突、跨作用域隔离、未决任务排除及备份恢复。活动源任务保持原状态。

## 运行范围

本资格使用隔离 Linux/SQLite 数据库、合成历史和真实操作系统子进程。强制终止覆盖迁移及导入事务中的指定写入点；物理断电和多节点持久性按 W4 验证。外部 Codeup 评审源的字段映射及生产切换由 #60 接续，现有评审持续运行。

```bash
backend/.venv/bin/python scripts/qualify_history_migration.py \
  --output workspace/history-migration-qualification.json
```

该命令仅操作临时数据，并输出测试结果与证据摘要。源库版本不兼容时先保留原库，在干净副本恢复受支持版本；未知远端引用不自动取得执行资格，后续执行继续经过 E2AG 授权与对账。
