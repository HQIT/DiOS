# DSH 供应商故障与自动恢复

R06 已通过固定 DSH、SDK、真实 HTTP 端点与独立远程 Worker 的完整恢复验证。限流和额度不足分别进入原有冷却策略；单路探测成功后恢复三路执行，同资源保持互斥，独立账号可以继续工作。证据见 [R06 结果](evidence/provider-errors-20261127.json)。

## 错误边界

DSH 0.2.2-rc.5 的 `turn/end` 保留 `LlmFailure`，原 ACP 出口仅返回错误文本。受控镜像通过 [兼容补丁](../../scripts/patch_dsh_acp_errors.py)将原有结构放入 JSON-RPC `error.data`，保持原错误码和消息。补丁核验源文件与执行 bundle 的 SHA-256；不匹配的版本拒绝应用。该组合包含 DiOS 维护的补丁，尚非上游原版行为。

SDK 0.1.20 仅在 DSH 的 `session/prompt` 错误中映射以下分类：

| DSH 分类 | DiOS 分类 |
| --- | --- |
| RATE_LIMIT | PROVIDER_RATE_LIMITED |
| QUOTA / ACCOUNT_QUOTA | PROVIDER_QUOTA_EXHAUSTED |
| TIMEOUT | PROVIDER_TIMEOUT |
| TRANSPORT | PROVIDER_NETWORK_ERROR |

未知分类、缺失结构及普通文本保持 `ACP_REQUEST_FAILED`。认证、会话创建及其他 Harness 的映射维持原语义。原始供应商消息不进入 SDK 诊断日志。

固定 DSH 的 Pi-AI 适配器会从供应商错误文本提取上述分类；DiOS 消费其已定义的结构化结果，不从模型正文推断故障。此次验证覆盖两种实际 HTTP 429 响应，未扩展为所有供应商、超时及网络形态的完整资格。供应商 `Retry-After` 当前未由这条适配链完整保留，冷却时间沿用管理员配置的有界策略。Cursor 的普通文本错误来源仍需供应商支持。

## 执行与授权

每类故障执行六次实际 DSH 会话：一次 HTTP 错误、一次恢复探测、一个独立账号任务、三个恢复并发任务。回环 HTTP 服务提供合成响应；容器 `network=none`、非 root、只读根文件系统，模型凭据为无权限的测试字符串，外部模型调用为零。

两轮均核验单路 half-open、三个独立 Worker PID/执行进程/会话、原 Run 与工作授权保留、新 Attempt/epoch 和独立 E2AG Grant、旧所有者报告拒绝及全部排队任务完成。HTTP 响应事件、SDK 错误帧、最终产物及其内容摘要保留在证据中。重试准入、派发和结果接收继续使用生产 E2AG 路径；未增加重试授权旁路。

隔离验证入口：

```bash
DIOS_TEST_DOCKER=1 DIOS_TEST_DSH_PROVIDER_IMAGE=<已核验的固定镜像ID> \
  backend/.venv/bin/python scripts/qualify_reference_faults.py \
  --cases R06 --output workspace/reference-provider-errors-run
```

未配置固定镜像时测试跳过，资格入口保持未通过。开发组合构建自已有受控 DSH 镜像，安装当前 SDK wheel 并运行上述摘要锁定补丁；实际镜像 ID、wheel 与源码摘要见证据。

## 标准与实现依据

- [JSON-RPC 错误对象](https://www.jsonrpc.org/specification#error_object)允许结构化 `error.data`。
- [ACP ErrorResponse](https://agentclientprotocol.github.io/typescript-sdk/types/ErrorResponse.html)保留 `code`、`message`、`data`；运行协议仍为已固定的 ACP v1。
- [DSH ACP session](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/src/session.ts)定义任务结束到 RPC 错误的边界；实际补丁以固定发布包摘要为准。
