# A2A 接口兼容勘察

日期：2026-11-11 · 任务：F01-3 #57 · 结论：现有网关作为 legacy 适配路径保留，标准兼容须经过明确版本的契约整改和运行验收。

本轮使用 [A2A 0.3.0 固定规范](https://a2a-protocol.org/v0.3.0/specification/)检查现有 `message/send`、`tasks/get`、`tasks/cancel` 形态。该版本用于评估当前接口的兼容缺口；目标支持版本由 F01-1 固定。最新规范已改变部分对象/方法表示，不将不同版本的要求混用。

## 检查结果

11 项有界检查中，3 项满足预期，8 项存在缺口。满足项为任务查询身份、未知任务错误和未知方法错误。逐项输入、预期、实际结果及源文件摘要见 [JSON 证据](evidence/a2a-contract-survey-20261111.json)。

| 项目 | 当前行为 | 接入影响与责任 |
| --- | --- | --- |
| Agent Card 版本 | 缺少 protocolVersion | 无法据 Card 固定协议；F01-1 定义兼容版本，K01-4 实现协商。 |
| Provider 结构 | provider 存在但缺少 url | 严格客户端结构校验可能失败；K01-4 固定输出 Schema。 |
| Task 结构 | 查询结果缺少 0.3.0 要求的 kind | 按选定版本生成响应；新版本不沿用旧 discriminator 要求。 |
| JSON-RPC 版本 | jsonrpc=1.0 仍进入任务查询并成功 | 请求层缺少版本校验；K01-4 校验请求包。 |
| 非法参数 | tasks/get 的数组参数返回内部错误 -32603 | 参数错误应在方法边界明确拒绝；K01-4 统一错误映射。 |
| 终态取消 | completed 任务仍返回成功结果 | 需要不可取消错误语义；执行中取消仍需真实进程/会话确认。 |
| 消息上下文 | message.contextId 未映射到传给 service 的 context_id | 跨轮关联可能丢失；F01-1 固定映射，K01-4 验证透传。 |
| 流式声明 | 配置 streaming=true 后 Card 宣称支持，但 message/stream 返回方法不存在 | Card 只应开放实际已实现能力；K01-4 绑定声明与实现。 |

这些差异均来自当前源码行为；精确字段与错误要求对应固定版本规范的 AgentCard、Task、Message、JSON-RPC 和错误章节。当前 service 路径最终转发至 DiAgent 的 OpenAI 兼容接口，DiAgent 的原生 A2A 端点尚无实现证据。

## 探测方法与覆盖

[探测脚本](../../scripts/probe_a2a_contract.py) 提取当前源码的指定函数体，在内存 Agent/Task 和依赖替身上执行。方法调用没有连接数据库、网络、Docker 或模型；消息发送仅记录传给依赖的参数，终态取消不进入写入路径。源码 SHA-256 随结果保存。

复核命令，在仓库根目录执行：

```bash
python scripts/probe_a2a_contract.py --output /tmp/dios-a2a-contract-survey.json
```

脚本完成表示采集成功，JSON 的 met/gaps 才表示符合情况。此证据覆盖函数边界，HTTP 中间件、认证、并发竞争、流式交付、真实取消和故障恢复仍需集成验证。

## 后续交付

F01-1 固定标准版本、legacy 迁移及 Run/Attempt/Session 映射；K01-4 实现版本化请求/响应与能力协商；K01-2 提供可靠取消、恢复和所有权；K01-5 汇合真实 Agent 验收。

F01-3 下一轮使用隔离合成仓库验证 Cursor 的权限拒绝、取消、断连恢复与输出完整性。dsh 作为第二个原生 ACP 对照，复用相同平台样例。完整结果与历史事件由 DiOS 保存，生产评审队列保持原所有权。
