# 数字员工与工作项

同一数字员工可按工作类型绑定不同执行配置，保留稳定身份、职责、版本和任务历史。WorkItem 负责业务目标、员工绑定、成果关联和人工确认。模板、修订与工作项存放在独立 SQLite 中；DiOS 提供任务、成果和 E2AG 执行治理。

这是客户无关数字分身平台的独立 Linux/WSL 管理包。当前入口为受信任运维 CLI；多人 Web 管理和业务角色审批由后续平台入口承接。

## 安装

在仓库根目录、独立 Python 3.12 环境中执行：

```bash
pip wheel --no-deps sdk/python applications/workforce-registry -w workspace/workforce-dist
pip install workspace/workforce-dist/dios_sdk-0.1.21-py3-none-any.whl workspace/workforce-dist/dios_workforce_registry-0.2.0-py3-none-any.whl
dios-workforce --help
dios-workitem --help
```

运行前由 DiOS 管理员预置每名员工独占的主体、凭据和 E2AG 策略。专用主体由员工生命周期管理，须与目录桥接包的受管人员集合分开；一个主体只有一个生命周期写入方。员工凭据需拥有相应 profile 的 `run.submit` 和 `run.read` 权限。知识/能力引用属于业务配置，执行授权仍由 DiOS 决定。

## 从模板到任务

以下路径为部署示例；`PROFILE_DIGEST` 使用 `dios task profiles` 返回的真实指纹。API 需提供 `profile-precondition >=1,<2` 和 `e2ag-work >=1,<2`。

`template.json`：

```json
{
  "name": "技术助手", "role": "研发支持", "responsibilities": "提供带出处的分析",
  "owner": "团队负责人", "audience": "团队成员",
  "capability_refs": ["review"], "knowledge_refs": [],
  "bindings": {
    "review": {"profile_id": "review", "profile_digest": "PROFILE_DIGEST", "requires": {"e2ag-work": ">=1,<2"}}
  }
}
```

`employee.json` 的 identity 使用该员工凭据请求 `/api/os/runtime/identity` 返回的五个字段：

```json
{
  "identity": {"issuer": "example:issuer", "scope": "default", "id": "employee-001", "subject": "employee:001", "actor": "service:001"},
  "overrides": {"name": "团队技术助手"}
}
```

`config.json` 和 token 文件由当前用户拥有，权限设为 `0600`。`admin.command` 使用已部署 DiOS Python 环境的绝对路径，须能导入 `app.runtime.cli`，并指向对应实例的数据库及身份目录。管理员配置只授予运维人员。

```json
{
  "employees": {"employee-001": {"base_url": "http://127.0.0.1:8000", "token_file": "/srv/workforce/secrets/employee-001.token"}},
  "admin": {
    "command": ["/srv/dios/backend/.venv/bin/python", "-m", "app.runtime.cli", "--database-url", "sqlite+aiosqlite:////srv/dios/state/runtime.sqlite"],
    "catalog": "/srv/dios/secrets/principals.json"
  }
}
```

```bash
dios-workforce --state-dir /srv/workforce/state template --template assistant --template-version 1 --file template.json
dios-workforce --state-dir /srv/workforce/state draft --employee employee-001 --template assistant --template-version 1 --file employee.json --expected-version 0
dios-workforce --state-dir /srv/workforce/state publish --employee employee-001 --config config.json --expected-version 1
dios-workforce --state-dir /srv/workforce/state submit --employee employee-001 --config config.json --work-type review --sample first-item --goal '分析边界条件' --key request-001
dios-workforce --state-dir /srv/workforce/state show --employee employee-001
dios-workforce --state-dir /srv/workforce/state disable --employee employee-001 --config config.json --expected-version 2
```

提交返回 DiOS Run ID，可由 Console 或 SDK 继续观察。`--sample` 必须是对应执行配置已登记的输入；动态业务输入按后续 WorkItem 扩展接入。

## 工作项与人工确认

0.2.0 提供 `dios-workitem`，与员工注册表共用受保护的上层状态目录。复核人与员工采用不同的主体，`config.json` 增加：

```json
{
  "reviewers": {
    "reviewer-001": {"base_url": "http://127.0.0.1:8000", "token_file": "/srv/workforce/secrets/reviewer-001.token"}
  }
}
```

