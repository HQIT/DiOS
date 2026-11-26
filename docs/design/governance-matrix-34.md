# E2AG 策略、存储与故障契约

SQLite/PostgreSQL 与内置权限检查/原生 OPA 的四种组合均通过同一组 12 项治理测试，共 48 项。授权审批、持久意图和 Runtime 撤销顺序保持一致。工程证据见[资格记录](evidence/governance-matrix-20261126.json)。

#35 的持久撤销修复新增“拒绝操作回滚后仍保留撤销”场景；当前复现脚本每组 13 项、合计 52 项，结果见[持久撤销证据](evidence/revocation-durable-20261126.json)。

## 职责与顺序

内置 E2AG 检查调用者、服务、工作负载、资源四层完整权限元组，并绑定 Run、Attempt、Session、Worker 与授权版本。OPA 提供附加策略决定；两条路径均由 Runtime 负责执行绑定、审批消费和派发提交。

[SQLite 的 BEGIN IMMEDIATE](https://www.sqlite.org/lang_transaction.html)与 [PostgreSQL 的事务级 advisory lock](https://www.postgresql.org/docs/16/explicit-locking.html)分别串行化作用域内关键写入。测试使用两个独立数据库连接，分别控制撤销先提交、派发先提交的交错顺序。

- 撤销先提交：后续派发拒绝，工具调用次数为零。
- 派发先提交：审批已经消费，该调用属于在途动作，仍可能产生副作用；保留原工具结果，拒绝新动作及撤销后任务结果验收。
- 派发审计失败：事务回滚，审批与原调用身份保留；故障解除后重新检查并继续。
- 持久意图写入失败：工具调用次数为零，进入未知结果处理，重复请求不自动重放。
- 工具已返回完整回执但控制面处理失败：重新建立数据库连接和回执服务，从原回执恢复；工具仅调用一次。
- 授权服务不可用：暂停派发；恢复后沿用原审批和调用绑定，重新检查当前授权。

独立连接在工具调用入口核验已提交的派发状态、单次消费及审计记录，同时检查本地持久意图的绑定、参数摘要和派发版本。

## 资格矩阵

| 存储 | 策略路径 | 通过 | 故障与顺序 |
| --- | --- | ---: | --- |
| SQLite | 内置权限检查 | 12/12 | 审计、意图文件、策略文件、回执恢复、撤销竞争 |
| SQLite | 原生 OPA | 12/12 | 同上；暂停/恢复 OPA 服务 |
| PostgreSQL 16.13 | 内置权限检查 | 12/12 | 同上；独立连接验证事务可见性 |
| PostgreSQL 16.13 | 原生 OPA | 12/12 | 同上；暂停/恢复 OPA 服务 |

PostgreSQL 的 `fsync`、`synchronous_commit`、`full_page_writes` 均为 `on`。OPA 镜像、策略包版本、数据库镜像及每项任务身份保存在资格记录中。完整权限元组另含 10 组允许/拒绝输入，每种组合均与预期一致。

相关 Worker/工具夹具回归 27 项通过，15 项可选 Docker Worker 测试跳过；[Cursor 原生控制证据](evidence/cursor-native-controls-20261126.json)及[原生 OPA/MCP HTTP 证据](evidence/approval-qualification-20261124.json)沿用已通过结果。

## 适用范围与后续工作

本矩阵在单台 Linux 主机使用记录型工具传输，OPA 与 PostgreSQL 为独立原生容器。重建控制组件验证回执恢复；主机断电与物理跨主机故障另有资格项。运行代码未变，开发服务继续使用已部署版本。

[OPA Data API](https://www.openpolicyagent.org/docs/rest-api)返回策略决定及版本信息。独立上游策略更新与 Runtime 提交仍属于两个写入域，本矩阵验证 Runtime 内的撤销事务顺序。身份源事件的重复、乱序、撤销后再启用，以及跨来源传播水位由 K03-3.01 承接；工具治理独立安全评审保留 K03-2.03。

## 复现

在仓库根目录运行；环境需有后端依赖、Docker、资格记录指定的 OPA 镜像及 `postgres:16` 本地镜像。脚本创建专用临时容器、随机本地端口及合成数据，结束时清理其自建容器和卷。

```bash
backend/.venv/bin/python scripts/qualify_governance_matrix.py \
  --output workspace/governance-matrix/matrix.json
```
