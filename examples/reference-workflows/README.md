# 三类参考路径输入

此目录提供 `reference-workflows` 的管理员登记输入与知识 Provider 合成资料。`mapping.json` 固定原始 `reference-scenarios.json` 的摘要和逐项预期；场景内容沿用原计划。

| 目录文件 | 用途 |
|---|---|
| `sales-inputs.json` | 规格与约束，输出建议和排除原因 |
| `team-inputs.json` | 三岗位来源，输出汇总和待指定负责人 |
| `education-inputs.json` | 学生导读，只绑定讲义来源版本 |
| `catalog.json` | Provider 的原始合成资料，包含教师受限答案 |
| `mapping.json` | 输入摘要、来源版本与原验收预期 |

每个场景使用独立 Profile、受控工具和 Provider 凭据。教育凭据只授予 `lecture-1`，Provider 在检索前过滤 `answer-1`。运行时凭据和授权配置由管理员保存在私有目录。

原始输入/结果约束与业务转换属于独立应用；这里的 `workspace/context_id` 是业务引用。DiOS 的 scope、主体和 Work 授权另行确定，不从业务输入推导权限。

部署与验证见 [参考路径接入契约](../../docs/design/reference-workflow-contracts-29.md)。
