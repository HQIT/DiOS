# 执行证据完整性与瓶颈报告

DiOS 提供可离线复算的执行证据报告，关联任务、尝试、Worker、会话、阶段、治理检查点、用量与结算记录。K04-3.04 的四类瓶颈对照通过；报告命令已在现有 Linux/WSL 开发环境运行。

## 现有任务的证据覆盖

本次报告包含当前开发作用域的全部 85 个 Run、89 个 Attempt。历史身份及 239 个 Artifact 保持一致。

| 指标 | 结果 | 解释 |
| --- | ---: | --- |
| 任务首次终态 | 85/85 | 成功 54、失败 24、取消 7；重试不增加逻辑任务完成量 |
| Profile 摘要 | 85/85 | 可定位当时执行配置摘要 |
| 独立 Revision 关联 | 0/85 | 这些历史任务未登记 Revision；保留 Profile 摘要 |
| 显式来源关系 | 0/85 | 未登记关系，不从模型文本推断知识使用 |
| 可计算的已结束阶段 | 475/475 | 数据库收件时间，无负时长补零 |
| 已记录治理耗时 | 7/1101 | 本次返回的有界检查点样本；旧记录多数没有计时 |
| 会话用量报告 | 0/81 | 未报告值保留未知，不计为零成本 |
| 当前实际并发 | 0 | 当前任务均已终结；历史并发峰值未知 |

治理诊断每任务最多返回 100 条，历史报告存在检查点截断；全历史治理耗时缺失率和分位数未知。报告保留截断标记、返回记录分母及捕获时间。Agent 内部工具事件的完整性未观测，显式来源关系的存在率也不代表业务任务应有来源的覆盖率。

历史阶段中，`streaming` 的收件间隔 P50 为 3.29 秒、P95 为 78.01 秒；`creating_session` 最大 44.90 秒。这些值用于定位待查阶段，包含传输、轮询和上报等待，无法单独推定模型计算时间。

## 四类瓶颈对照

采用 [OpenTelemetry 的 Span 起止边界](https://opentelemetry.io/docs/concepts/signals/traces/)划分测量范围，同一进程内用单调时钟计算耗时。生产审计沿用已有 E2AG 计时；额外的传输、存储和执行边界探针只运行于隔离实验。

每组启动两个独立 Linux Worker 进程和两个原生容器会话，经过实际 E2AG 授权、持久领取、执行、结果校验与完成记录。五组共完成 10 个任务，每组均观测到实际并发 2，保留 Worker PID、原生进程身份、会话及采样记录。容器负载为固定合成协议数据，不调用模型。

| 单独改变的因素 | 对应测量边界 | 相对基线的 P50 增量 | 判定 |
| --- | --- | ---: | --- |
| 策略读取增加 150 ms | E2AG `policy_refresh_seconds` | 151.81 ms | 可识别策略刷新等待 |
| 协议读取增加 150 ms | Adapter 日志传输调用 | 151.60 ms | 可识别传输边界等待 |
| 独立 SQLite 连接持有写锁 150 ms | 事务进入等待 | 238.98 ms | 可识别真实锁竞争，包含并发排队 |
| 容器内负载增加 3 s | 负载进程内单调时钟 Span | 3.0001 s | 可识别执行耗时 |

执行 Span 与 `streaming` 收件间隔分别记录。前者证明负载自身耗时，后者反映控制面实际接收进度；上报延迟会改变两者差值。策略总检查时间包含其子阶段，事务等待、执行与传输也可能重叠，各项不能相加作为总耗时。

吞吐由每组两个真实完成记录除以该组观测墙钟时间计算，原始小时/日统计按首次终态记录分桶。该小样本验证计量与归因机制；两档容量、物理跨主机和真实模型性能沿用各自 WBS 验收。

## 用量与费用

Agent 用量按 Attempt 和类型选取最新 sequence，同一会话的累计快照不重复求和；ACP 未明确累计范围的快照保留原值及范围标记。结算逐账户、逐 Attempt 列出预留、实际值和证据存在状态，跨币种及可能重叠的账户不直接相加。

费用准入、未知消耗保留、幂等结算及撤权行为复用 [K04-2.01 验收](consumption-acceptance-45.md)。本次核验 `consumption.py`、契约与数据库 schema 摘要与原验收一致。Agent 自报费用与管理员凭证结算保留不同证据等级。

## 使用

报告命令面向有本地数据库读取权限的运维人员。数据库连接通过环境变量提供；命令不迁移数据库、不派发任务。Web 与 SDK 用户继续通过已有权限过滤的执行证据及诊断导出接口查询。

```bash
backend/.venv/bin/python scripts/report_runtime_evidence.py \
  --scope development --output-directory workspace/evidence-report

backend/.venv/bin/python scripts/report_runtime_evidence.py \
  --input workspace/evidence-report/capture.json \
  --output-directory workspace/evidence-report-recomputed
```

默认连接变量为 `DIOS_RUNTIME_DATABASE_URL`；`--database-url-env` 可指定变量名。`--profile-id` 对任务、完成量和并发使用同一配置范围。`--limit` 默认 500、最多 2000，超限明确标记为所选子集。输出目录必须不存在，目录权限 0700、文件权限 0600。

`capture.json` 保存允许字段的原始测量，`report.json` 保存分母、分位数、UTC 小时/日完成量和独立时间戳的实际并发，`report.md` 供阅读。任务内容、工具参数、成果正文、主体信息及 Secret 引用不进入捕获文件。历史报告的完整捕获保留于运维私有目录；[公开合成测量](evidence/evidence-boundary-samples-20261126.json)可复算隔离实验。

```bash
backend/.venv/bin/python scripts/verify_evidence_boundaries.py

backend/.venv/bin/python scripts/probe_evidence_boundaries.py \
  --output-directory workspace/evidence-boundary-probe
```

探针使用已存在的固定摘要镜像、本机 Linux Docker 和独立 SQLite；不会读取业务任务或模型凭据。生产审计尚未采集提交落盘耗时和 Agent 内部 Span，这些字段保持未知；实验中的事务提交与清理单独测量。

## 验证与运维

SQLite/PostgreSQL 共 7 项报告测试通过，覆盖空分母、筛选、重复身份、截断、时钟异常、最新用量快照和结算未知值。五组原生实验通过，历史报告离线复算字节一致。候选镜像依赖与导入检查通过；现有开发实例已安装并运行报告命令，API、操作员读取和监测正常，数据库无新增迁移。

开发 OpenBao 的两个 12 小时 AppRole SecretID 到期后无法重认证，其中一个角色因连续失败进入锁定。依据原生 [AppRole 签发](https://openbao.org/docs/auth/approle/)和[角色解锁接口](https://openbao.org/docs/api/system/user-lockout/)恢复，角色权限与锁定策略保持原配置；无效 Bearer 请求已恢复为 403。开发临时 SecretID 仍需按期供给，长期平台身份供给由既定身份部署验收承接。

完整结果与源码摘要见[工程记录](evidence/evidence-completeness-20261126.json)。
