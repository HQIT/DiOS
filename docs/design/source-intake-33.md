# 托管 OAuth 来源自动登记

2026-11-24 · #33 / #34

企业 Access Token 经 Authlib/RFC 7662 验证后，按已配置的 issuer、subject、client_id、scope 和 DiOS 主体映射准入。首次请求将凭据与来源元数据一起写入 OpenBao KV v2；后续请求读回同一固定版本，E2AG 继续绑定具体来源、审批和工作期限。主体权限配置保持只读。

## 原生能力与接入职责

[OpenBao KV v2](https://openbao.org/docs/secrets/kv/kv-v2/)支持一条记录包含结构化数据、CAS 和独立的 create/read/update/delete 权限。登记使用 `cas=0`，记录键由可信主体映射、Provider 配置和令牌摘要确定，版本固定为 1。凭据和来源元数据由服务端一次提交，消除了凭据与本地索引分别写入的中间状态。

DiOS 验证身份映射、API audience、有效期和必需 OAuth scope，再调用该存储机制。写入结果以读回记录为准；网络丢失回执、并发冲突和请求重试不会产生新版本。读回时重新验证主体、来源位置、摘要及映射。固定版本记录失效不会回退到最新版本。

[OAuth Introspection](https://www.rfc-editor.org/rfc/rfc7662.html)仍是来源当前状态的依据。新令牌形成独立记录；OAuth 刷新继续由原生登录组件负责。旧 Work/Grant 的来源及绝对期限不被自动改写。

## 配置

可信主体目录增加可选 `oauth_intake`，现有 OAuth Provider 的 `secret_store` 用于只读解析；两者指向同一服务地址。每个自动准入主体声明 `oauth_identity`，并使用已有 `source_refs` 格式。以下为目录增量片段：

```json
{
  "oauth_intake": {
    "writer": {
      "address": "https://secrets.example",
      "token_file": "/run/dios-intake/token",
      "timeout_seconds": 2
    },
    "mount": "dios-sources",
    "prefix": "admission",
    "timeout_seconds": 5,
    "max_source_ttl_seconds": 3600
  },
  "principals": {
    "employee-portal": {
      "source_refs": [],
      "tokens": [],
      "oauth_identity": {
        "issuer": "https://identity.example/realms/company",
        "subject": "<verified enterprise subject>",
        "client_id": "approved-portal",
        "required_scopes": ["openid", "dios.execute"]
      }
    }
  }
}
```

原有主体身份、权限、期限及 `oauth2` 字段仍须完整提供。自动登记同一作用域内要求映射唯一；配置不根据邮箱、任意请求头或任务内容自动授予权限。来源最大剩余期限上限可配置为 60—86400 秒；超过上限的令牌拒绝登记，已登记记录保持原期限。具体任务仍须满足既有最短工作期限要求。

自动登记来源保存必需 scope，后续 introspection 一旦不再包含这些 scope，已有 E2AG 工作被拒绝并持久撤销。手工配置的旧来源沿用显式来源契约，可在该引用中声明 `required_scopes`；启用自动登记不会改写旧引用。

## 角色与故障

写入角色仅授予 `dios-sources/data/admission/*` 的 create/read；读取角色只有 read。写入角色没有 update、delete、list、undelete、destroy 或 metadata 权限。[策略示例](../../examples/runtime/openbao-intake.example.hcl)与既有原生 Agent 配合使用，Agent 负责服务令牌续期。部署时由 API 持有写入 sink，执行控制端使用读取 sink，按主机权限与挂载隔离。

| 场景 | 结果 |
| --- | --- |
| 六路并发提交同一新来源 | 同一绑定、一个 KV 版本 |
| 服务端已提交，客户端未收到写入回执 | 读回原记录，继续同一来源 |
| 写入服务身份失效 | 已登记来源继续使用只读角色；新来源等待写入服务恢复 |
| Secret 服务或身份服务不可观察 | 拒绝新准入/派发，保留既有审批 |
| 已登记来源被软删除或销毁 | 执行被拒绝；CAS=0 和角色策略阻止重新登记覆盖墓碑 |
| 管理者恢复 Secret | 已撤销 Work/Grant 保持失效；新工作重新准入 |
| 主体映射、修订或 Provider 变化 | 新来源按新配置登记，旧绑定按既有撤销规则复核 |

该记录也是登记结果，不需要写回主体 JSON 文件或另建本地索引。成功写入后调用方退出，下一次请求仍能发现同一记录。HTTP 身份验证的外部网络调用移出 Runtime 数据库读事务，避免等待身份服务时持有 SQLite 读锁。

登记总 deadline 默认示例为 5 秒，可配置 0.05—10 秒；每次 Secret 请求和 introspection 继续受各自有界超时及响应上限约束。普通认证结束前再次验证当前来源；跨服务检查与派发间的严格排序继续由 #34 验收。

## 保留与清理

原生 `delete_version_after` 可使过期记录不可读，应在专用挂载启用，并让保留时间覆盖已允许的来源最大期限。有效期校验仍以身份服务和 E2AG 为准。真实测试覆盖原生定时删除。

软删除保留加密数据和墓碑。[官方删除语义](https://openbao.org/docs/secrets/kv/kv-v2/)区分软删除与 destroy；物理销毁按企业留存策略由独立运维角色执行。有效来源期间保留版本元数据，防止删除全部 metadata 后重新创建同名凭据。API 登记角色没有执行这些清理操作的权限。

## 验证与下一步

[工程证据](evidence/source-intake-20261124.json)记录后端 513 项（386 通过、127 条件跳过）、真实 OpenBao/IdP 13 项及最终专项回归。开发环境两个新来源自动登记后完成真实 Worker、独立审批、报告校验及 CLI/SDK；两组浏览器各 7 项通过。原生写入 Agent 续期和服务重启后的来源/历史读取已验证，57 条原历史保留，任务总数为 59。

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -p test_runtime_source_intake.py -v
cd ..
backend/.venv/bin/python scripts/probe_source_intake.py \
  --source-config /private/dedicated-source.json \
  --secret-fixture-directory workspace/validation-20261124-managed-secrets \
  --output workspace/source-intake-native.json
```

真实探针要求专用开发 IdP 来源和 development-secrets 标记的 OpenBao 实例。它创建独立 KV 挂载与角色，结束后清理这些测试资源；Runtime 数据库独立。

下一步接 Console 的可信网关入口与 CSRF，并完善来源刷新后的提交回执恢复。标准注销事件、受控长任务续期、企业 TLS/主机隔离和跨服务严格撤销沿用 #33/#35/#34 的验收计划。
