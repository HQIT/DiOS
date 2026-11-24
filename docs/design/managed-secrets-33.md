# 托管来源凭据与 E2AG

2026-11-24 · #33 / #34

OAuth 来源令牌和 introspection 客户端密钥支持 OpenBao KV v2 固定版本引用。OpenBao 管理 Secret 的存储、版本和访问策略，原生 Agent 管理服务身份认证、续期与重认证；DiOS 通过官方 HTTP API 读取已登记版本，并继续核验来源令牌和 E2AG 工作授权。

## 选型依据

[KV v2](https://openbao.org/docs/secrets/kv/kv-v2/)提供版本读取、CAS 写入、版本删除与销毁，[HTTP API](https://openbao.org/docs/api/secret/kv/kv-v2/)提供固定版本及状态元数据。[原生 Agent](https://openbao.org/docs/agent-and-proxy/autoauth/)在续期失败或失效后重新认证，失败时退避。验证版本为 OpenBao 2.7.3，使用官方镜像摘要和原生 AppRole；项目采用 [MPL-2.0](https://github.com/openbao/openbao/blob/v2.7.3/LICENSE)。

此 Provider 为可选部署组件。现有文件来源配置继续工作；企业已有同类服务可按接口另行适配与资格验证。浏览器登录沿用 #33 计划，OAuth2 Proxy 的[服务端会话](https://oauth2-proxy.github.io/oauth2-proxy/configuration/session_storage/)作为候选，尚未完成 DiOS 登录接线。OpenBao 服务令牌续期与 OAuth 用户来源令牌刷新分别管理，本接入实现前者。

## 配置

可信主体目录的 oauth2 可使用以下客户端密钥配置；issuer、audience、endpoint、client_id 沿用[来源授权契约](runtime-oauth-source-33.md)。

```json
{
  "secret_store": {
    "address": "https://secrets.example",
    "token_file": "/run/dios-secrets/openbao-token",
    "timeout_seconds": 2
  },
  "client_secret_ref": {
    "mount": "dios-secrets",
    "path": "introspection-client",
    "version": 1,
    "field": "value"
  }
}
```

client_secret_ref 与 client_secret_file 二选一。主体 upstream 的 path 可替换为 secret_ref：

```json
{
  "secret_ref": {
    "mount": "dios-secrets",
    "path": "sources/alice",
    "version": 1,
    "field": "value"
  }
}
```

来源 ref、version、sha256、issuer、subject、audience、client_id、expires_at 仍必需。KV version 必须为正整数，禁止 latest/0；来源引用版本与 Secret KV 版本分别表达授权登记和存储版本。Secret 内容必须与来源摘要匹配。KV 路径、字段和版本参与来源摘要，可信 Secret 服务地址参与 Provider 摘要；公开任务仅保留摘要和来源身份，不暴露 Secret 路径、服务令牌或客户端密钥。

网络请求要求 HTTPS，回环测试实例可使用 HTTP；TLS 可通过 ca_file 指定企业 CA，校验保持启用。请求不继承代理环境、不跟随重定向。读取与 introspection 共用 OAuth 总超时预算，默认 2 秒；Secret 响应上限 64 KiB，选定字段限 16 KiB ASCII 无空白凭据。每次检查重新读取私有 Agent sink 和固定 Secret 版本，无本地有效状态缓存。

## 生命周期与执行语义

| 情况 | 行为 |
| --- | --- |
| 服务令牌续期、重认证或 sink 原子替换 | 下次读取使用新服务令牌，来源绑定及原工作期限保持 |
| 固定来源版本被删除、销毁或不存在 | 来源授权失效；E2AG 持久撤销对应 Work/Grant |
| 已观察删除后恢复 Secret | 旧 Work/Grant 保持失效，新任务重新准入 |
| KV 版本与登记不符、格式错误、403、限流、网络故障或服务 sealed | 身份服务暂不可用，拒绝新派发，保留已有审批和工作授权 |
| introspection 客户端 Secret 不可用 | 身份服务暂不可用，恢复客户端配置后重新检查 |
| 预定未来删除 | 删除生效前可用，每次查询重新检查；期限届满或实际删除后失效 |

读取权限只授予具体 source/client 数据路径。DiOS 使用的服务身份不持有写入、删除、其他来源读取或 root 权限。E2AG 的作用域、原期限、审批消费和执行控制点保持原契约；Secret 服务的原子版本操作不消除 IdP 查询与 Runtime 提交之间的跨服务排序窗口。

## 部署和验证

[Agent 配置示例](../../examples/runtime/openbao-agent.example.hcl)与[只读策略](../../examples/runtime/openbao-reader.example.hcl)用于独立服务部署。Agent sink 显式设置 0600；默认读取后移除 AppRole SecretID。Agent 重启时需由既有运维 Secret 投递机制重新供给；长期无人值守部署优先结合平台已有工作负载身份或证书认证。开发实例保留一个 12 小时的专有 SecretID 文件用于重启，源 OAuth Token 仍按自身期限失效。

运行资格测试：

```bash
backend/.venv/bin/python scripts/probe_secret_provider.py \
  --output workspace/secret-provider.json
cd backend
PYTHONPATH=.:tests .venv/bin/python -m unittest test_runtime_openbao test_runtime_oauth_source -q
```

原生探针仅使用随机生成的测试 Secret，在回环 13189/13190 启动独立 Raft 实例和原生 Agent。测试包括 CAS、固定版本、最小权限、自动续期、最大期限重认证、服务令牌撤销恢复、版本删除/销毁、重启 sealed 后解封与数据保留；结束后移除该探针容器和临时目录。

真实来源执行探针 `scripts/probe_managed_source_runtime.py --fixture-directory <专用开发夹具目录> --output <证据文件>` 要求标记为 development-secrets、地址为 http://127.0.0.1:13188 的 OpenBao 实例、专用有效 OAuth 来源和测试管理凭据。它创建独立测试 KV 键及只读服务身份，覆盖服务令牌失效后恢复原审批、实际 MCP 初始化时删除来源、零额外工具调用及恢复 Secret 后旧工作持续失效；结束后清理自己的测试键、策略与服务令牌。通用业务数据库和已有来源键保持原状。夹具目录包含 containers.json（server 容器 ID）、upstream.json（provider/reference）与仅供测试管理的 fixture.json（root_token）；管理凭据限制为专有文件，禁止用于业务 Worker。

开发验证还覆盖来源 v4 → v5、KV v1 → v2 的显式登记切换，原历史保留；当前实现的 OAuth 用户令牌更新仍由可信管理流程执行。IdP 可在令牌 exp 之前返回 inactive，执行判定同时检查当前状态与期限。

[工程证据](evidence/managed-secrets-20261124.json)分别记录原生服务、适配器、E2AG 回归和开发部署结果。企业 TLS/托管解封、物理多主机与 HA、OAuth 用户凭据刷新、浏览器 SSO、通用 Worker Secret 挂载及证书签发轮换继续按 #33/#58 验收。

本轮后端全量 491 项：370 通过、121 按环境条件跳过，失败为零；托管凭据专项 23 项通过。原生服务 14 项、真实 KV/IdP/MCP 边界 7 项及真实 Worker/CLI/SDK/浏览器链路通过，53 条原历史保留。
