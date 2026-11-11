# 持久运行与存储参考实现

日期：2026-11-11 · 状态：参考实现，#30/#32 进行中 · 编制及验证：Codex。

## 已实现

`backend/app/runtime` 提供显式版本迁移、SQLite/PostgreSQL 事务后端、幂等队列、Worker/Attempt 身份、租约与 fencing、阶段报告去重、取消请求/确认、撤销代次、事件与状态原子提交、独立消费确认及单调查询投影。产物使用可替换的二进制 Provider，内容寻址复用与字节摘要校验；历史导入先保存原始身份关联，标为 legacy_unverified，并不创建可执行任务。

租约过期进入 reconciliation_required；查询不会重新入队。确认旧进程停止且无未决副作用后，可信协调器才能进入 retry_wait。排空只阻止新认领，活跃任务仍可结束。进程监督独立于模型 RPC：实际退出、生命周期预算、输出上限和继承管道悬挂均有界处理，保留部分输出；查询超时不作为进程终止依据。

参考后端在 SQLite 使用 BEGIN IMMEDIATE，在 PostgreSQL 按 Workspace advisory lock 串行化短事务。该锁仅为正确性基线；生产并发设计按 [ADR 0008](../adr/0008-governance-semantics-and-provider-selection.md) 和 [实现选择方案](e2ag-runtime-implementation-options.md) 比较。SQLAlchemy 在参考适配内部使用，非 SQL 后端需实现语义接口，不以 SQL Session 作为公共边界。

## 操作入口

`cli/dios runtime --help` 提供 migrate、submit、show、list、events、drain。它是依赖数据库访问权限的本地运维入口；服务端认证和公共 API 由 #33/#34 接入。必须显式提供 `DIOS_RUNTIME_DATABASE_URL` 或 `--database-url`，不默认连接现有业务数据库。

WSL 仓库根目录示例，路径只用于独立验证库：

```sh
export DIOS_RUNTIME_DATABASE_URL=sqlite+aiosqlite:////tmp/dios-runtime-example.db
backend/.venv/bin/python cli/dios runtime migrate
backend/.venv/bin/python cli/dios runtime list
```

submit 只入队元数据。现有 FastAPI、cron、retry worker 和 Agent 入口尚未转接本模块；应用启动不会自动升级 runtime 表或启动第二套执行器。94 项回归的数据库和业务输入均为临时测试夹具，现有 Codeup/GitHub 评审继续运行。

## 迁移与恢复

Schema v1 在独立 runtime_* 表中追加，不重命名或覆盖 legacy 表；版本与冻结 Schema 源摘要记录在 runtime_schema。未知版本、校验不符、无版本残留表或缺失结构使迁移失败。重复 migrate 不重复创建数据。迁移由运维显式执行；上线前保存备份及回退点，先扩展表再切换消费者。当前没有删除字段和降级脚本；回退旧消费者时保留新表与历史。

数据库保存状态、引用和轻量事件；对象 Provider 保存大产物；Secret 内容由后续凭据 Provider 管理。存储目录由运维配置且限制为受信服务可写。未关联的上传对象可能因数据库失败成为孤儿，后续 GC 须按引用、保留期与在途上传保护处理。进度消费确认独立于产物读取；失败上传不阻断阶段事件。

历史导入当前支持来源/种类/原 ID 的幂等映射和原记录保留。真实来源清点、任务关联解析、API 历史投影、分块导入和生产对账仍属 #32/#60；来源凭据必须在导入 Adapter 中脱敏为引用。

## 验收与剩余工作

[检查报告](evidence/runtime-foundation-checks-20261111.json)：94 项测试全部通过，无跳过；Python 3.12.3、SQLAlchemy 2.0.48、asyncpg 0.31.0，WSL Linux。隔离 PostgreSQL 镜像为 postgres:16-alpine，镜像 ID `sha256:108b27c919e6e7bc124350fb265deea9adac58e118eade428c6d1ad44b90debe`。包括四个独立 OS 进程竞争认领和提交前/后崩溃；这证明夹具进程与状态语义，不表示模型三路并发或企业吞吐已验收。

复跑：在隔离环境设置 `DIOS_TEST_POSTGRES_URL` 后，仓库根目录运行 `backend/.venv/bin/python scripts/check_runtime_foundation.py --output docs/design/evidence/runtime-foundation-checks-20261111.json`。未设置 PostgreSQL URL 时报告会明确列出跳过项。

| 任务 | 剩余验收 |
| --- | --- |
| #30 | 持久定时/重叠合并、分类退避/半开恢复、实际 Adapter 接管、可核验进程与会话观测、自动结果恢复、升级排空与工具派发撤销竞争 |
| #32 | 后端中立原子接口、生产候选比较、历史关联/API 投影、详情同步与有界断网 spool、升级失败及备份恢复完整演练 |
| #33/#34 | 真实身份/Grant 签发续期、PDP/PEP、审批消费与意图提交、只读沙箱、严格撤销生效 |
| #54 | 按论文负载分解治理开销，比较锁粒度/数据布局/持久策略及后端，再验证两档容量 |

Windows 已设置隐藏子进程标志；Windows 进程树及计划任务实测待完成。当前 Run/Attempt 是内部参考存储模型，正式 v1 API、Console 和远端 CLI 消费需通过完整契约和授权回归后接入。G2 的独立安全/迁移评审人待认领，本次不标记可合并或发布。

## 实现选择进展

[六种实现比较](provider-selection-32.md) 已给出轻量与共享事务两条产品化路线。正式 Runtime 当前仍为本页参考实现；下一步接入对象粒度 PG 和轻量公平写入队列，保持统一身份、原子提交与恢复接口。SQLite/LMDB 的实验 Adapter 尚未接管业务调度。

## 后续阶段性选择

[ADR 0010](../adr/0010-lightweight-runtime-default.md) 确认 SQLite 为轻量 Runtime 当前默认，有界 FIFO 写入与基本混合查询已验收。共享状态与高可用部署按实际故障模型独立验收。下一步推进历史/API 投影、离线 spool 与恢复交付。
