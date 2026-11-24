# OPA 三层委派工具策略

本目录是 #35 的可执行策略组件。`delegation.rego` 检查受信委派记录，`tools.rego` 将它与既有 `data.dios.tools.allow` 取交集。委派工具的登记地址为 `/v1/data/dios/delegated_tools/allow`。部署 Bundle 时包含既有工具规则、本目录两份规则，并将该入口加入 OPA 查询白名单。

数据位于 `data.dios_delegation`，由可信授权记录发布端维护。模型参数和调用请求均不提供这些数据：

| 字段 | 约束 |
| --- | --- |
| `work_bindings[authorization_ref]` | 在工作准入时固定的授权回执，包含 `run_id`、`scope`、`subject_ref`、`actor_ref`、`chain`、`epochs` |
| `chain` | 根授权 ID 后跟 0～3 个委派 ID；ID 唯一，顺序固定 |
| `epochs` | 每个链路在准入时捕获的授权代次；回执不得因上游变化而覆盖 |
| `links[id]` | `id`、`parent_ref`、`subject_ref`、`actor_ref`、`audience`、`permissions`、`expires_at`、`depth`、`authorization_epoch`，以及 `scope`、`state` |
| `permissions` | 完整五字段权限元组；每级集合必须是父级子集 |
| `expires_at` | RFC 3339；子级不晚于父级；每级当前有效 |
| `state` / `authorization_epoch` | 仅 active 允许；撤销或改变授权需要递增代次；重新启用不恢复旧回执 |

`input.context.binding` 与 `input.context.permission` 由现有 Runtime 生成。回执必须匹配当前授权 ID、Run、scope、subject 和 actor；叶节点 actor 必须是当前执行 actor。当前限定 audience 为 `dios-runtime`，跨服务 audience 转换保留在后续 Provider 接入。

Bundle 规则版本与授权数据版本分别管理。授权数据不能由普通查询凭据写入；OPA 数据更新需要可信发布身份和受限管理路径。生产发布端还需保证单调代次、不可变回执、完整快照、新鲜度和恢复，本轮未启用开发服务的委派策略。现有默认 OPA 管理接口继续拒绝写入。

真实 OPA/Runtime/MCP 验证：

```sh
cd backend
DIOS_TEST_OPA=1 .venv/bin/python -m unittest discover -s tests -p 'test_runtime_delegation_opa.py' -v
```

测试使用固定摘要的官方 OPA 容器和独立临时数据库，经原有工具请求、独立审批、派发及回执路径执行。测试数据使用 OPA 原生 Data API 发布；该测试写入方式不构成生产发布接口。
