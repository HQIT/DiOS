# 本地监测部署

DiOS 暴露固定标签的调度指标，Prometheus 负责持续采集、指标留存和本地告警。监测凭据只允许读取当前控制面 scope 的 `/api/os/runtime/scheduler-metrics`，不提供任务、配置、工具、成果或执行授权。E2AG 的审批、派发和恢复控制继续由原执行链路负责。

## 安装

Linux（含 WSL）使用独立目录及已下载的 Prometheus 镜像。初始化会固定镜像 ID，生成新的专用凭据、采集配置、四条既有健康规则及 Compose 配置。

```bash
python deploy/observability/init.py \
  --root /srv/dios-monitor \
  --image prom/prometheus:v3.5.0 \
  --target-url http://127.0.0.1:13081
```

镜像版本为本次验证版本，部署方可显式选择其他已验证镜像。初始化要求新目录，保留现有目录与凭据。

原生后端进程加载生成的 `backend.env`；已有任务按正常排空升级流程完成后重启后端。Linux Compose 安装使用生成的只读挂载覆盖文件：

```bash
docker compose --project-directory /srv/dios \
  -f /srv/dios/compose.yaml \
  -f /srv/dios-monitor/backend-monitoring.compose.yaml up -d backend

docker compose -f /srv/dios-monitor/compose.yaml up -d
```

Prometheus 采用 Linux host 网络，读取固定采集地址，监听 `127.0.0.1:13090`。后端和监测进程应能读取同一份 `0600` 凭据文件；初始化使用当前 UID/GID。配置目录只读挂载，时间序列目录单独持久化，无 Docker socket 或业务成果目录挂载。

跨主机目标必须使用 HTTPS；默认启用系统证书校验，不跟随采集重定向。采集配置每 15 秒请求一次，超时 10 秒，并限制响应大小、样本数和标签数。

## 凭据与权限

后端使用 `DIOS_RUNTIME_MONITOR_TOKEN_FILE` 指定专用凭据文件，Prometheus 使用 `authorization.credentials_file` 读取同一文件。文件须为普通文件，权限不允许组或其他用户读取；符号链接、空值、格式错误和与操作员凭据相同的配置均拒绝。

每次采集和返回前重新读取凭据，轮换或撤销在后续读取时生效。用同目录临时文件原子替换可更新凭据，目录挂载保留新文件可见性。操作员令牌、业务身份和监测凭据分开管理；监测凭据不会进入通用身份认证或 E2AG 授权接口。

## 留存与告警

默认留存时间为 15 天，TSDB 大小目标为 1 GB，可用 `--retention-time` 和 `--retention-size` 配置。该大小限制不包含所有 WAL、内存和临时空间，磁盘需预留余量。容器配置 512 MB 内存上限和日志轮换，数据目录在容器重建后保留。

沿用四条健康规则：调度观测不可用、持续周期失败、慢周期及指标采集失败。规则分别保留 1 分钟或 2 分钟的持续窗口。告警可在本地 Prometheus `/alerts` 查询，`ALERTS` 时间序列随指标留存。默认不配置 Alertmanager 接收方、Webhook、飞书或其他外部通知。

HTTP 管理和生命周期重载接口保持关闭。变更配置后执行容器重启；需要备份时间序列时，停止 Prometheus 后复制其数据目录，再启动原服务。后端任务不受监测容器重启影响。

```bash
docker compose -f /srv/dios-monitor/compose.yaml restart
```

规则验证及日志：

```bash
docker compose -f /srv/dios-monitor/compose.yaml run --rm --no-deps \
  --entrypoint /bin/promtool prometheus check config /etc/prometheus/prometheus.yml

docker compose -f /srv/dios-monitor/compose.yaml logs --tail 100
```

## 故障处理

采集失败先检查后端、TLS、网络和监测凭据；调度观测告警进一步查看 Console 的循环证据及脱敏诊断包。监测失败或任务长等待不会触发任务重派、进程重启或自动放宽 E2AG 策略。Prometheus 自身不可用时，其本地告警页面也不可用，需要基础设施层的进程监督或独立外部监控。

采用 [Prometheus 采集与凭据配置](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)、[本地存储规则](https://prometheus.io/docs/prometheus/latest/storage/)和[安全模型](https://prometheus.io/docs/operating/security/)。开发部署位于本机 13090 端口；持续采集、告警触发/恢复、读取权限及历史重启恢复的证据见[验证记录](evidence/monitoring-deployment-20261126.json)。