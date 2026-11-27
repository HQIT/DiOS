# 存储写满与 Linux 节点重启恢复

DiOS Worker 在本地存储写满时保持后台运行，按 1–30 秒指数退避探测写入能力。恢复后先核对原任务的退出证据、结果内容和 E2AG 结果权限，再处理排队任务。

## 存储故障

8 MiB 独立测试卷产生真实 `ENOSPC`。故障期间完整输出留在原任务容器中，任务不计为成功，也不领取新的排队任务。释放空间后，原 Attempt 与 Session 保持，完整结果通过验证；无需重启 Worker，未重复启动原任务。

恢复仅处理磁盘空间、配额耗尽以及既有 `SPOOL_FULL`。权限错误等其他异常继续明确失败；单次执行模式仍返回错误供调用者处理。写入探测使用固定内容摘要，重复探测复用同一对象；成功读取状态不代替写入能力检查。

依据 [SQLite 事务错误处理](https://www.sqlite.org/lang_transaction.html)，磁盘写满后的事务可能回滚，须由应用确认结果并恢复。故障卷采用 [Docker tmpfs 限额](https://docs.docker.com/engine/storage/tmpfs/)，仅消耗指定测试卷额度，不填满宿主磁盘。

## 节点重启

第二台物理主机上的 Linux WSL 节点经停止、重新启动后，重新建立 mTLS 连接并使用持久配置与产物目录。测试同时保留完整结果和仍在运行的合成任务，验证以下行为：

- 已完成输出经退出证据和 E2AG 重新校验后归入原 Attempt 与 Session。
- 被中断且没有完整结果的任务保留失败与原始输出，不计成功、不自动扩大重试范围。
- 旧 Worker 所有权报告被拒绝；新任务在当前授权下执行。
- 无关的既有容器保持停止状态。

配置、证书、spool 和产物须放在安装的持久目录。`/tmp` 可能在节点启动时清理，不能承载恢复所需数据。节点启动及 Worker 服务启动由基础设施管理；DiOS 负责启动后的身份校验、所有权和结果对账。

[Microsoft 的 WSL 命令说明](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)支持停止指定发行版。本次验证范围为第二物理主机上的完整 Linux 节点停止与启动；物理断电、控制面 HA 和存储复制 RPO/RTO 继续按 D01-3.03 的故障模型验证。

## 验证入口

[测试脚本](../../scripts/qualify_storage_restart.py)接收已核验的 SSH 配置、镜像、源码目录及独立重启 hook：

```sh
DIOS_TEST_DOCKER=1 backend/.venv/bin/python scripts/qualify_storage_restart.py \
  --ssh-config /path/to/private/ssh_config --host physical-worker \
  --remote-source /path/to/qualification/source \
  --runtime-image sha256:VERIFIED_IMAGE_ID \
  --case-root /var/tmp/dios-qualification \
  --restart-hook /path/to/private/restart-argv.json \
  --output workspace/storage-node-result.json
```

重启 hook 为 JSON argv 数组，脚本末尾传入当前测试的 scope 及其 SHA-256。hook 在重启前检查其他工作负载，在指定节点恢复 Docker 和已核验 SSH 后返回 JSON 结果。地址、访问凭据和宿主命令留在部署侧。

`--case` 可单独选择存储或重启场景。运行前确认该节点已划为测试用途，重启不会影响其他业务。

验证结果见[机器可读证据](evidence/storage-node-recovery-20261127.json)。结合[网络与进程故障](physical-fault-qualification-53.md)及[两主机环境](physical-worker-qualification-53.md)，D01-3.02 所需故障入口全部具备。
