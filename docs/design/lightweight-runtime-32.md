# #32：轻量默认与有界写入

日期：2026-11-11 · SQLite 阶段性默认已接受 · #32 进行中。

## 结果与实现

SQLite 是当前轻量默认后端，LMDB 保留候选，共享状态和高可用部署按具体拓扑验收。[ADR 0010](../adr/0010-lightweight-runtime-default.md) 记录选择依据和适用范围。

正式 Runtime 的 Database 已接入进程内 FIFO 写入入口。所有 Workspace 共用同一实例的写入口，一个执行者、64 个等待位置、5 秒排队时限，事务外等待不占数据库连接。WRITE_QUEUE_FULL、WRITE_QUEUE_TIMEOUT 和 WRITE_DRAINING 提供稳定拒绝码。超时与取消会释放等待位置，包括授予执行权与取消同时发生的情况。

写入等待属于易失准入；Run 受理以数据库事务提交返回为准。进程退出或响应丢失后，调用者沿用原幂等身份重试/对账。该队列不会替代已落库的逻辑任务队列。

Run 与 Attempt 查询改用只读一致性快照，不进入 SQLite 写队列；query_only 防止读连接误写并在归还前复位。SQLite 显式使用 FULL 同步提交，原 schema、事件与状态原子性保持不变。当前标准库驱动 SQLite 3.45.1 沿用已有 journal 模式；WAL 引擎升级独立验证。

Database.close 停止接收新写入，等待已接收的写入结束后释放连接池。排队超时覆盖等待阶段，正在执行的事务由已有提交/回滚机制负责。读请求、进程和真实 Agent 的完整服务排空由 #30 生命周期层统一处理。

## 基本验收

新增 8 项针对性检查覆盖 FIFO、满队列、超时清理、排队取消、授予与取消竞争、执行中取消、排空拒收、只读快照/连接复用及同身份重试。全后端回归 **102 项通过，0 跳过**，包含隔离 PostgreSQL 与原有独立进程恢复检查。[回归记录](evidence/lightweight-runtime-regression-20261111.json)。

混合查询直接调用正式 Runtime：每个用例预置 200 个 Run，每秒 25 次 submit、10 次 get/事件页查询，3 轮、每轮 5 秒。读写 P99 门槛均为 1500 ms，要求零错误、唯一任务身份、事件总数与摘要一致。新路径结果如下：

| 轮次 | 写入 P99 ms | 查询 P99 ms | 写入/查询数 | 错误 |
| --- | ---: | ---: | ---: | ---: |
| 1 | 92.06 | 50.26 | 125/50 | 0 |
| 2 | 55.292 | 39.596 | 125/50 | 0 |
| 3 | 50.784 | 39.211 | 125/50 | 0 |


同一脚本保留旧准入路径对照，共 6 个用例。此处是短窗口工程冒烟门槛，环境未实施资源隔离和长期稳态控制，不作为企业容量或数据库性能排名。逐轮数据与配置见 [混合查询记录](evidence/lightweight-runtime-mixed-20261111.json)。

复跑：仓库根目录执行 `backend/.venv/bin/python scripts/check_lightweight_runtime.py --output docs/design/evidence/lightweight-runtime-mixed-20261111.json`。该脚本仅使用临时 SQLite 文件，输入全部为合成数据；生产业务、历史和调度器没有切换。

该轮回归中记录的子进程 transport 回收问题已由后续 [Worker 报告与生命周期增量](worker-report-spool-30-32.md) 补充清理和回归检查。

## 下一步

[有界 Worker 报告 spool](worker-report-spool-30-32.md) 已接入 RuntimeStore 受信接收接口和 CLI 状态查询，[历史关联投影](history-query-32.md) 已交付受信查询接口与 CLI，[恢复与排空基线](../operations/runtime-recovery.md) 已完成首轮演练。下一主项为 #30 真实 Worker 的持久调度与恢复，#34 继续完整 E2AG 治理。跨主机 Worker 通过认证 API 调用控制服务，多副本控制面按共享事务与故障模型验收；当前不扩大数据库候选实验。
