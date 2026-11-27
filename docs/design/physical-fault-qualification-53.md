# 跨主机网络与 Worker 故障恢复

DiOS 控制面与远程 Worker 在两台物理主机的 Linux 环境中运行。网络分区、Worker 进程强制结束、200 ms 下行延迟三个场景通过，使用原有 mTLS、持久租约、E2AG 和结果对账逻辑。

## 实测行为

| 故障 | 实际行为 | 判定 |
| --- | --- | --- |
| 控制通道断开 | 停止失去控制通道的受控执行，保留失败与输出；恢复连接后，常驻 Worker 自动对账并重新授权执行 | 原 Run 与工作授权保留，两次 Attempt；首次分类为 CONTROL_PLANE_UNAVAILABLE；恢复约 70.94 秒，未手工重启 Worker |
| Worker 收到 SIGKILL | 任务容器独立存活并输出完整结果；重启 Worker 后等待原租约到期，验证输出并提交结果 | 原 Attempt、Session 和进程实例保持；额外任务启动次数为零；恢复约 59.71 秒 |
| 下行增加 200 ms 延迟 | mTLS 查询实际耗时约 0.562 秒；任务继续执行和提交成果 | 仅一个 Attempt，成功完成；未因慢响应重复执行 |

恢复时间来自本次实验，包括正常租约到期、轮询和退避，不构成企业 SLO。测试不改写租约时间或数据库状态。SIGKILL 场景验证 Worker 进程中断，完整节点重启与存储故障继续由 D01-3.02 承接。

## 故障机制与治理

网络故障由 [Toxiproxy 2.13.0](https://github.com/Shopify/toxiproxy/releases/tag/v2.13.0) 注入。禁用专用代理产生断连，latency toxic 产生定向延迟；代理仅监听本机回环地址，控制通道仍采用端到端 mTLS。Toxiproxy 为实验工具，DiOS 运行依赖保持不变。

[Temporal 的幂等与错误处理文档](https://docs.temporal.io/activity-definition)说明执行完成而结果尚未送达时重试可能重复动作。DiOS 在该窗口复用现有任务身份、退出证据与结果验证，先判断是否已有可接受的完整输出，再决定恢复或重新授权执行。

容器中断只作用于随机测试 scope。Docker Adapter 将 scope 的 SHA-256 写入资源标签，故障夹具和清理使用同一值。验证结束后，测试管理容器和任务容器均为零；已有其他容器保持原状态。

## 复现入口

先按[两主机资格环境](physical-worker-qualification-53.md)准备镜像、源码及专用 SSH 别名，下载官方 Toxiproxy 二进制并核对发布摘要：

```text
v2.13.0 / toxiproxy-server-linux-amd64
SHA-256 bf529e8bb9a05cd293f45a3f1d0aa66ff9469680dcdca7e0ebeae21f1b83de89
```

```bash
DIOS_TEST_DOCKER=1 backend/.venv/bin/python scripts/qualify_physical_faults.py \
  --ssh-config /path/to/private/ssh_config \
  --host physical-worker \
  --remote-source /path/to/qualification/source \
  --runtime-image sha256:VERIFIED_IMAGE_ID \
  --toxiproxy /path/to/verified/toxiproxy-server \
  --output workspace/physical-faults-result.json
```

`--case` 可选择单项故障，支持只复测受影响场景。凭据、节点地址和故障日志留在私有工作目录；公开[验证报告](evidence/physical-faults-20261127.json)记录源码摘要、原任务关联、测量值与适用范围。

## 后续交付

D01-3.02 保持进行中，下一步补存储故障和完整节点重启。控制面 HA、存储复制与 RPO/RTO 在 D01-3.03 验证；源端恢复隔离及父子任务容量继续按原依赖推进。