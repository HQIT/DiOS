# 托管执行的升级排空

升级先关闭新执行入口，等待已接纳任务和请求自然结束，再回收空闲服务。兼容 A2A、聊天流和持久 Worker 共用一个切换判定入口，属于 DiOS L1 生命周期能力，对应 #10 / #30。

## 机制选择

采用成熟 Worker 的停止领取、排空在途执行模式；[Temporal Worker](https://github.com/temporalio/sdk-typescript/blob/main/packages/worker/src/worker.ts)提供可参考的 shutdown/drain 生命周期。实现复用 DiOS 已有 Runtime Lifecycle、[SQLAlchemy 事务](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html)和已安装的 [filelock 原生异步锁](https://py-filelock.readthedocs.io/en/latest/api.html)。当前接入无需新增调度服务。

兼容应用数据库与运行数据库各自提交事务。协调器先持久关闭兼容入口，再调用 Runtime Lifecycle.begin_drain；第二步失败后兼容入口继续关闭，可重试。恢复依次校验 Worker 修订号、恢复 Worker，再恢复兼容入口；任一步失败都在状态中可见。状态检查包含两侧结果与观测时间。

```mermaid
flowchart LR
  D[关闭兼容入口] --> K[关闭 Worker 领取]
  K --> T[等待任务与请求结束]
  T --> E[确认 E2AG 工具调用已结清]
  E --> S[回收空闲共享服务并归档日志]
  S --> R[ready_for_cutover]
  R --> U[切换 API 与 Worker]
  U --> V[核对两侧修订号并恢复入口]
```

## 准入与排空

`managed_runtime_control` 保存 scope、mode、revision。准入检查与记录提交持有同一个原生主机锁。单次事件 fan-out 保持完整准入，排空等待该段完成；同协程内复用锁，派生协程独立获取。锁等待上限五秒，超时返回 RUNTIME_OPERATION_IN_PROGRESS，可重试，未取得锁的 drain 不改变入口状态。

聊天请求和兼容 service A2A 调用写入 `managed_service_calls`：请求身份、Agent、可选任务身份、派发标记、状态、错误与更新时间。调用前持久化派发标记；完整返回并保存结果后结清。流式请求以真实上游 DONE 和结果保存为结束证据。自行合成的错误 DONE 不构成完成证据。

| 情形 | 排空行为 |
| --- | --- |
| 新聊天、A2A、事件、直接服务启动 | 关闭期间拒绝准入；聊天返回 503 RUNTIME_DRAINING |
| 关闭前已经落账的 A2A 任务 | 继续执行，保持原身份，计入待完成数 |
| 已开始的事件分发 | 本次 fan-out 完成后才关闭入口 |
| 等待重试的事件 | 保留历史与重试次数；关闭期间不领取 |
| E2AG 审批 | 关闭期间不消费批准决定；恢复后仍按有效期及一次性决定规则处理，拒绝决定可继续 |
| 请求派发前失败 | failed；没有在途远程执行 |
| 请求派发后断流、超时、客户端断开 | unknown；保持阻塞，等待完成证据 |
| API 异常退出 | 未结清请求继续阻塞；账本 active 表示未结清，实际存活须另据进程和会话观察确认 |
| Worker 工具调用在途、结果未知或恢复隔离 | 沿用现有 Lifecycle/E2AG 判定，禁止回收共享服务与切换 |
| 等待排空超时 | 返回超时，不停止 Agent、不重派任务 |
| 无映射旧任务或旧容器 | 保留阻塞，进入显式核验迁移 |

`ready_to_stop_services` 要求兼容任务与服务请求已结清，配置的 Worker 内核也已可切换。`settle-services` 按 RuntimeAdapter 停止空闲服务、确认退出、归档日志并清理。`ready_for_cutover` 进一步要求无剩余服务对象。升级保持现有 E2AG 撤权、审批、工具调用和结果未知判定，不变更工作授权。

## 运维接口

| API | 操作 |
| --- | --- |
| GET `/api/os/upgrade/status` | 两侧 mode/revision、观测时间、任务与请求阻塞数、最多 32 条待结清请求、服务对象及切换判定 |
| POST `/api/os/upgrade/drain` | 幂等关闭入口与 Worker 领取 |
| POST `/api/os/upgrade/settle-services` | 门槛满足后回收空闲共享服务；存在在途或未知执行返回 409 |
| POST `/api/os/upgrade/resume` | 提交 `revision` 和配置内核时必需的 `kernel_revision`；过期修订号返回 409 |

API 使用安装的 operator Access Token；未配置 Token 时不开放升级控制。普通应用身份不获得控制权限。返回禁用缓存，记录不含凭据或工作区路径。CLI 使用相同安装环境：

```bash
python -m app.services.managed_upgrade status
python -m app.services.managed_upgrade drain
python -m app.services.managed_upgrade settle-services
python -m app.services.managed_upgrade wait --timeout 60
python -m app.services.managed_upgrade resume --revision <compatibility_revision> --kernel-revision <kernel_revision>
```

先配置相同的 DIOS_DATABASE_URL、DIOS_RUNTIME_SCOPE 与锁目录；使用 Worker 的安装还须配置实际 DIOS_RUNTIME_DATABASE_URL。未配置运行库时 coverage 仅为 compatibility。当前兼容 Provider 在指定控制主机运行，同主机 API 进程共享本地锁目录；远程 Worker 继续使用已有持久调度链路。

数据库初始化新增两张表，升级前备份应用库和运行库。入口保持关闭直到显式 resume；切换失败可恢复原版本进程，保留两个数据库和账本，核对状态后恢复入口。初次部署本功能前应先停止外部投递并按既有方法确认无在途兼容执行；旧版本尚无服务请求账本。

未知请求仍需要 Provider 的结果/进程证明来结清；当前无强制清空接口。既有 cron 的跨重启补触发、旧任务显式接管、共享服务逐请求授权和统一 Run/Attempt/Session 审计映射继续按原计划推进。

## 验证

[验证记录](evidence/managed-upgrade-20261124.json)覆盖真实 API 与容器流式请求、排空后拒绝新请求、原进程自然完成、日志归档、重启后的持久关闭、版本冲突、事件 fan-out、审批与重试保护、完整后端回归和开发部署。容器流使用合成协议输出，模型与 ACP 的能力资格沿用各自专项证据。
