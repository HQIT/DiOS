# 脱敏诊断包

每个任务提供独立诊断 ZIP，包含状态、尝试、E2AG 工作授权与工具审批、恢复计划、成果元数据，以及运营者可见的调度心跳和接收端版本。诊断包与完整成果独立读取，导出不读取成果正文，也不触发任务、工具或恢复操作。

## 读取入口

Console 任务详情提供“导出脱敏诊断包”。API 为 `GET /api/os/runtime/runs/{run_id}/diagnostic-export`，返回 `application/zip`、`Cache-Control: no-store` 和随机下载文件名。

```bash
backend/.venv/bin/python -m app.runtime.cli --scope example \
  diagnostic-export RUN_ID --output /srv/support/task.zip
```

CLI 使用 `DIOS_RUNTIME_DATABASE_URL`，以具备数据库访问权的本地运营者身份读取。输出必须为新文件，Linux 权限为 `0600`；已有文件及符号链接均不覆盖。

Python SDK 0.1.18 的 `client.diagnostic_export(run_id)` 返回 ZIP 字节。SDK 的请求时限和响应大小上限继续生效。

## 内容与权限

| 内容 | 可见条件 | 导出内容 |
|---|---|---|
| 任务、尝试、事件、工作授权、恢复 | `run.read` | 状态、时间、版本、错误分类及包内关联 |
| 会话关联 | 会话对象读取权 | 包内匿名会话标识 |
| 工具、工具决策 | `run.read`、`tool.read` | 审批、执行状态、允许或拒绝、时间和关联 |
| 成果 | `run.read`、`artifact.read`、成果对象读取权 | 包内标识、字节数、所属尝试和时间 |
| 调度与同步 | 平台运营者 | 本 scope 有界调度实例观测、本 Run 本地接收端投影版本 |

生成前检查对象访问权，返回前再次检查已包含对象、任务动作和实际请求凭据。普通应用身份不会通过任务导出获得 scope 级运营信息。已下载文件无法远程撤回。

字段采用明确白名单。排除输入、代码、模型正文、工具参数和结果、凭据、文件路径、主体资料及审批自由文本。除平台生成的 Run 身份外，其他身份使用每包随机密钥的 HMAC 匿名标识；同包关联保持一致，不同包无法直接拼接匿名标识。未知错误代码也匿名化，已知错误保留固定分类。

该设计采用 [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) 的敏感字段排除与受控读取原则，以及 [OpenTelemetry Redaction Processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/redactionprocessor/README.md) 的字段白名单方法。

## 大小与完整性

ZIP 总大小和解压后的文件总大小均限制为 512 KiB。包内只有 `diagnostic.json`、`manifest.json` 和 `README.md`。每类查询限制行数：尝试 20、事件 100、工作授权 1、执行授权 20、工作决策 100、工具 50、工具决策 100、成果 100、恢复计划 1、调度实例 20、同步接收端 20。

清单记录查询上限、更多记录标记、权限排除、字节裁剪及导出记录数。元数据超过总上限时裁剪集合并明确标记。SHA-256 和字节数用于核验包内文件，原始审计链的真实性另由其验证机制负责。诊断包为有界投影，历史详情继续通过分页证据查询及成果接口读取。

## 故障判读

| 现象 | 独立证据 |
|---|---|
| 模型长时间等待 | 尝试阶段、开始时间、最近输出时间、最新进程或远端观测 |
| 调度循环停摆或异常 | 调度心跳、循环完成时间、连续错误数及 `stale/slow/degraded` 分类 |
| 接收端更新落后 | 接收版本、Run 版本、版本差及接收时间 |
| 工具结果未知 | `result_unknown` 与已有审批及授权决策同时保留 |

`confirmed_running` 要求有效租约、新鲜进程观测、存活进程身份及可见会话；远端任务报告不计作本地进程实测。缺少心跳表示观测缺口，不能据此重复派发。接收端版本落后可定位同步链路，但不推断其具体网络原因；未登记或未在当前数据库保存观测的远端驾驶舱显示未知。平台数据未更新与浏览器请求失败分别排查。

实测、远端报告和未知按记录标注，导出不生成估算值。未接入门禁的 Harness 内部活动保持未观测。

工程证据见[诊断导出验证记录](evidence/diagnostic-export-20261126.json)。