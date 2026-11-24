# 认证身份与委派签发

应用通过已认证的身份，从已有父授权派生委派。DiOS 记录签发身份、来源会话和授权范围，并在后续执行中检查这些依赖。OIDC 注销事件可撤销该会话签发的委派及其下游工作。

## 机制与边界

身份认证复用现有 `PrincipalProvider`：本地受信身份目录、标准 OAuth Token introspection、浏览器 OIDC 登录和来源凭据管理。授权链复用 Runtime 事务及原生 OPA。根授权继续由主机管理员通过 CLI 配置。

[RFC 8693 §4.1](https://www.rfc-editor.org/rfc/rfc8693.html#section-4.1)区分当前 actor 与历史 actor。DiOS 以认证得到的 actor 校验父授权的当前受托方，签发记录保留原始主体和实际签发者。以下接口管理 Runtime 授权记录；OAuth Token Exchange 继续由既有身份 Provider 承担。

OPA 采用[官方 external-data Option 2](https://www.openpolicyagent.org/docs/external-data)：Runtime 提供事务内权威快照。发行时与等待 OPA 后，均检查签发身份当前有效、权限仍覆盖委派范围、期限未超出身份期限。每次执行检查还验证已捕获的签发身份；身份服务暂时不可用时拒绝本次执行，保留恢复依据。

## 接口

| 接口 | 用途 |
| --- | --- |
| `POST /api/os/runtime/delegations` | 从父授权派生子级，返回授权记录 |
| `GET /api/os/runtime/delegations/{reference}` | 签发者查询自己的授权链和审计事件 |
| `POST /api/os/runtime/delegations/{reference}/revoke` | 签发者按 `expected_version` 撤销授权 |

派生请求包含 `idempotency_key`、`parent_ref`、`profile_id`、目标 `actor`、完整五字段 `permissions` 和 Unix 秒 `expires_at`。原始 subject 从父授权继承；签发者从认证上下文获取；OPA 配置从父授权继承。请求不能指定身份、scope、来源凭据或 PDP。

身份目录按 profile 显式授予 `delegation.issue`、`delegation.read`、`delegation.revoke`。签发身份的当前权限集合须覆盖全部待委派权限，且其 actor 必须匹配父级 actor。子级仍须满足原有三层深度、权限子集和期限收窄规则。API 管理接口只接受认证主体；本地主机管理员使用现有 CLI。

幂等键按认证身份和用户提供的 key 分区。重复请求返回原记录，内容变化返回冲突；传输超时保留原 key 重试。查询及撤销只允许原签发身份，并再次检查当前动作权限。管理员可通过 CLI 处理已失效身份留下的授权。

Python SDK 的 `TaskClient` 提供 `issue_delegation(...)`、`delegation(reference)` 和 `revoke_delegation(reference, expected_version=...)`，沿用 HTTPS、禁止重定向转发凭据及传输结果不确定性处理。

## 执行与撤权

`runtime_delegation_issuers` 保存不可变的签发身份绑定、受信身份目录路径和 profile。记录不含原始 Token；备份与恢复识别新增 schema。准入、Work/Grant 检查、工具派发及结果接收都会沿链核对签发身份。已观察到的签发权限收回进入现有工作授权撤销流程，恢复身份权限不会复活已撤销 Work。

经过既有签名校验的 OIDC Back-Channel Logout 事件，按 issuer、client、session 或 subject 匹配签发记录。在同一 Runtime 事务中，相关委派递增代次并置为 revoked，依赖的活跃 Work/Grant 失效，审计事件关联注销回执。同一工作即使同时是直接会话任务和委派依赖，也只计一次。

新会话不继承旧会话签发权限的有效性。已完成报告与旧授权回执继续保留。委派来源注销和任务发起人注销分别保留各自证据；上层业务按应用语义解释身份与资源。

## 验证与待完成项

[证据记录](evidence/delegation-identity-20261124.json)覆盖原生 OPA、认证 HTTP 路由、SDK、签发者权限变化、数据库备份恢复和真实 OIDC 会话下的 Worker 执行与注销撤权。

工作执行会主动复核签发身份，标准注销事件可即时更新持久授权。独立委派记录的后台身份巡检、跨 Provider 的统一撤权排序、跨服务 audience 转换和物理多主机容量验证仍待推进。外部授权源查询与本地提交之间存在跨服务窗口，按现有 E2AG 成熟方案矩阵继续限定和验证。
