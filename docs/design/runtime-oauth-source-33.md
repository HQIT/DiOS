# 上游身份与 E2AG 执行授权

2026-11-24 · #33 / #34 / #35

可信主体目录可登记 OAuth 来源凭据引用，并通过 [oauth_sources/source_refs](source-sessions-33.md) 为同一主体关联多个独立来源版本。API 准入、工作授权复核、工具审批及派发通过 RFC 7662 查询来源令牌的当前状态；Authlib 负责客户端认证，整个网络请求有超时和响应大小上限。普通任务载荷不能提供发行方、主体、客户端或凭据路径。

## 配置与执行边界

此版本使用既有可信主机配置方式：运维或上层凭据管理服务登记来源引用，令牌及客户端密钥可存于属主专有的 POSIX 文件，或使用[OpenBao KV v2 托管引用](managed-secrets-33.md)。API 接受已登记来源的 Bearer Token，匹配摘要后验证来源。启用 [oauth_intake](source-intake-33.md) 后，符合固定身份映射和必需 scope 的新来源可自动登记为 OpenBao 不可覆盖记录。Windows Secret ACL、浏览器自动登录与 OAuth 来源刷新仍由 #33 接续；托管服务当前覆盖固定版本读取和服务身份续期。

主体目录顶层增加 oauth2：

```json
{
  "issuer": "https://identity.example/realms/company",
  "audience": "dios-api",
  "endpoint": "https://identity.example/realms/company/protocol/openid-connect/token/introspect",
  "client_id": "dios-api",
  "client_secret_file": "/run/dios-secrets/introspection-client",
  "timeout_seconds": 2
}
```

对应主体增加 upstream，tokens 可为空；profiles、objects 和已有 API 动作继续使用原契约：

```json
{
  "ref": "identity/source-a",
  "version": "v1",
  "path": "/run/dios-secrets/source-a-v1",
  "sha256": "<SHA256 of the exact token without whitespace>",
  "issuer": "https://identity.example/realms/company",
  "subject": "<IdP subject>",
  "audience": "dios-api",
  "client_id": "approved-application",
  "expires_at": 1900000000
}
```

固定可信 issuer、audience、subject、client_id 与令牌摘要共同限定来源。来源期限不得超过 IdP 返回的 exp；工作期限取本地授权和来源绝对期限的较小值，数据库时钟偏差不能延长来源期限。可信发行方、受众、查询端点和客户端配置的摘要进入绑定；这些配置变化使旧执行绑定失效。令牌替换必须登记新引用版本；旧任务保留原来源绑定，不能借新令牌复活旧 Grant。当前实现不自动刷新来源令牌，长任务续期仍受原来源期限约束。

远程端点要求 HTTPS，HTTP 仅允许回环地址。请求不继承代理环境、不跟随重定向，默认总超时 2 秒，允许配置 0.05—5 秒，响应不超过 64 KiB。未缓存 active 判定。文件要求属主一致、无组/其他用户权限、普通文件且无末级符号链接；凭据只在可信服务内部读取，Worker/模型不会收到来源令牌或客户端密钥。

任务持久记录包含来源 issuer、subject、client、引用、版本、期限和登记摘要；不包含令牌、文件路径或客户端密钥。CLI 的 task identity、Python SDK 的 identity() 和 Console 使用同一个认证入口。上层企业与岗位关系保持在外部应用。

## 故障、恢复与撤销

| 观察 | 行为 |
| --- | --- |
| active=false、来源身份/受众/客户端不匹配、来源凭据被替换或期限已过 | 拒绝执行；工作验证器和工具入口持久撤销对应 Work/Grant |
| 超时、无法连接、HTTP 错误、缺失 active、格式错误或超大响应 | AUTH_PROVIDER_UNAVAILABLE；阻止新派发，保留已有审批和工作授权 |
| 服务恢复且原来源仍有效 | 同一工具调用可复核原审批继续执行；已派发调用沿用既有幂等/回执机制 |
| 旧来源被撤销后重新启用或登记新版本 | 旧工作授权保持失效，新任务重新准入 |

控制点覆盖待执行工具、审批后等待和 MCP 初始化后的最终检查。单次查询到本地提交之间仍存在跨服务排序窗口；严格撤销继续按 #34 原验收推进。已有 Worker 故障策略决定失败尝试是否重试；本适配不把身份查询失败解释为远端进程已死亡，也不新增自动重派任务。

浏览器区分身份服务暂不可用与来源授权失效，暂时故障不会显示为 Access Token 无效。来源更新后，具有相同可信主体归属和读取权限的应用仍可查询历史成果。

## 验证

单元及 Runtime 回归覆盖来源字段拒绝、总超时、错误响应、文件权限/符号链接、来源期限、任务输入无凭据、取消检查、审批保留、恢复后同一调用继续、MCP 初始化时撤销与旧 Grant 持久失效。

真实集成使用独立 Keycloak 26.8.2 与本地 MCP HTTP 端点，验证身份服务停机/重启后恢复原调用、源令牌在 MCP 初始化时被真实撤销、零额外 tools/call 以及工作授权持久撤销。运行前必须提供专用可撤销的测试来源：

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -p 'test_runtime_oauth_source.py' -v
DIOS_TEST_OAUTH_SOURCE_CONFIG=/absolute/private/upstream.json \
  .venv/bin/python -m unittest discover -s tests -p 'test_runtime_oauth_integration.py' -v
```

专用来源文件包含 provider、reference、revocation_endpoint、container；测试校验容器 dios.purpose=development-identity 标签，暂停再启动该容器，并撤销该测试来源令牌。MCP 端点使用回环 19999 端口。实际企业账号和共享生产身份服务不用于本测试。

[执行证据](evidence/runtime-oauth-source-20261124.json)记录全量 467 项（347 通过、120 环境条件跳过）、最终授权专项 68 项（54 通过、14 环境条件跳过），以及真实身份服务/MCP 故障恢复和撤销验证。专项覆盖全量测试启动后补入的可信配置摘要变更回归。开发环境 SDK 提交 → 独立审批 → Worker/OPA/E2AG → 带来源报告已完成，CLI 身份与 SDK 一致；浏览器通过主体隔离、报告、移动布局及中英文错误提示检查，52 条原历史保留。

[前置协议资格](enterprise-identity-33.md)与运行时接入分别记录；企业 SSO 产品入口、自动刷新、密钥托管、注销事件传播和三层委派保持既定计划。
