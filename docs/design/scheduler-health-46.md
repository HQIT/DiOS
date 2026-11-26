# 调度器健康与标准告警

任务调度和订阅调度分别记录循环存活、最近周期完成、最近成功、连续错误及耗时。运行任务数量、模型输出和浏览器请求与这些证据独立：模型等待不代表调度停止，看板请求失败不改变执行状态。

## 观测与执行边界

每次进程内调度循环启动生成新实例身份，`runtime_loop_observations` 按 scope、服务与实例持久保存观测。周期开始、结束、空闲和退出均有明确状态。独立 `monitor_version=1` 迁移及备份校验包含该表；升级前仍执行既有排空和安全切换流程。

运行中或空闲时每 5 秒更新观测，20 秒无新鲜心跳标记过期。周期超过 120 秒且仍有新鲜心跳标记耗时过长。时间阈值是初始运维默认值，后续根据容量实测调整。周期完成时间使用数据库时钟，耗时使用同进程单调时钟。

周期成功表示一次调度检查返回，可能没有到期工作，也可能因其他实例持有所有权而未领取。工作是否实际进入队列、是否通过 E2AG 授权、是否执行成功，分别由原 occurrence、Run 和授权证据说明。监测保留原有租约、幂等、审批和派发控制点。

耗时过长只产生观测；监测等待不会取消、重启或重复提交正在执行的周期。服务关闭时沿用原调度任务取消语义并记录停止。遥测数据库错误不会重做调度动作；持久记录缺失或过期保持未知，不以进程本地意图冒充持久成功。

## 健康状态

| 状态 | 依据 |
| --- | --- |
| 正常 | 新鲜心跳，已有成功周期，当前无连续错误 |
| 周期失败 | 最近周期抛出错误，后续成功周期清零连续错误 |
| 周期耗时过长 | 当前周期已超过 120 秒，心跳仍新鲜 |
| 观测过期 | 心跳超过 20 秒，或数据库时间异常导致观测来自未来 |
| 已停止 | 同一循环记录了生命周期退出 |
| 尚无成功周期证据 | 未登记、启动中，或遥测失败后尚无可用成功时间 |
| 未启用 | 本实例明确关闭任务定时功能，仍发布配置状态 |

多个新鲜实例中，慢周期或错误会保留为服务级异常。历史退出和过期实例不覆盖当前健康实例。接口同时返回最新观测和支撑当前诊断的实例证据，页面展示后者，防止健康实例掩盖同服务另一实例的故障。

每个服务最多返回最近 100 条实例，标记更多历史；若新鲜实例本身超出上限，则服务状态保持未知。实例历史保留，不在观测请求中删除。长期保留策略及大规模实例容量测试继续在 #46 推进。

## 接口和访问

操作员可调用：

```text
GET /api/os/runtime/scheduler-health
GET /api/os/runtime/scheduler-metrics
```

应用身份调用返回 403，无凭据返回 401。指标接口另支持仅采集用途的专用凭据，配置见[本地监测部署](monitoring-deployment-46.md)。指标仅使用固定服务名及状态标签，不包含用户、任务、实例身份、错误文本或凭据。JSON 仅保留规范化错误码；未分类异常统一标记 `SCHEDULER_ERROR`。

可信本地管理 CLI：

```sh
dios runtime scheduler-health
```

Console 同步中英文、桌面和手机展示。10 秒请求超时后保留旧数据并提示更新失败，实例数量和状态显示待刷新。

## Prometheus 接入

沿用 [Prometheus 监测指南](https://prometheus.io/docs/practices/instrumentation/)的心跳、最近完成及错误观测方式；使用[标准告警规则](https://prometheus.io/docs/practices/alerting/)的持续窗口抑制短暂波动。

[四条告警规则](../../deploy/observability/scheduler-alerts.yml)覆盖观测不可用、持续周期失败、周期耗时过长和指标采集失败。[规则测试](../../deploy/observability/scheduler-alerts.test.yml)验证延迟触发、恢复消警及正常／禁用状态不触发。

现有 Prometheus 可增加以下采集任务，并加载规则文件。凭据文件使用独立的只读监测凭据；跨主机采集使用 HTTPS。

```yaml
scrape_configs:
  - job_name: dios-schedulers
    scrape_interval: 15s
    metrics_path: /api/os/runtime/scheduler-metrics
    scheme: https
    authorization:
      credentials_file: /run/secrets/dios-monitor-token
    static_configs:
      - targets: [dios.example.com]
rule_files:
  - /etc/prometheus/rules/scheduler-alerts.yml
```

规则包和采集链路已验证；开发部署已持续运行 Prometheus，提供本地告警和时间序列留存，未配置外部通知。控制面或数据库整体不可用时，外部采集失败告警提供观测；同一控制面无法独立证明自身网络或进程故障。

## 验证与后续

[验证记录](evidence/scheduler-health-20261125.json)包含 SQLite/PostgreSQL、慢周期不重复执行、失败后恢复、观测写入失败、进程崩溃与重启、权限隔离、备份、标准 Prometheus 规则测试、真实采集及中英文界面验证。

[有界脱敏诊断包](diagnostic-export-46.md)和[只读监测部署](monitoring-deployment-46.md)已交付；后续按 #46 推进治理性能与证据完整性报告，物理多主机及大规模容量验收沿既定 WBS 进行。
