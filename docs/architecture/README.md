# DiOS 架构基线

版本：0.3 · 日期：2026-11-11 · 状态：已接受（G0）

文档组标识：ARCH-BASELINE-0.3；接受授权与技术评审见评审记录；关联任务：[F01-0 #55](https://github.com/HQIT/DiOS/issues/55)。适用范围：目标架构与工程规则；接受版本在评审记录中固定提交 SHA。v0.3 纳入 #57 的能力证据、持久执行对象、E2AG 集成与持续扩展；工程手册同步，设计模板沿用通用模板。

DiOS 为数字员工提供受控运行底座，上层管理组织与工作，开放应用承载业务成果。三层方向已由 [ADR 0003](../adr/0003-layered-digital-workforce-platform.md) 确认；本基线固定工程原则与约束，G0 通过后进入详细设计。

## 阅读路径

| 读者要解决的问题 | 文档 |
| --- | --- |
| 系统是什么，各层为什么这样划分 | [架构逻辑与图解](system-design.md) |
| 如何接入不同来源的 Agent 与 Harness | [开放智能体接入](agent-interoperability.md) |
| 各 Agent 实际提供什么接口，首轮验证谁 | [智能体接入能力矩阵](../design/agent-integration-capability-matrix.md) |
| E2AG 如何接入外部 Agent，哪些能力需要扩展 | [E2AG 接入与扩展](../design/e2ag-agent-integration.md) |
| 持续代码评审如何进入三层架构 | [评审业务能力评估与接入设计](../design/continuous-code-review-assessment.md) |
| 领域对象、API、状态和兼容怎样固定 | [领域/API 契约 v1](../design/domain-api-contract-v1.md) |
| 两档容量、SLO、部署与基准如何验收 | [容量/SLO 设计 v1](../design/capacity-slo-v1.md) |
| 哪些约束必须长期保持 | [架构原则](principles.md) |
| 需求如何落实到设计、任务和验收 | [需求与验收追踪](traceability.md) |
| 谁评审、如何变更、何时允许开发与发布 | [工程手册](../engineering/handbook.md) |
| 哪些设计已经达成共识，还有什么待定 | [架构评审记录](review-v0.3.md) |
| 当前代码组织和运行方式 | [现有架构与实现](../architecture.md) |
| 工作顺序和进度 | [Roadmap](../../ROADMAP.md) · [GitHub Project](https://github.com/orgs/HQIT/projects/2) |
| 开放接入影响了哪些计划，下一步做什么 | [整体计划调整与下一批工作](../planning/open-agent-plan.md) |

图解中的组件、关系与部署均描述目标设计；当前实现范围单独记录在现有架构与 Roadmap。正式契约在后续领域/API、授权和容量设计中形成。

## 基线构成

架构原则定义约束，图解解释协作逻辑，ADR 保存重要选择及理由，详细设计固定接口和状态机，测试与运行证据验证约束。工程手册将这些材料与 Issue、PR 和版本连接起来。

Markdown 是文档正文来源，`diagrams/*.svg` 是可编辑矢量图；`scripts/build_architecture_diagrams.py` 生成图，`scripts/render_architecture_guide.mjs` 将正文与图合成为可离线阅读的 HTML。导出文件用于阅读，变更以仓库源文件为准。

评审入口为 [review-v0.3.md](review-v0.3.md)。原则、责任边界、授权逻辑和工程门槛已接受，F01-1 领域/API 与 F01-2 容量设计已形成 v1 交付，见 [联合记录](../design/review-f25-f26.md)；后续授权和实现任务按各自门槛推进。

## 图文导出

运行 `python scripts/build_architecture_diagrams.py` 重建六张 SVG；持续评审图 6 保留独立 SVG 源文件，合计七张图。HTML 渲染器使用 Node.js 与 `marked`；可传入独立工具目录，保持产品依赖稳定：

```bash
node scripts/render_architecture_guide.mjs --modules-dir /path/to/node_modules --output /path/to/DiOS-guide-v0.3.html
```

HTML 内嵌图与样式，正文可离线阅读；仓库补充资料链接依赖本地源码路径。交付前检查本地链接、图文语义、实际渲染和导出版本。
