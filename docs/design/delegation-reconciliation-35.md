# 委派签发身份巡检

签发身份巡检覆盖所有尚有效的认证委派，包括尚未创建任务的委派。确认身份失效或签发权限收回后，Runtime 撤销对应委派及依赖的活跃 Work/Grant；身份服务暂时不可用时保留授权记录并自动重试。实际执行仍逐次检查当前授权。

## 调度与持久状态

复用现有身份巡检的有界批处理、数据库租约和指数退避流程。原生 [systemd timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)负责周期触发；已运行的 service 保持运行，重复触发不创建另一个实例。多个主机或手工命令同时执行时，由 Runtime 的 scope 事务和记录租约协调领取。

新增 `runtime_delegation_checks` 保存委派 ID、授权版本、绑定摘要、owner、generation、租约期限、最近检查、最近成功、下次检查、失败次数和结果。身份查询在数据库事务外进行。提交时复核 owner、generation、租约、不可变绑定及授权版本；过期或被替换的查询结果不更新授权。

| 查询结果 | 持久处理 |
| --- | --- |
| 身份及签发范围有效 | 记录成功时间，清零失败次数，按正常间隔复查 |
| Provider 不可用 | 保留授权，按有上限的指数退避复查 |
| 身份失效、签发权限收回或身份期限缩短 | 同事务撤销委派及依赖 Work/Grant，记录原因和受影响数量 |
| 查询期间已显式撤销、已过期或授权版本改变 | 旧结果标为 superseded，保留当前授权状态 |
| 查询租约失效或所有者被替换 | 旧结果标为 stale，新所有者继续处理 |

恢复 Provider 后，下一次到期巡检自动重试。已撤销的授权保持终态，需要重新签发。CLI 管理、API 撤销与后台巡检共用事务撤销函数。OIDC 注销事件继续通过独立事件接收链路即时撤权。

## 运行与观察

```sh
cd backend
.venv/bin/python -m app.runtime.cli --scope enterprise delegation-reconcile \
  --limit 20 --interval 60 --retry-base 15 --retry-max 300
```

每批最多处理 `limit` 条到期记录，返回结果和实际检查数量。示例 [service](../../examples/runtime/dios-delegation-reconcile.service) 与 [timer](../../examples/runtime/dios-delegation-reconcile.timer) 使用现有运行账户和受保护环境配置。部署前按既有流程排空任务、校验备份并执行增量迁移；定时服务不自动迁移数据库。

`delegation show` 和认证查询 API 返回 `source_check`。任务详情的委派链展示签发身份巡检结果、最近检查及活跃授权的下次检查时间；超过预期时间的观测标为过期。巡检观测用于解释状态，执行准入继续使用实时授权检查。

备份恢复包含巡检状态和租约。进程退出后，租约到期即可由新进程领取；旧进程的迟到结果无权提交。查询故障不会据此宣告 Worker 死亡或重新创建业务任务。

## 验证与容量

[证据记录](evidence/delegation-reconciliation-20261124.json)包含独立数据库连接并发领取、租约接续、过期结果拒绝、Provider 故障恢复、权限撤回、备份恢复，以及原生定时服务对独立委派和真实 Worker 的验证。开发环境还验证了中英文和手机宽度下的巡检状态展示。

当前保证以单个 Runtime 权威数据库为边界。跨 Provider 的严格撤权排序、跨服务 audience 转换、PostgreSQL 与物理多主机资格、批量巡检容量和授权路径延迟继续按整体部署计划验证。
