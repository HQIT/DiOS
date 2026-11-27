# 共享预算与 Worker 故障恢复

共享并发额度约束活动及结果未知的 Attempt；消费预算在领取前预留，只有执行状态明确且结算证据可用时才核销。Worker 失联、退出和租约到期均保留未结算预留。E2AG 继续约束原工作、会话、执行权和结果恢复。

## 联合场景

三个任务共用并发上限 2、消费预算 4、每次尝试预留 2。两个独立远程协议 Worker 运行真实容器；第三个 Worker 无法领取第三项任务。控制面与 Worker 经 mTLS 通信，Worker 不持有数据库连接。

第一个 Worker 被 SIGKILL 后，任务容器完成输出。第二个任务正常结束，但两个尝试的消费预留仍保留。自然租约到期后，第一个任务进入对账，原所有者心跳被拒绝；新 Worker 恢复同一 Attempt、会话及完整结果，不重新启动任务容器。恢复后的用量仍只有原尝试的一份记录。

同一结算键并发重放四次，只形成一份结算。更换键再次结算被拒绝。该账本最终为预留 2、已结算 1、剩余 1，第三项任务继续排队，未创建 Attempt。

| 断言 | 证据 |
| --- | --- |
| 实际两路并发 | 两个 Worker PID/实例、两个容器进程实例及会话 |
| 不确定执行保留占位 | 自然租约到期后的 `reserved_attempts=1`、`unresolved_attempts=1` |
| 失联与正常完成均保留消费预留 | 两个尝试合计预留 4，未结算前不能再领取 |
| 旧所有者拒写 | 原凭据及绑定在租约到期后被拒绝 |
| 原结果恢复 | 相同 Attempt/Session，仅一份用量记录，新增执行为零 |
| 幂等结算 | 四次相同键返回同一回执，第二个键返回已结算 |
| 用量与账本关联 | Run/Attempt/Session、对象摘要、用量帧和结算回执对应 |

用量为容器输出的合成上报值，验证身份关联和核销机制。模型供应商账单、Token/金额硬上限不在该场景中；严格上限继续由既有准入规则拒绝不具备相应能力的 Harness。

## 执行入口

```bash
backend/.venv/bin/python scripts/qualify_resource_recovery.py \
  --local --output workspace/resource-recovery-sqlite

# DIOS_TEST_POSTGRES_URL 指向隔离测试库。
backend/.venv/bin/python scripts/qualify_resource_recovery.py \
  --local --postgres --output workspace/resource-recovery-postgres

backend/.venv/bin/python scripts/qualify_resource_recovery.py \
  --postgres --ssh-config /path/to/verified/ssh_config --host physical-worker \
  --remote-source /path/to/backend --runtime-image sha256:VERIFIED_IMAGE \
  --output workspace/resource-recovery-two-hosts
```

本地需已有 `dios-worker-fixture:local` 镜像。双机模式分别在控制端和第二节点启动 Worker，复用 D01-3.02 的 SSH/mTLS 环境；开始前比较 Linux boot 摘要、backend 源码和工作负载镜像。远端运行镜像须包含当前锁定依赖，目录只用于资格测试。回收仅限本次随机 scope。

## 当前资格

SQLite 和 PostgreSQL 的本机独立进程场景已通过，详见[逐项证据](evidence/resource-recovery-20261127.json)。第二台物理主机当前不可达，双机分支尚未实测。网络分区、旧控制面主节点隔离、宿主机故障及供应商实际账单继续按 K04-2.04 原验收推进；本项保持待验证，整体 60/79。

并发准入、预算及结算沿用[共享执行额度](execution-capacity-45.md)和[消费预算](consumption-budget-45.md)的现有设计。本场景补充故障联合证据，不新增调度器或账本。
