# Reference Workflows

独立参考应用，使用合成数据生成选型建议、岗位计划汇总和学生导读。业务规则属于本应用；DiOS 提供固定版本、执行授权、工具审批、任务与产物。

发行物为 `reference-workflows==0.1.1` wheel/sdist 和独立镜像。依赖公开 `dios-sdk>=0.1.20,<0.2` 及独立 `knowledge-evidence==0.1.0` 的引用校验器，运行环境无需 DiOS 后端或数据库客户端。

```sh
python -m build applications/reference-workflows
python -m pip install --no-index --find-links ./wheels reference-workflows
```

管理员使用应用镜像摘要登记 `external.reference-workflow/v1`。三个输入目录位于 `examples/reference-workflows/`；原始场景与验收预期来自 `docs/design/fixtures/reference-scenarios.json`。输入包含场景、业务上下文、约束及来源版本引用，使用 canonical SHA-256 冻结。

入口 `dios-reference-workflow` 从 `DIOS_WORK_INPUT` 读取任务，通过 SDK 的 `ToolChannelClient` 调用 `knowledge_search`。调用经 E2AG 审批，完成后验证实际工具调用证据，再校验来源与业务规则。知识凭据由网关持有；Worker 使用 `network=none`、只读文件系统和空 `worker_secret_refs`。

输出包含 `scenario/context_id/input_digest/outcome/citations/evidence/markdown/review_status`。`pending_review` 交给上层业务确认；技术成功保存在 DiOS Run，业务完成由上层管理。

模型执行入口：`dios-reference-harness --harness cursor|dsh --config <管理员配置>`，安装时选择 `reference-workflows[acp]`。两种实现复用 SDK 的受控 ACP 适配、模型发起的 MCP 检索、审批、会话和结果契约。模型需同时返回原始来源与业务结论；应用核验来源摘要并检查结论，错误结果返回 `EVIDENCE_INSUFFICIENT`，原输出由 DiOS 保留。应用不会以确定性计算替换模型的错误结论。

Cursor 与 DSH 的三路径实测见[跨实现参考资格](../../docs/design/reference-harness-qualification-29.md)。工作项接管、人工复核决定和跨实现业务交接由独立平台继续承接。

输入中的 `context_id` 和 `source_refs` 用于关联和来源校验；实际资料访问范围由 Provider 的服务端凭据授权确定。学生入口只允许讲义版本；资料不全、来源变更、受限答案或内容不符时无法产生成功报告。

扩展场景在独立应用中增加规则、输入/结果 Schema 和参考样例，发布新应用版本与 Revision；DiOS 继续处理相同的能力契约。详细映射与运行证据见 [参考路径接入](../../docs/design/reference-workflow-contracts-29.md)。
