# 定时调度、排空与 Linux 重启

定时调度持久化触发点、待执行记录和交付回执。多个调度进程处理同一触发点时只创建一个 Run 和 Work；上一轮运行期间将新触发合并到缓冲记录。排空停止领取与新增交付，已有任务自然完成；恢复接单后继续处理缓冲记录，执行仍经过 E2AG。

## 工程验证

[资格证据](evidence/schedule-reboot-qualification-20261126.json)包含 20 项定时契约测试及以下真实 Linux 虚拟机组合。控制服务运行原生 API 后台调度循环，另启动三个独立调度进程竞争；Worker 在 Docker 内执行合成任务。

| 场景 | 结果 |
| --- | --- |
| 三路重复触发 | 一个 Run、一个 Work |
| 活跃任务期间重叠触发 | 保留原 Run、Attempt、会话，后续触发进入缓冲 |
| 活跃期间排空 | 原进程继续运行；另一 Worker 返回空闲，调度器不增加 Run |
| 自然完成 | 原 Attempt 成功，进程观测为已退出；结果和标准输出产物完整并通过摘要验证 |
| Linux 内核重启 | boot ID 改变；控制容器自动启动，镜像和容器身份保留；排空状态、游标、触发记录、任务、Work 与产物身份保留 |
| 恢复接单 | 缓冲记录生成新的 Run 和 Work，并经过 E2AG 完成执行；最终两个成功 Attempt、四个产物 |

环境为 Ubuntu 24.04、Linux 6.8.0-151-generic、单个隔离虚拟机，云镜像经过官方 SHA256 清单及签名校验。Docker 两端对镜像 ID 分别显示配置摘要和 manifest 摘要，导入前后 RootFS 层与有效运行配置一致。实际模型服务未参与该调度验证。

本项证明排空后的内核重启恢复。不同版本升级与回退沿用 K02-1.03 的已有证据，备份恢复与升级联动由 K02-3.03 承接；物理多主机故障由 K01-2.04 承接。

## 复现

使用可销毁的 Linux 虚拟机，主机名设为 `dios-qualification`，安装 Docker 并启用开机启动。SSH 仅转发至宿主回环地址；资格脚本会在该虚拟机执行 `systemctl reboot`。

1. 在宿主独立证据目录保存 `guest_key`、已核验的 `known_hosts` 和 `launch.json`（包含 `ssh_port`）。将待测后端及 `dios-worker-fixture:local` 通过 Docker save/load 导入虚拟机，记录导入后的不可变镜像 ID。
2. 在虚拟机建立 `/opt/dios-qualification`，放入 `images.json`，格式为 `{"backend":"sha256:…","fixture":"sha256:…"}`。复制 [guest driver](../../scripts/qualification_schedule_guest.py) 为该目录的 `driver.py`。
3. 用后端镜像执行一次 `setup`：挂载上述目录，设置 `PYTHONPATH=/app`，入口为 `python /opt/dios-qualification/driver.py setup`。已有数据库会被拒绝覆盖。
4. 用同一镜像启动 `dios-qualification-control`，指定 `--restart unless-stopped`、`PYTHONPATH=/app`、该目录的 `service.env`，挂载资格目录及 `/var/run/docker.sock`。控制 API 使用镜像默认入口。等待健康检查成功。
5. 在宿主执行以下命令；证据保存在指定目录。脚本校验专用主机名和空账本，随后创建定时任务、排空、重启虚拟机并恢复接单。

```bash
python3 scripts/qualify_schedule_reboot.py \
  --workspace workspace/qualification-schedule-reboot-20261126
```

`--resume-drained` 仅用于原记录已经自然结束、尚未重启时继续同一次资格验证。观察超时先检查原进程和数据库状态，保留既有身份及产物。结果归档后关闭专用虚拟机。

Ubuntu 虚拟机启动采用[官方 QEMU/cloud-init 指南](https://ubuntu.com/docs/public-images/public-images-how-to/launch-qcow-with-qemu/)；镜像身份解释依据 [OCI manifest](https://specs.opencontainers.org/image-spec/manifest/) 与 [OCI configuration](https://specs.opencontainers.org/image-spec/config/)。
