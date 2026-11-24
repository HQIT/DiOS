# 企业身份接入与上游授权生命周期

2026-11-24 · #33 / #35 · 成熟实现资格与接入顺序

DiOS 接入企业已有身份服务，以可信发行方和 subject 标识主体。企业、部门、岗位、项目成员仍由上层系统管理。身份服务、对象授权 Provider、E2AG 工作授权各自保留职责；OAuth 登录状态不直接生成工具权限。

## 实现选择

| 能力 | 成熟实现与标准 | DiOS 接入责任 |
| --- | --- | --- |
| 浏览器登录、授权码、PKCE、OIDC ID Token | OAuth2 Proxy 原生浏览器入口；Authlib/joserfc 协议资格；企业 IdP，Keycloak 作为资格实例 | 固定可信发行方和回调，持久关联 state/nonce，验证签名、受众与主体映射 |
| 浏览器会话托管 | OAuth2 Proxy 支持 Cookie/Redis 会话后端，可作部署选项 | 已完成原生网关/Valkey 单实例资格；DiOS 入口隔离、CSRF、多来源绑定及多实例仍须接入验收 |
| API 令牌当前状态 | RFC 7662 Introspection；Authlib 原生客户端 | 明确 API audience、允许的 client、过期时间和 scope；不可达时拒绝新准入，区分身份撤销与暂时无法观察 |
| 下游令牌交换 | RFC 8693；Keycloak 标准 Token Exchange | 受众和范围收窄、原工作期限、subject/actor 分离、来源引用及撤销依赖 |
| 用户退出与安全事件 | OIDC Back-Channel Logout；SSF/CAEP | 校验签名、issuer/audience、事件类型、sid/sub、时间及重放；映射到既有授权撤销并进行恢复对账 |
| 长任务凭据与工作授权 | 既有 E2AG Work/Grant/Attempt/Session、Secret Provider | 接入源凭据生命周期；凭据留在可信服务和 Secret Provider，公开记录只含引用 |

[Authlib 官方客户端](https://docs.authlib.org/en/stable/oauth2/client/http/index.html)提供 PKCE、state 校验、刷新、撤销和 introspection。[OAuth2 Proxy 会话说明](https://oauth2-proxy.github.io/oauth2-proxy/configuration/session_storage/)提供 Cookie/Redis 两种存储。选定 Authlib 1.9.10 为 BSD-3-Clause，Keycloak 26.8.2 为 Apache-2.0；Keycloak 不成为 DiOS 强制依赖。

## 影响接入的约束

RFC 7662 的 active 是身份服务当前查询结果；缓存会延后撤销可见性。登录阶段采用新查询并验证返回的 issuer、sub、aud、exp 和 client_id。ID Token 用于登录验证，API 使用面向自身受众的 Access Token。[RFC 7662](https://www.rfc-editor.org/rfc/rfc7662.html)

Keycloak 的标准交换可收窄 audience；其访问令牌交换不提供自动上游撤销链，委派功能仍标为 Preview。返回成功、保留 sub 或获得新令牌，均不足以证明 #35 的受控代理链。交换期限、刷新期限和撤销后的行为须分别测试。[官方 Token Exchange 说明](https://www.keycloak.org/securing-apps/token-exchange)

[OIDC Back-Channel Logout](https://openid.net/specs/openid-connect-backchannel-1_0.html)提供标准注销事件，[SSF/CAEP](https://openid.net/specs/openid-caep-1_0.html)提供安全事件语义。事件传递延迟、丢失与重放仍须纳入 E2AG 验收；这些机制不提供 IdP 与 Runtime 派发事务间的原子提交。

## 按既定任务接续

1. **#33 身份准入**：通过原生协议资格后接可信发行方、API audience、主体映射和密钥更新。CLI/SDK 使用独立客户端凭据；浏览器采用授权码与 PKCE。普通请求不能指定可信 issuer、subject 或 actor。
2. **#33 凭据生命周期**：使用成熟 Secret 服务接收源凭据引用并提供受控刷新；旧文件 Secret Provider 作为本地受限实现。覆盖轮换、重启、凭据失效和日志脱敏，补完后接长任务。
3. **#35 上游依赖**：持久关联工作授权与源会话/凭据；以标准注销/安全事件和周期对账驱动撤销，失效后旧 Grant 保持失效。单独验证代理主体和完整作用域收窄。
4. **#34 执行控制**：请求、审批、续期和派发均检查当前依赖状态；身份服务故障进入可恢复的拒绝状态。严格撤销仍需通过原计划中的并发排序和效果接受端验证。

首个完整交付验收是：企业用户登录 → 提交工作 → 独立审批 → 上游撤销 → 待执行工具被阻止，恢复服务后合法新授权可执行；历史身份和已完成产物保持可核验。该门槛同时覆盖 API、CLI/SDK 和 Console，不以登录成功替代授权生命周期完成。

## 复现资格测试

使用隔离环境安装 `Authlib==1.9.10`、`httpx==0.28.1`；仓库已有 Playwright 与 Chromium。执行：

```bash
workspace/identity-qualification-33/bin/python scripts/probe_identity_provider.py \
  --node /path/to/node \
  --output workspace/identity-provider-20261124.json
```

探针使用固定摘要的官方容器、随机客户端凭据、独立 Realm、回环端口与真实浏览器授权码回调。结束后删除本次容器及临时凭据；不访问现有 DiOS 数据库或运行任务。开发模式的内嵌存储重启仅验证本实例会话保持，企业持久库、高可用、多主机和 TLS 为后续部署验收。

## 实测结果与验收门槛

[逐项证据](evidence/identity-provider-20261124.json)记录 25 项通过：真实 Chromium 登录、ID Token 签名/issuer/audience/nonce 校验、state 错配、错误 PKCE、授权码重放、未知令牌、受众正反对照、客户端认证、不同用户、刷新轮换与重放、受众收窄、源令牌撤销、无关用户保留、会话退出、服务身份、重启会话保持和服务离线。

同一测试中，源 Access Token 已被 introspection 判为 inactive，交换后的下游 Access Token 仍为 active；标准交换结果无 act 委派声明。测试配置下交换有效期未超过来源有效期。这三项是本实例观察结果，后续代理链、刷新与撤销验收须分别覆盖。

Authlib 低层客户端需由调用方保存授权请求返回的 state，并在换取令牌时传入该值；本探针包含错配拒绝测试。PKCE、state、nonce 各自独立验证。Authlib 1.9.10 与本仓库现用 httpx 0.28.1 的组合通过本轮检查，但库提示推荐迁移 httpx2；当前 Runtime 与真实身份服务集成回归覆盖该组合；httpx2 升级仍须独立验证传输兼容。

[Runtime 来源授权接入](runtime-oauth-source-33.md)已使用 Authlib/RFC 7662 接入已登记来源凭据的 API 准入、E2AG 工作复核与工具执行；真实 IdP 停机恢复、撤销和开发 Worker 链路通过验证。当前采用可信主机登记，Secret 支持 POSIX 文件及 [OpenBao KV v2 托管引用](managed-secrets-33.md)，Console 仍以 Access Token 登录。[浏览器 SSO 接入](browser-sso-33.md)记录原生网关、Valkey、身份源故障和既有 DiOS 来源验证的结果。下一步先分离主体与多来源绑定，再接企业登录入口；真实企业 IdP、签名密钥轮换、标准注销事件交付、三层委派和物理多主机继续按原 #33/#35 验收。
