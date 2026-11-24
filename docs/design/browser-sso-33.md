# 浏览器 SSO 与 E2AG 来源接入

2026-11-24 · #33 / #34 / #35

企业浏览器入口采用 OAuth2 Proxy 的原生 OIDC 登录、刷新和服务端会话机制。资格组合为 OAuth2 Proxy 7.15.6、Keycloak 26.8.2 和 Valkey 9.2.0；固定镜像摘要见[机器证据](evidence/login-gateway-20261124.json)。Valkey 通过 Redis 协议实测互操作，采用 [BSD-3-Clause](https://github.com/valkey-io/valkey/blob/9.2.0/COPYING) 许可。企业已有 IdP 和兼容会话服务可另行验证接入。

## 能力与职责

| 组件 | 职责 | 交给 DiOS 的信息 |
| --- | --- | --- |
| 企业 IdP | 身份认证、Access Token 状态、原生刷新、会话退出 | 可信 issuer、sub、aud、client、exp；经验证可用的会话关联 |
| OAuth2 Proxy | 授权码、PKCE、state、nonce、浏览器票据、并发刷新锁 | 经过覆盖处理的 Access Token，仅传给受信任 API 入口 |
| Valkey | 加密会话存储与原生刷新锁，AOF 持久化 | 网关内部会话；不成为 DiOS 授权事实来源 |
| DiOS 身份 Provider | 验证来源、映射主体、版本化来源引用、多会话隔离 | subject/actor、权限、来源引用与期限 |
| OpenBao / Secret Provider | 受限保存来源凭据版本，托管服务凭据生命周期 | 固定版本 Secret 引用 |
| E2AG | 工作期限、工具审批、派发前复核、持久撤销和证据 | 绑定任务的 Work/Grant，当前允许或拒绝的原因 |
| 上层应用 | 企业/部门/岗位/项目业务关系和交付界面 | 使用 DiOS 能力接口与对象权限 |

[OAuth2 Proxy 服务端会话](https://oauth2-proxy.github.io/oauth2-proxy/configuration/session_storage/)使用浏览器票据和服务端存储，并支持刷新锁。登录、刷新、会话加密继续由成熟组件承担；DiOS 的新增工作集中于既有授权模型与这些组件的衔接。

## 实测边界

[逐项证据](evidence/login-gateway-20261124.json)共 25 项通过。真实 Chromium 验证了授权码与 PKCE S256、state/nonce 错配、回调重放、票据篡改、六请求并发刷新跨越原 Access Token 到期、独立用户、网关重启、AOF 存储重启、退出后旧票据重放及依赖恢复后的新登录。浏览器 localStorage/sessionStorage 中无令牌，票据具有 HttpOnly/Lax 属性。资格配置显式设置 `code_challenge_method=S256` 和 `insecure_oidc_skip_nonce=false`，防止默认设置跳过 nonce 校验。

身份头测试中，网关覆盖 `X-Forwarded-User` 和 `X-Forwarded-Access-Token`；配置中未使用的 `Authorization` 和 `X-Auth-Request-User` 可被原样透传。API 入口必须明确采用一种身份来源，清除无关身份头，并通过专用监听地址、网络隔离或 mTLS 限定网关来源。直接访问入口、重复头、混合 Bearer/Cookie 和伪造身份头均须有拒绝用例。公网 CLI/SDK Bearer 入口保持独立认证路径。

携带会话 Cookie 的 API 客户端在设置外站 Origin 后，POST 仍到达测试上游。此项说明网关没有替上游执行 Origin 校验；该 API 客户端测试不模拟浏览器 SameSite 行为。浏览器写接口需要同源校验及成熟框架提供的 CSRF 防护，不能仅依赖登录回调的 state 或 Cookie 的 Lax 属性。

IdP 停机时，网关可能继续转发已有会话。HTTP 200 仅表示请求到达测试上游；是否具有当前执行权限仍由 DiOS 的 RFC 7662 来源校验和 E2AG 决定。本组合在 Access Token 到期后返回 302；IdP 恢复后原客户端仍需重新登录，新的真实浏览器登录成功。同一故障场景调用现有 DiOS `OAuthSource.current()`：来源正常时通过，IdP 停机时返回 `AUTH_PROVIDER_UNAVAILABLE`，过期后返回 `AUTH_PRINCIPAL_EXPIRED`。本轮直接验证来源控制点；完整工作审批恢复与工具阻断沿用[已完成的 Runtime 集成验证](runtime-oauth-source-33.md)。

本地退出清除代理会话后，旧票据重放被拒绝，但 IdP SSO 可以自动重新登录。配置 `backend_logout_url` 后，本组合实测来源令牌变为 inactive，另一个用户仍可访问。[官方退出说明](https://oauth2-proxy.github.io/oauth2-proxy/features/endpoints/)要求区分代理退出和身份服务退出。该配置的成功案例不代表标准注销事件投递、丢失恢复和所有 IdP 已验收。

会话存储短暂停机后，原客户端观察到重新登录重定向；恢复不能仅看网关健康检查。恢复验收同时检查会话结果、来源状态与用户可见提示。

## 与现有代码的接入顺序

`source_binding()` 把来源版本和摘要绑定到工作授权。[多来源绑定](source-sessions-33.md)已支持主体引用多个来源版本；追加来源保持其他会话和历史工作绑定。[托管来源自动登记](source-intake-33.md)已接入，下一步接 Console 登录入口。

1. **主体与来源引用分离（已接入）**：主体权限和 revision 保持现有契约；来源按不可变版本保存，同一主体允许多个有效来源。任务保存准入时的来源引用，查询和执行依该引用复核，不改写历史 Work。退出或撤销按已验证的来源/会话范围定位。
2. **可信来源准入（已接入）**：凭据经固定 issuer、API audience、client、subject、必需 scope 与期限校验，再按可信映射登记。OpenBao 原生 CAS=0 将凭据和来源元数据存为一条不可覆盖记录；重试读回固定版本，登记无需改写主体目录。到期不可读采用原生保留策略。
3. **受保护浏览器入口**：接入网关专用认证路径、CSRF 和统一错误语义；Console 提供企业登录/退出状态。CLI/SDK 沿用各自已授权客户端，浏览器和服务端均不把网关 ID Token 当成 DiOS Access Token。
4. **刷新与工作授权衔接**：网关负责 OAuth 刷新。新令牌形成新来源版本供新请求使用；已有工作保留原绝对期限。长任务通过既定续期接口重新验证权限、审批和来源依赖，旧 Grant 的撤销状态保持单调。后台服务身份按原工作负载身份计划接入。
5. **注销事件与恢复对账**：按 #35 接 OIDC Back-Channel Logout / 安全事件，校验 issuer、audience、sid/sub、签名与重放，结合当前状态查询处理丢失事件。不同会话的关联标识及可用性须针对真实 IdP 资格验证；不从邮箱或任意请求头推断。

首批产品验收：两个浏览器会话并行登录同一主体 → 各自提交工作 → 一路刷新不破坏另一路绑定 → 独立审批 → 撤销一路来源阻断对应待执行工具 → 无关会话继续 → 恢复服务后原合法审批可复核继续。另行覆盖重复登记、Secret 写入中断、重复/乱序注销事件、历史成果读取和跨入口伪造请求。

## 复现与部署条件

```bash
backend/.venv/bin/python scripts/probe_login_gateway.py \
  --node /absolute/path/to/node \
  --output workspace/login-gateway-qualification.json
```

需要 Docker、后端依赖、仓库 Playwright 和 Chromium。探针检查回环端口 13192/13194/13195/13196 空闲，使用隔离 Realm、随机凭据及独立临时 AOF 目录，结束后清理本次容器。只调用现有 OAuth 来源验证器；现有 DiOS 服务、数据库及运行任务保持原状态。

本次覆盖单机容器重启与依赖故障；企业部署还须验收 HTTPS、Secure Cookie、可信代理链、密钥托管轮换、物理多主机及 HA。回环 HTTP 和短期测试账号配置仅用于此探针。Console 当前仍使用 Access Token 登录，浏览器 SSO 产品接线按上述顺序推进。
