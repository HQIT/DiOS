# 目录授权桥接

把企业目录的用户、组成员关系转换为 DiOS 的版本化主体权限变更。该包独立安装，拥有业务映射配置、SCIM 连接、私有 SQLite 同步账本，通过公开管理 CLI 对接 DiOS；没有 DiOS 后端模块或私有运行表依赖。

支持 **在线目录 → 版本化权限计划 → 可恢复逐主体发布 → E2AG 拦截**，以及完整导出快照的离线计划模式。企业真实目录、SSO 登录和新主体自动开通继续联验。

## 快速体验

在仓库根目录的独立 Python 环境运行：

```bash
python -m pip install ./applications/directory-bridge
python applications/directory-bridge/examples/write_demo.py /tmp/dios-directory-demo
dios-directory --snapshot /tmp/dios-directory-demo/snapshot.json \
  --mapping /tmp/dios-directory-demo/mapping.json \
  --current /tmp/dios-directory-demo/current.json
```

输出中，部门 A 的用户获得报告读取权限，部门 B 已停用用户得到 `enabled=false`、空配置权限。示例使用合成数据，只生成计划。计划含来源用户/组 ID、输入摘要、预期主体版本和有效期；重复计算同一已发布状态不产生变更。

## 可信输入

`snapshot` 是导出封套：`version=1`、`source`、`observed_at`（Unix 秒）、完整的 SCIM `users` 和 `groups` ListResponse。只按稳定 `id` 关联，组名与用户自己携带的角色字段不产生权限。要求明确的 `User.active`；分页不完整、未知成员、嵌套组、重复 ID/JSON 键及超过 4 MiB 的输入均拒绝。嵌套组需由可信目录服务先展开。

`mapping` 由平台管理员维护：固定 `source`、DiOS `issuer/scope`、SCIM 用户 ID 到已有主体 ID 的映射，以及组 ID 到 `profiles` 权限的映射。它拥有所选专用主体的**全部 profile 权限**，不合并未知手工授权。多个角色的动作和完整权限元组取并集，DiOS 准入仍与 service/workload/resource 策略取交集。主体创建、令牌和 SSO 来源绑定由现有身份管理负责；桥接包不签发身份。

`current` 是以下受信管理命令输出组成的 JSON 数组，包含 issuer、scope、principal_id、revision、enabled、profiles，不含令牌或来源凭据：

```bash
PYTHONPATH=backend python -m app.runtime.cli --database-url "$DIOS_RUNTIME_DATABASE_URL" \
  --scope "$DIOS_RUNTIME_SCOPE" identity-policy \
  --catalog /absolute/private/principals.json --principal application-a
```

管理命令在 DiOS 的运维环境运行。目录桥接进程只接收安全投影，不读取后端私有表或凭据目录。终端用户继续使用自己的认证身份。

## 发布与恢复

离线快照默认最多 300 秒，可配置 1–3600 秒；过期或未来时间拒绝。离线计划发布前重新确认目录有效性和 `valid_until`，将选中项的 `changes` 保存为受保护 JSON，使用对应 `expected_revision`：

```bash
PYTHONPATH=backend python -m app.runtime.cli --database-url "$DIOS_RUNTIME_DATABASE_URL" \
  --scope "$DIOS_RUNTIME_SCOPE" identity-update \
  --catalog /absolute/private/principals.json --principal application-a \
  --revision 1 --file /absolute/private/changes.json
```

离线运维流程负责检查计划新鲜度并归档原计划、输入摘要和每项回执；底座 `identity-update` 校验主体版本，不解释 SCIM 元数据。在线同步器自动执行新鲜度检查、逐主体发布及回执归档。

发布递增主体版本，撤销旧 Work、Grant 和委派。相同版本与相同变更可重试；同一旧版本的冲突变更拒绝。停用后再启用保留主体及历史身份，旧执行授权继续失效。组成员移除清除对应 profile 权限；已存在的对象分享不由此映射器重新配置。

一次发布只覆盖一个主体。多主体操作中断时保留已成功回执，以新安全投影重新生成剩余计划；目录不可用或导出失败不能冒充空目录。明确完整的空目录会停用全部受管用户，需按人员批量停用流程审核。

## 在线生命周期同步

`dios-directory-sync` 适用于单 Linux 主机上的受管主体。按 `mapping.subjects` 与 `mapping.roles` 中的稳定 ID 调用 SCIM `GET /Users/{id}`、`GET /Groups/{id}`，请求必要属性；有 ETag 时使用条件 GET。组的完整成员数组必须明确返回，其他用户的成员引用在上层过滤。读取不到必需字段、未知嵌套组或不完整响应时停止该轮发布。

