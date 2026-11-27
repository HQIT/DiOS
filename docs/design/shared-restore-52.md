# 共享后端干净恢复资格

固定 PostgreSQL 16 镜像和单控制面 POSIX 产物卷的整套备份已在独立空环境恢复。112 张 Runtime 表、111 行夹具数据和 45 个文件保持一致，原 Run、Attempt、Session、E2AG 授权及四条产物引用完整保留。兼容服务库、配置、证书文件与 Worker spool 一同还原。

## 恢复机制

数据库使用官方 [pg_dump custom archive](https://www.postgresql.org/docs/16/app-pgdump.html)，以只读备份账号生成快照；空目标使用 [pg_restore 单事务恢复](https://www.postgresql.org/docs/16/app-pgrestore.html)，由独立 owner 身份创建表、约束和触发器。运行身份继续仅持 DML 权限，备份身份不能写入。

任务自然完成并排空后，停止网关、Worker 及其他写入者，再捕获数据库与文件；文件捕获复用现有目录句柄、摘要及变更检测。恢复目标使用独立数据库容器、卷与新目录，源数据库停止。逐表规范化摘要与文件清单全部相等后，沿用已有恢复变换设置全局冻结、撤销旧执行凭据，并冻结兼容服务库。

恢复全过程不启动目标调度器或执行器。共享后端解除冻结与 HA 接管属于 D01-3.03；当前 SQLite 双库启用命令不适用于 PostgreSQL。证书、路径及外部资源需要管理员按恢复映射重新配置后才可发布目标。

## 实测结果

| 条件 | 结果 |
| --- | --- |
| 两路真实 Worker | 独立 PID、容器进程和模型会话；mTLS/E2AG 受控执行后完成 |
| 空环境完整恢复 | 112 表全量摘要及 45 个文件匹配；任务历史和产物身份保持 |
| 截断数据库备份 | 单事务恢复失败，目标仍无表，随后正常备份可恢复 |
| 已有目标重复恢复 | 明确失败，原目标数据完整保留 |
| 产物损坏 | SHA-256 校验拒绝，原备份内容不变 |
| 对象和数据库权限 | Alice 可读取，Bob 被拒绝；运行账号不能 DDL，备份账号不能写入 |
| 旧执行凭据 | 撤销并递增版本，撤除旧凭据宽限 |
| 未经确认恢复执行 | 新提交、领取及普通 resume 均被恢复冻结拒绝 |

[工程证据](evidence/shared-restore-20261127.json)包含镜像 ID、逐表摘要、实际进程/会话与源码摘要。联合场景 1 项通过、零跳过；无真实模型调用。两个 Worker 的执行发生在备份前，恢复后验证历史、权限、产物及冻结。单次恢复和核验耗时 12.065 秒，负载为上述小型夹具；企业 RPO/RTO、复制和容量继续沿 D01-3/D01-4 验收。

## 复现

```sh
backend/.venv/bin/python scripts/qualify_shared_restore.py \
  --output workspace/shared-restore-NEW
```

需要已有 PostgreSQL 16 和 `dios-worker-fixture:local` 镜像、Docker 与 OpenSSL。脚本创建隔离容器和卷；输出目录包含受保护配置、密钥及备份，权限为 0700。公共证据仅包含摘要和运行身份；按各子目录的 `installation.json` 定位本次资源。操作步骤沿[共享部署手册](../../deploy/shared/README.md)。
