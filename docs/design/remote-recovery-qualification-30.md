# 远程 Worker 并发恢复与 E2AG 裁决

DiOS 对共享模型资源实施持久熔断、单路试恢复及逐任务授权校验。限流或额度恢复后，探测成功关闭熔断，原队列可继续并行执行；其他模型资源拥有独立熔断状态。同一 Work 的重试创建新 Attempt 和新 Grant，旧执行代次无法写入当前结果。

这与 [Azure Circuit Breaker 模式](https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker)及 [Resilience4j 的半开探测限制](https://resilience4j.readme.io/docs/circuitbreaker)一致。DiOS 的状态归属在服务端持久账本，执行资格继续经过 E2AG；结果未知时先对账，具有未知工具副作用的任务不自动重放。

## 原生执行资格

[工程证据](evidence/remote-recovery-qualification-20261126.json)覆盖两组故障：`PROVIDER_RATE_LIMITED` 与 `PROVIDER_QUOTA_EXHAUSTED`。每组使用真实 mTLS 控制通道、独立 CLI Worker 进程、固定摘要的 Docker 执行镜像和相互独立的本地产物目录。

| 阶段 | 可核验结果 |
| --- | --- |
| 模型资源故障 | 原生任务输出显式错误，原 Run 进入 `retry_wait`，保留第一次 Attempt；服务端时钟决定冷却到期 |
| 三路争抢探测 | 配置允许三路执行，只有一路领取半开探测；另外两路返回空闲，实际进程与会话数为一 |
| 无关资源执行 | 探测尚未完成时，另一模型资源的任务正常运行；同一作用域可观察到两个独立进程及会话 |
| 探测成功 | 原 Run 成功，保留原 Work，两个 Attempt 对应两个不同 Grant；熔断关闭 |
| 旧代次回写 | 原 Worker 身份通过 mTLS 提交旧 Attempt 的事件，返回 `STALE_OWNER` |
| 恢复并发 | 三个排队任务由三个独立 Worker 进程领取；容器 PID、进程实例和会话各不相同，随后全部完成 |

故障由隔离的协议夹具注入，未消耗真实模型额度。并发根据实时观测的进程和会话确认。资格范围是单 Linux/WSL 主机上的远程协议与多进程执行；物理跨主机故障属于 K01-2.04。

## 不变量与提交顺序

| 验收要求 | 对应证据 |
| --- | --- |
| K01-2-A02 幂等与未知结果 | 断网后的完整结果从原 Attempt 恢复，完成确认丢失不重复生成产物；查询超时保留探测所有权，未知工具结果阻止自动重试 |
| K01-2-A05 分类恢复与旧所有者拒绝 | 两组原生恢复与旧代次远程拒写；取消、撤销、期限失效、排空及尝试预算均限制自动恢复。重启后保留熔断账本；进程退出与悬挂 RPC 证据见[恢复组合](recovery-combinations-30.md) |
| K01-2-A07 撤销与派发竞争 | 复用[治理矩阵](governance-matrix-34.md)及[持久撤销复核](evidence/revocation-durable-20261126.json)：撤销先提交则无外部调用；派发意图先提交允许已有回执归档，随后撤销阻止新的执行与成功接收 |
| K01-2-A08 无关执行与热点不变量 | 单路探测与其他账户并行，恢复后三路并行；本地写入队列验证 FIFO、有界容量、超时移除、取消与排空；审批只消费一次及旧授权拒绝沿用治理矩阵 |

矩阵复用前核对当前事务、授权、派发和队列代码与持久撤销验证版本一致。独立身份源与 Runtime 仍具有各自的事务边界。队列公平性限定于一个控制服务的 SQLite 写入入口，全局多控制面公平性与容量目标在 W4 验证。

## 复现

```bash
backend/.venv/bin/python scripts/qualify_remote_recovery.py \
  --output workspace/remote-recovery-qualification.json
```

需要 Linux、Docker、OpenSSL 及本地 `dios-worker-fixture:local` 镜像。资格固定本地镜像摘要，创建临时数据库、测试证书和容器，并仅清理本次作用域的资源。K01-2.03 继续覆盖定时重叠、排空和重启组合，K01-2.05 保留独立交付接受。