已知 ID 的读取覆盖停用、移组、删除及重新启用。目录列表自动发现、创建新 DiOS 主体及映射范围变更继续单独接入；已有同步账本固定来源、目标和受管主体集合，配置跨命名空间变更会拒绝。角色权限可调整，下一轮按最新映射对账。

受保护的连接配置示例（文件和 Token 文件均须属主独占读写）：

```json
{
  "base_url": "https://directory.example/scim/v2",
  "token_file": "/run/directory-bridge/scim-token",
  "runtime_command": ["/opt/dios/backend/.venv/bin/python", "-m", "app.runtime.cli"],
  "catalog": "/etc/dios/private/principals.json",
  "timeout_seconds": 5,
  "max_age_seconds": 300,
  "missing_resources": "reject"
}
```

运行环境提供 DiOS 管理 CLI 所需的数据库连接与 `PYTHONPATH`。执行身份必须已获管理授权；目录凭据不会进入命令参数、Runtime 输入或日志。HTTPS 启用服务器证书验证；HTTP 仅用于回环开发服务；禁止重定向及继承代理。每次请求有覆盖完整响应读取的时间与大小上限。

```bash
# 执行一轮；--state-dir 目录须为属主独占（0700）。
dios-directory-sync --config /etc/directory-bridge/connection.json \
  --mapping /etc/directory-bridge/mapping.json --state-dir /var/lib/directory-bridge

# 持续运行，限流/故障退避到期后自动复核；不会安装后台服务或创建聊天。
dios-directory-sync --config /etc/directory-bridge/connection.json \
  --mapping /etc/directory-bridge/mapping.json --state-dir /var/lib/directory-bridge \
  --watch --interval 60

dios-directory-sync --state-dir /var/lib/directory-bridge --status
```

默认 `missing_resources=reject` 将 404 作为需检查的来源问题。只有目录管理员确认该凭据能读取完整受管范围、404 明确表示资源删除后，才配置 `disable`；此时仍要求符合 SCIM Error 格式的 404。401/403、普通网页 404、网络错误及限流均不产生删除或停用指令。

上层 SQLite 账本先持久化本轮计划和待发布变更，再调用管理 CLI；逐条成功后保存回执，全部完成后推进 `checkpoint`。该水位表示成功发布的本地批次；来源采用逐资源最终一致读取，保存 ETag 并以本轮开始时间限制观测有效期。撤权延迟受同步间隔、读取和故障退避影响，已发布的主体版本由 E2AG 在执行边界继续检查。

中断后先重新读取目录和当前主体。仍有效的未确认变更沿用原预期版本重放；已过时的授予被新计划替换。重复触发由独占锁拒绝；管理 CLI 子进程继承该锁，父进程退出后仍在执行的发布不会与下一轮重叠。账本记录限流/来源不可用/认证失败/发布未确认等分类错误和下次重试时间；5 秒起指数退避、普通故障最多 300 秒，遵守最长 3600 秒的 `Retry-After`。

## 验证

```bash
python -m pip wheel --no-deps applications/directory-bridge -w workspace/directory-bridge-dist
python -m pip install workspace/directory-bridge-dist/dios_directory_bridge-0.2.0-py3-none-any.whl
PYTHONPATH=backend:backend/tests python scripts/qualify_directory_bridge.py \
  --output workspace/directory-bridge-qualification
```

42 项测试通过：安装后包、本地 SCIM HTTP 服务与真实 CLI/API/E2AG 在隔离 SQLite 上验证部门任务及成果隔离、条件请求、成员移除、账号停用、删除、限流恢复、独占发布、旧版本拒绝、回执丢失恢复、重新启用不复活旧授权。Worker 使用确定性测试适配器，不调用外部模型。

数据模型采用 [SCIM Core Schema（RFC 7643）](https://www.rfc-editor.org/rfc/rfc7643.html)，完整列表语义参考 [SCIM Protocol §3.4.2（RFC 7644）](https://www.rfc-editor.org/rfc/rfc7644.html#section-3.4.2)。该实现消费标准格式的完整导出；没有声明提供完整 SCIM 服务端。授权规则由受信平台产生、执行端再次校验的职责划分与 [OPA 外部数据接入](https://www.openpolicyagent.org/docs/external-data)一致；本切片复用 DiOS 现有 E2AG 更新入口。
