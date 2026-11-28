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

SQLite 和 PostgreSQL 的本机独立进程场景已通过，详见[原证据](evidence/resource-recovery-20261127.json)。WSL/H200 双机联合场景 2/2 通过，控制端使用 SQLite，原始记录见[双机证据](evidence/resource-recovery-h200-20261128.json)。两端 Linux boot 摘要不同，backend 源码和工作负载镜像摘要一致，最多两路实际并发；没有付费模型调用。

| 联合场景 | 已通过断言 |
| --- | --- |
| 两路执行、Worker 退出与恢复 | 共享并发与预算准入；第三项排队；未知执行不释放预留；自然租约到期后旧心跳返回 STALE_OWNER；同 Attempt/Session 原结果恢复；四次同键结算仅一份回执 |
| 控制通道中断、重连与撤权 | 原生 Worker 自动对账；中断任务按单次尝试策略确认失败，未结算预留继续保留；离线期间撤权的排队任务没有 Attempt；结算后新任务成功，旧心跳拒写 |

第二个场景的预算为 3、每次预留 2：失联时占用 2，另一任务不能领取；原尝试确认失败并以合成凭据结算 1 后，新任务可预留 2，最终为预留 2、已结算 1、可用 0。预算开放后已撤权任务仍无 Attempt，撤权拒绝与预算不足分别得到验证。

测试仅切断本次 mTLS 通道，未操作宿主网络或山河服务；管理单元和测试容器已回收。Rootless Docker 镜像层使用现有临时缓存，本场景验证执行与预算语义。E2AG 的准入、恢复与撤销沿用现有实现，本轮无需修改授权核心。

旧控制面主节点隔离与复制提交仍需 [HA 故障环境](../../deploy/ha/qualification.md)，按既定节点级故障暂停安排保留待验；K04-2.04 未结项，整体 61/79。供应商账单及费用硬上限按既定能力条件处理。


并发准入、预算及结算沿用[共享执行额度](execution-capacity-45.md)和[消费预算](consumption-budget-45.md)的现有设计。本场景补充故障联合证据，不新增调度器或账本。

## Rootless Docker 与网络故障入口

原生 Python 管理进程连接已有 [rootless Docker socket](https://docs.docker.com/engine/security/rootless/tips/)，工作负载仍按同一 Profile 在受限容器执行；[Toxiproxy](https://github.com/Shopify/toxiproxy)只干预本次控制通道。

```bash
backend/.venv/bin/python scripts/qualify_resource_recovery.py \
  --ssh-config /path/to/verified/ssh_config --host physical-worker \
  --remote-source /path/to/backend --remote-python /path/to/venv/bin/python \
  --remote-docker-host unix:///run/user/UID/docker.sock \
  --toxiproxy /path/to/verified/toxiproxy-server \
  --output workspace/resource-recovery-network
```

每个管理进程由独立 systemd 用户单元托管，限制 512 MiB、64 个线程/进程和 240 秒运行时间。结束时停止本次单元，只回收随机 scope 的容器；现有 daemon 配置保持原状。API 与工具访问仍由 mTLS/E2AG 校验。