创建时通过该凭据确定复核人身份；员工绑定固定当前发布修订。以下示例假设员工仍为 active，工作项从未创建；实际调用使用上一条返回的 `version`。

```bash
dios-workitem --state-dir /srv/workforce/state create --id work-001 --title '边界分析' --work-type review --sample first-item --goal '检查输入边界' --reviewer reviewer-001 --config config.json
dios-workitem --state-dir /srv/workforce/state bind --id work-001 --employee employee-001 --expected-version 1
dios-workitem --state-dir /srv/workforce/state dispatch --id work-001 --config config.json --expected-version 2
dios-workitem --state-dir /srv/workforce/state show --id work-001
```

派发返回的 `run_id` 由 DiOS Worker 执行。管理员通过现有 `identity-update` 为复核主体配置该 Run 的 `run.read`、`artifact.read` 对象权限；WorkItem 不自动授予权限。`objects` 更新应保留该主体原有授权映射，且使用当前身份 revision。受保护变更文件中的对应条目为：

```json
{"objects": {"ACTUAL_RUN_ID": ["run.read", "artifact.read"]}}
```

配置授权后观察结果：

```bash
dios-workitem --state-dir /srv/workforce/state observe --id work-001 --reviewer reviewer-001 --config config.json --expected-version 3
```

运行尚未结束时仍为 `running`；成功且全部成果通过当前权限及 SHA-256/长度校验后，进入 `awaiting_confirmation`。输出包含 Run 与成果 ID、摘要和大小；复核人通过 Console 或 SDK 下载查看原成果后再明确决策：

```bash
dios-workitem --state-dir /srv/workforce/state confirm --id work-001 --reviewer reviewer-001 --config config.json --decision accept --note '已核对分析内容和来源' --expected-version 4
dios-workitem --state-dir /srv/workforce/state show --id work-001
```

`accept` 进入 `confirmed`，`reject` 进入 `needs_revision`。确认再次读取真实成果并核验权限；撤权或成果变化会拒绝确认。相同决策重放只返回原回执，冲突决策拒绝。Run 成功、人工退回与业务通过分别记录。

派发应答丢失时，使用原工作项和原版本重试 `dispatch`，接续固定的员工提交键；查询超时保持原状态。失败 Run 进入 `execution_failed`，完整历史保留。当前每个工作项对应一个 Run；返工、多岗位汇总、接管、补偿和审批待办沿 #40 后续切片推进。此 CLI 由受信任人员操作，复核归属使用真实 API 主体；企业 SSO 和多人入口另行联验。

## 发布与恢复规则

模板版本和员工修订不可修改；修改模板需新版本，修改员工产生新草稿。发布校验真实身份、可见执行配置、授权动作、E2AG 和所需接口版本。失败保留当前已发布版本。多个工作类型可分别绑定不同 profile。

每次提交固定发布修订、profile 指纹与幂等键。应答丢失后使用相同 `--key`、输入和目标重试；同键不同内容拒绝。底层执行配置变更返回 `PROFILE_VERSION_CONFLICT`，原请求保留为 pending，可通过 DiOS 历史对账；重新发布后，新业务请求使用新键。

停用先持久化 `disabling`，阻止新任务，再通过 DiOS `identity-update` 禁用专用主体、撤销旧授权。应答丢失可使用原 `--expected-version` 重试；完成后状态为 `disabled`。停用保留历史，不保证已经产生的外部副作用可撤销。重启应用会接续本地记录。重新启用需管理员显式恢复主体并再次发布；旧 Work/Grant 保持撤销。

CLI 进程持有独占锁，授权更新子进程继承同一锁。部署升级先结束当前命令，再切换包；保留状态目录。当前包不提供并发共享 Registry 对象的 Web 服务接口。

## 验证

安装 wheel 后，在已安装后端测试依赖的环境执行：

```bash
python scripts/qualify_workforce_registry.py --output workspace/workforce-qualification
```

测试使用真实 API、中间件、E2AG 存储/校验和管理员子进程；Worker 使用确定性测试适配器。覆盖模板/发布、身份隔离、结果关联、重启、应答丢失、停用撤权及版本拒绝。真实企业目录/SSO、物理 Runtime 替换和跨 Harness 在线切换沿原验收继续。

[员工分层与验收](../../docs/design/workforce-registry-37.md) · [WorkItem 状态与验收](../../docs/design/workitem-40.md)
