# 配置建议智能体

`agent.py` 使用现有 `dios_sdk.cursor.cursor_execute`，从 `DIOS_WORK_INPUT` 接收公开配置和修改要求，返回待审阅的完整配置。输入摘要绑定原配置，结果包含 `configuration_proposal`、`summary`、`evidence` 与 `review_status=pending_review`。

镜像需要 Python、已安装的 DiOS SDK 和 Cursor ACP。将本目录的 `agent.py` 固定到镜像 `/opt/dios/configuration-assistant.py`，在已验证的 Cursor Profile 中设置：

```json
{
  "id": "studio-cursor",
  "console": {
    "enabled": true,
    "input_kind": "dios-config-proposal-v1",
    "input_catalog": "/absolute/operator/studio-inputs.json",
    "name": "Cursor · 配置建议",
    "implementation": "Cursor ACP v1",
    "synthetic": false
  },
  "governance": {"mode": "e2ag"},
  "launch": {
    "command": ["python", "/opt/dios/configuration-assistant.py", "--config", "/opt/dios/acp.json"]
  }
}
```

以上为覆盖字段，镜像摘要、网络、资源、只读挂载和 Secret 引用继续采用受管 Profile 的配置。输入文件采用仓库 `examples/runtime/studio-inputs.json`。工作策略与 Secret 目录需要显式准入该 Profile 的固定摘要及所需凭据引用；由现有 CLI Worker 执行。

配置建议不需要工具通道。Cursor 使用 ask 模式，原生 Shell、写文件、读取 Secret 和 WebFetch 沿用拒绝规则。工作负载只生成建议，修改的命令或路径作为待审阅数据返回。工具调用仍由 SDK 控制边界拒绝。

当前动态任务输入上限沿用 Runtime 契约；Studio 预检建议输入为 11 KiB，超限返回明确错误，手动编辑仍支持 64 KiB。模型费用和执行时间计入原 Runtime 任务。
