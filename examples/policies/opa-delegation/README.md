# OPA 三层委派工具策略

本目录是 #35 的可执行策略组件。`delegation.rego` 检查受信委派记录，`tools.rego` 将它与既有 `data.dios.tools.allow` 取交集。委派工具的登记地址为 `/v1/data/dios/delegated_tools/allow`。部署 Bundle 时包含既有工具规则、本目录两份规则，并将该入口加入 OPA 查询白名单。

Runtime 生命周期接入使用 `/v1/data/dios/delegation/allow`，与既有工具控制点共同约束执行。授权数据由 Runtime 从当前事务生成 `input.context.delegation`；模型参数和调用请求不能提供这些数据。隔离策略测试保留 `data.dios_delegation` 回退数据源；存在 input 快照时只使用该快照：

| 字段 | 约束 |
| --- | --- |
| `work_bindings[authorization_ref]` | 在工作准入时固定的授权回执，包含 `run_id`、`scope`、`subject_ref`、`actor_ref`、`chain`、`epochs` |
| `chain` | 根授权 ID 后跟 0～3 个委派 ID；ID 唯一，顺序固定 |
| `epochs` | 每个链路在准入时捕获的授权代次；回执不得因上游变化而覆盖 |
| `links[id]` | `id`、`parent_ref`、`subject_ref`、`actor_ref`、`audience`、`permissions`、`expires_at`、`depth`、`authorization_epoch`，以及 `scope`、`state` |
| `permissions` | 完整五字段权限元组；每级集合必须是父级子集 |
| `expires_at` | RFC 3339；子级不晚于父级；每级当前有效 |
| `state` / `authorization_epoch` | 仅 active 允许；撤销递增代次；生命周期管理需用新 key 重新签发 |

`input.context.binding` 与 `input.context.permission` 由现有 Runtime 生成。回执必须匹配当前授权 ID、Run、scope、subject 和 actor；叶节点 actor 必须是当前执行 actor。当前限定 audience 为 `dios-runtime`，跨服务 audience 转换保留在后续 Provider 接入。

Bundle 规则版本与授权数据版本分别管理。Runtime 在事务中维护授权记录与固定回执，调用 OPA 后复核数据库时间；撤销与依赖 Work/Grant 失效同事务提交。开发服务已部署并完成真实任务验证，既有 profile 按需配置 `delegation_ref`。OPA 管理接口拒绝普通查询方写入。创建、派生、撤销、迁移及信任边界见[委派生命周期](../../../docs/design/delegation-lifecycle-35.md)。

真实 OPA/Runtime/MCP 验证：

```sh
cd backend
DIOS_TEST_OPA=1 .venv/bin/python -m unittest discover -s tests -p 'test_runtime_delegation_opa.py' -v
DIOS_TEST_OPA=1 .venv/bin/python -m unittest discover -s tests -p 'test_runtime_delegation_lifecycle.py' -v
```

测试使用固定摘要的官方 OPA 容器和独立临时数据库，经原有工具请求、独立审批、派发及回执路径执行。策略组件测试使用 OPA 原生 Data API；生命周期测试使用当前 Runtime 事务 input，并覆盖持久回执、祖先撤销、PDP 等待期间期限变化和备份恢复。
