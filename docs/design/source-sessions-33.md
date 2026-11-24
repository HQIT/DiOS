# 独立 OAuth 来源与工作授权

2026-11-24 · #33 / #34

一个 DiOS 主体可以关联多个独立的 OAuth 来源版本。来源凭据由已有 Secret Provider 保管，Authlib/RFC 7662 校验当前状态；E2AG 工作绑定准入时的具体来源。追加来源、来源列表重排和另一会话刷新均保持原工作绑定及绝对期限。

## 配置

可信目录的 `oauth_sources` 保存版本化来源，主体的 `source_refs` 只列出已授权来源的目录键。每个来源沿用 [OAuth 来源契约](runtime-oauth-source-33.md)，包括 issuer、subject、audience、client_id、ref、version、sha256、expires_at，以及文件路径或 OpenBao 固定版本引用。

```json
{
  "oauth_sources": {
    "browser-a-v1": {"ref": "identity/session-a", "version": "v1", "...": "其余来源字段"},
    "browser-a-v2": {"ref": "identity/session-a", "version": "v2", "...": "原生刷新后的来源字段"},
    "browser-b-v1": {"ref": "identity/session-b", "version": "v1", "...": "独立浏览器会话的来源字段"}
  },
  "principals": {
    "application-user": {
      "source_refs": ["browser-a-v1", "browser-a-v2", "browser-b-v1"],
      "tokens": [],
      "...": "原有主体、权限、revision 与期限字段"
    }
  }
}
```

这是目录片段；占位字段须替换为有效来源及主体字段。现有 `oauth2` 顶层 Provider 配置保持原格式。目录总上限仍为 256 KiB；最多 1024 个来源，每主体最多 64 个引用。当前同一主体的来源必须具有相同 issuer、subject 和 client_id。多身份源联合、跨客户端委派按独立 Provider/委派契约推进。

目录拒绝重复来源摘要、重复 `(issuer, ref, version)`、悬空引用、同一来源关联多个主体、混合 `upstream/source_refs` 和混合来源/本地令牌准入。空 `source_refs` 表示当前无可用来源；它不会退化为无来源身份。目录应由可信管理进程原子替换。

旧 `upstream` 配置继续有效。将其原样移到 `oauth_sources` 并由 `source_refs` 引用，保持已签发工作绑定；转换时主体、revision、来源内容及 Provider 配置保持不变。旧格式附带的本地令牌属于其原有来源约束路径；新格式采用明确的来源凭据准入，本地服务身份使用独立主体。

## 运行行为

| 情况 | 当前行为 |
| --- | --- |
| 会话 A 刷新并追加 A v2 | 新请求可使用 v2；A v1 与 B v1 的工作继续按原来源和期限复核 |
| A 的令牌 inactive、期限已过或引用被撤回 | 拒绝 A 的执行；已有工作验证器持久撤销对应 Work/Grant；B 独立复核 |
| A 来源查询暂时失败 | 返回 AUTH_PROVIDER_UNAVAILABLE，保留 A 的审批；B 不读取 A 的凭据 |
| A 恢复可观察且原授权仍有效 | 复核原审批和同一调用；已撤销的旧授权保持失效 |
| 来源同版本内容被替换 | 原绑定摘要失配，拒绝旧绑定；不会覆盖任务中的来源证据 |
| 主体停用、revision 或权限变化 | 作用于该主体所有来源，沿用既定权限收窄和撤销规则 |
| 新来源读取旧任务与成果 | 依当前主体归属和对象权限读取；执行授权仍保留原来源 |

身份接口、任务和产物不包含其他会话的凭据、来源路径或来源目录。API、CLI/SDK 和 Console 共用认证入口，客户端协议保持原样。逻辑任务的幂等键仍按主体隔离；不同来源提交同一键但授权快照不同，沿用内容冲突语义，不产生第二个任务。自动刷新后的提交回执恢复属于后续浏览器接入验收。

## 验证与后续

[证据](evidence/source-sessions-20261124.json)记录后端 502 项（375 通过、127 条件跳过）、多来源专项 11 项、真实 IdP/MCP 8 项。开发环境两个来源各自完成 Worker 任务、独立审批、报告校验和 CLI/SDK 身份核对；两组浏览器各 7 项通过。55 条原历史保留，任务总数为 57。

真实集成创建同一账号的两个独立浏览器会话，使用 IdP 原生刷新追加来源版本；旧审批可执行且旧任务期限不变。撤销一路来源后，下一次 MCP 调用被阻止，另一会话的已审批工具仍成功。测试使用独立 Runtime 数据库，原始凭据不进入持久任务。

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -p test_runtime_oauth_sessions.py -v
cd ..
backend/.venv/bin/python scripts/probe_oauth_sessions.py \
  --fixture-directory workspace/validation-20261124-identity \
  --node /absolute/path/to/node \
  --output workspace/source-sessions-native.json
```

真实探针要求既有、标记为 development-identity 的专用 Keycloak 测试实例，凭据从其私有 fixture 读取；新建并撤销本次浏览器来源，使用回环 MCP 19999 端口。生产 IdP 不用于此探针。

本阶段完成多来源解析、绑定与执行控制。[托管来源自动登记](source-intake-33.md)已提供原子记录、幂等读回和原生到期策略；下一步接 Console 的可信网关入口和 CSRF；OAuth 刷新和会话存储继续采用已验证的原生组件。标准注销事件与跨服务严格撤销仍按 #35/#34 推进。
