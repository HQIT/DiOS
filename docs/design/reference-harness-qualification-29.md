# 跨实现参考路径资格

三条原始参考路径已分别通过 Cursor ACP 和 DeepSeek Harness 的真实模型执行，共六条合格路径。独立应用 `reference-workflows==0.1.1` 使用 SDK 0.1.20，同一输入、来源版本、结果规则和工具授权条件在两种实现上保持一致。业务规则与校验代码位于应用包，DiOS 后端及 SDK 无新增业务分支。

| 原始路径 | Cursor | DSH | 结果要求 |
| --- | --- | --- | --- |
| 选型建议 | 通过 | 通过 | 推荐 spec-A，排除续航不足的 spec-B，保留两份引用 |
| 岗位计划汇总 | 通过 | 通过 | 三份计划完整保留，role-B 缺负责人，不能虚构补全 |
| 学生导读 | 通过 | 通过 | 仅引用 lecture-1，成果不含 answer-1 或受限答案内容 |

每条路径固定应用 Revision、输入摘要、镜像及权限版本，经过真实 MCP 检索审批，保留 Run、Attempt、模型 Session、工具调用与退出收据。知识 Provider 用服务端凭据过滤资料；模型只收到获授权的来源。成果保持 `pending_review`，工具批准不代表业务验收。

## 模型与结果校验

应用使用已有 `cursor_execute` / `dsh_execute` 适配器，经 ACP v1 创建会话和提交 prompt。模型主动调用 `knowledge_search`，SDK 将调用映射到 Worker 工具通道；执行和接收结果沿用 E2AG。模型需返回来源原文、摘要和自身生成的业务结果。

来源摘要、版本、范围或业务结果不符时，应用返回 `EVIDENCE_INSUFFICIENT`。真实执行中，两条结果曾将外层资料对象或序列化文本误作业务内容；两次均失败，原输出保留，没有成功成果帧。提示词已明确内层 JSON 的字段，校验条件保持不变。另有两次隔离夹具漏配网关凭据解析器，被 `SECRET_ACCESS_DENIED` 拒绝；凭据随后仅配置在可信网关，未加入 Agent 的挂载范围。

## 版本与证据

[资格报告](evidence/reference-harnesses-20261127.json)列出六条合格路径、四条拒绝记录和一条较早通过的受影响路径，共十一条真实 Harness 会话。供应商内部模型请求数不以会话数代替。安装后的独立应用包通过十三项相关测试。

原始结果逐条记录实际镜像，SDK 与共享校验器字节摘要一致。场景提示词变动只重跑受影响路径；未变路径通过运行镜像内的场景代码摘要与当前源码比对复用。该摘要保留整个应用入口、解析与校验代码，仅排除本次不会执行的其他场景提示词常量。

可直接核验已有证据，无需再次调用模型：

```bash
python3 scripts/verify_reference_harness.py \
  docs/design/evidence/reference-harnesses-20261127.json
```

新增版本的隔离验证入口为 `scripts/qualify_reference_harness.py`，显式传入固定镜像 Profile、凭据清单引用、OPA 配置、场景及输出目录。凭据内容不写入资格报告。已有运行实例及生产评审不参与注入。

## 本期资格结论

F03-2.03 已按[本期范围](../planning/closure-scope-20261128.md)结项。六条固定版本参考路径的证据和应用源码摘要仍可核验；G05 接管、取消和迟到成果复核已补齐。G06 运行中跨实现交接为后续候选，五项评审应用专属验收由 #59/#60 承接。

[汇总证据](evidence/reference-closure-20261128.json)保留各报告原版本和条件；历史报告中的 remaining 表示当时状态，当前范围与结论以本节和范围文件为准。运行 `python scripts/verify_reference_closure.py` 可复核，无需再次调用模型。

接口依据：[ACP v1 Prompt Turn](https://agentclientprotocol.com/protocol/v1/prompt-turn)、[MCP Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)。应用沿用这两个协议及既有 SDK，没有增加私有 Agent 协议。
