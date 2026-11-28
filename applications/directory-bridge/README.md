# 目录授权桥接

把企业目录的用户、组成员关系转换为 DiOS 的版本化主体权限变更。该包独立安装，拥有业务映射配置，通过公开管理 CLI 对接 DiOS；没有后端模块或数据库依赖。

当前交付是 **可信目录快照 → 权限变更计划 → 运维执行 → E2AG 拦截**。企业在线 SCIM 同步、SSO 登录与多主体批量发布另行接续。

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

快照默认最多 300 秒，可配置 1–3600 秒；过期或未来时间拒绝。计划发布前重新确认目录有效性和 `valid_until`，将选中项的 `changes` 保存为受保护 JSON，使用对应 `expected_revision`：

```bash
PYTHONPATH=backend python -m app.runtime.cli --database-url "$DIOS_RUNTIME_DATABASE_URL" \
  --scope "$DIOS_RUNTIME_SCOPE" identity-update \
  --catalog /absolute/private/principals.json --principal application-a \
  --revision 1 --file /absolute/private/changes.json
```

运维发布流程负责检查计划新鲜度并归档原计划、输入摘要和每项回执；底座 `identity-update` 校验主体版本，不解释 SCIM 元数据。该切片不提供定时同步或自动批量执行。

发布递增主体版本，撤销旧 Work、Grant 和委派。相同版本与相同变更可重试；同一旧版本的冲突变更拒绝。停用后再启用保留主体及历史身份，旧执行授权继续失效。组成员移除清除对应 profile 权限；已存在的对象分享不由此映射器重新配置。

一次发布只覆盖一个主体。多主体操作中断时保留已成功回执，以新安全投影重新生成剩余计划；目录不可用或导出失败不能冒充空目录。明确完整的空目录会停用全部受管用户，需按人员批量停用流程审核。

## 验证

```bash
python -m pip wheel --no-deps applications/directory-bridge -w workspace/directory-bridge-dist
python -m pip install --no-deps workspace/directory-bridge-dist/*.whl
PYTHONPATH=backend:backend/tests python scripts/qualify_directory_bridge.py \
  --output workspace/directory-bridge-qualification
```

安装后包与真实 CLI/API/E2AG 在隔离 SQLite 上验证部门任务及成果隔离、成员移除、账号停用、旧版本拒绝、幂等重放、重新启用不复活旧授权。Worker 使用确定性测试适配器，不调用外部模型。

数据模型采用 [SCIM Core Schema（RFC 7643）](https://www.rfc-editor.org/rfc/rfc7643.html)，完整列表语义参考 [SCIM Protocol §3.4.2（RFC 7644）](https://www.rfc-editor.org/rfc/rfc7644.html#section-3.4.2)。该实现消费标准格式的完整导出；没有声明提供完整 SCIM 服务端。授权规则由受信平台产生、执行端再次校验的职责划分与 [OPA 外部数据接入](https://www.openpolicyagent.org/docs/external-data)一致；本切片复用 DiOS 现有 E2AG 更新入口。
