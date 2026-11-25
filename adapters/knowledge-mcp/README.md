# DiOS Knowledge MCP

独立只读工具适配器，使用官方 [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 的 Streamable HTTP 服务，将 DiOS Knowledge API 接入现有 E2AG 工具链。底座无需安装 MCP 服务端依赖；适配器只依赖公开 `dios-sdk`。

```text
Cursor ACP → Worker 工具通道 → Work 授权 / 独立审批 / 派发
           → MCP search → Runtime 读取主体 → Knowledge Provider → 对象权限与来源
```

## Linux 部署

在独立 Python 虚拟环境安装 `dios-sdk` 和本包的 wheel，然后启动：

```sh
dios-knowledge-mcp --config /absolute/private/bindings.json --port 13187
```

服务仅监听 `127.0.0.1`，工具路径 `/mcp`。配置文件及其父目录须由服务账户管理，凭据文件权限设为 `0600`。跨主机入口的 TLS、身份交换与网络策略沿用部署方案，不直接暴露该本地端口。

```json
{
  "version": 1,
  "runtime_url": "http://127.0.0.1:13081",
  "bindings": [{
    "token_sha256": "MCP_BEARER_TOKEN_SHA256",
    "enabled": true,
    "expires_at": 1800000000,
    "runtime_token_file": "/absolute/private/runtime-reader-token",
    "runtime_token_sha256": "SHA256_OF_EXACT_FILE_BYTES",
    "principal": {
      "issuer": "CONFIGURED_ISSUER", "audience": "dios-runtime",
      "scope": "CONFIGURED_SCOPE", "id": "knowledge-reader",
      "subject": "CONFIGURED_SUBJECT", "actor": "CONFIGURED_ACTOR"
    }
  }]
}
```

MCP 凭据与 Runtime 凭据分别创建。Runtime 凭据必须属于配置指定的读取主体，operator 凭据被拒绝。授予该主体需要检索的精确对象版本的 `object.read`、`object.download`；无需授予资料修改或任务管理权限。每次请求重查绑定、到期时间及 Runtime 身份，结果返回前再次检查绑定。配置和凭据变更无缓存。

## 工具注册

将工具注册到已有 `FileToolCatalog`：`transport=mcp-http`、`url=http://127.0.0.1:13187/mcp`、`name=search`，输入 Schema 使用 `dios_knowledge_mcp.server.INPUT_SCHEMA`，`approval_required=true`。`credential_ref` 引用受控 MCP 凭据，按现有工具注册摘要生成精确 `resource_version`，并将该权限与凭据引用纳入 Work 策略。Worker 侧通过别名使用工具；Agent 无需取得两类凭据。

工具只接受 `query` 和 `limit`；身份来自服务端配置。结果保留 `source_id/source_revision/object_ref/provenance/descriptor`。采用现有 MCP `2025-06-18` 协议，SDK 固定 `mcp==1.30.0`，上游许可证为 MIT。请求上限 64 KiB；结果上限 900 KiB，为控制面 1 MiB 传输上限保留封装空间；超限显式失败，不截断资料。

## 历史与撤销

新调用重新检索当前权限与来源。已经完成的工具调用按 invocation ID 重放其原始回执，属于历史执行记录；来源删除不会重写历史回执。历史结果仍受 Work/Attempt/Session 所有权约束，Work 撤销后 Worker 不再取得该回执。要求同时撤销衍生报告或历史回执的应用，应采用独立成果生命周期策略；当前工具适配器不声明跨历史产物的自动删除传播。

## 验证

适配器边界测试：`python -m unittest discover -s adapters/knowledge-mcp/tests -v`。

在仓库根目录运行隔离的真实 HTTP 与 Linux Unix socket 资格验证：

```sh
backend/.venv/bin/python scripts/qualify_knowledge_tool.py \
  workspace/NEW_QUALIFICATION_DIRECTORY \
  --adapter-python /absolute/adapter-venv/bin/python
```

脚本使用合成资料和受控任务，启动独立 Runtime/MCP 进程，验证审批、授权、引用、删除、历史重放和撤销，结束后停止自身进程并删除临时凭据。不调用模型。Cursor ACP 上游工具桥沿用已有真实任务证据。
