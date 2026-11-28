# 数字员工档案与发布

同一数字员工可按工作类型绑定不同执行配置，保留稳定身份、职责、版本和任务历史。模板共享、实例覆盖与发布记录存放在独立 SQLite 中；DiOS 提供任务、成果和 E2AG 执行治理。

这是客户无关数字分身平台的独立 Linux/WSL 管理包。当前入口为受信任运维 CLI；多人 Web 管理和业务角色审批由后续平台入口承接。

## 安装

在仓库根目录、独立 Python 3.12 环境中执行：

```bash
pip wheel --no-deps sdk/python applications/workforce-registry -w workspace/workforce-dist
pip install workspace/workforce-dist/*.whl
dios-workforce --help
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

提交返回 DiOS Run ID，可由 Console 或 SDK 继续观察。`--sample` 必须是对应执行配置已登记的输入；动态 WorkItem 输入由 #40 衔接。

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

[分层设计与验收](../../docs/design/workforce-registry-37.md)
