# E2AG 补强：成熟方案、职责与选型准入

2026-11-24 · 关联 ADR 0007/0008、#27/#30/#33/#34/#35/#38/#56/#58

## 工程原则

遇到能力缺口，先调研公开成熟实现和标准，优先直接采用或薄层适配。候选方案按同一需求、保证、故障场景、许可、部署成本与维护责任比较。确有缺口时，先讨论自研范围、依据与替代方案，再实现。E2AG 保留治理语义，具体策略引擎、授权状态与执行机制分别选型。

## 可复用方案

| 需求 | 公开方案与实际依据 | DiOS 接入判断 |
| --- | --- | --- |
| PDP/PEP 解耦 | [AuthZEN Authorization API 1.0 Final](https://openid.net/specs/authorization-api-1_0-final.html) 定义 subject/action/resource/context 和决策接口 | 新 Provider 接口优先映射标准；执行身份、租约、审批和派发事务仍属于 Runtime |
| 权限变化与一致性 | [SpiceDB consistency](https://authzed.com/docs/spicedb/concepts/consistency) 提供 ZedToken、最低新鲜度、精确快照与 fully_consistent；后者读取请求开始时快照 | 重点验证撤销后的新鲜度与版本传递。一次远程 Check 后再本地提交仍存在时间窗口，需要明确执行点协调协议 |
| 对象关系与模型升级 | [OpenFGA immutable models](https://openfga.dev/docs/getting-started/immutable-models) 支持固定模型 ID；[consistency](https://openfga.dev/docs/interacting/consistency) 的 HIGHER_CONSISTENCY 绕过缓存访问数据库 | 用于资源分享和关系授权的候选。模型版本与关系数据版本分开，不能用固定模型 ID 代替当前权限检查 |
| 工具及参数策略 | [OPA Agent Tool Calling](https://www.openpolicyagent.org/docs/agent-tool-calling) 明确 Harness 作为 PEP、OPA 作为 PDP，在执行工具前检查 | 优先评估已有 OPA 接入。可信主体、资源版本与执行上下文由宿主提供；模型只提出动作请求 |
| 策略分发 | [OPA external data](https://www.openpolicyagent.org/docs/external-data) 将 OPA 定位为数据/策略副本；[bundles](https://www.openpolicyagent.org/docs/management-bundles) 按最终一致方式更新 | Bundle 适合规则发布；严格撤销必须补齐新鲜度水位、更新确认和失败行为 |
| 意图与事件持久化 | [AWS transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html) 在同一事务记录业务变化与待发送事件，消费者需幂等 | 复用成熟 Outbox/Inbox 模式组织派发恢复；远端业务效果依赖端点幂等和可核验回执 |
| 权威数据分工 | [OpenFGA source of truth](https://openfga.dev/docs/best-practices/source-of-truth) 区分身份源、应用结构与细粒度授权 | 企业/项目/岗位继续属于上层应用；DiOS 接收通用主体、资源与权限引用 |

[SpiceDB 官方双写说明](https://authzed.com/blog/writing-relationships-to-spicedb)还讨论了应用事务、权限写入与一致性令牌的协调，以及应用提交失败后的残留关系。接入设计需逐项判断残留权限是否安全，补偿删除本身也可能失败；不把跨库顺序写入当作原子提交。

上述产品覆盖不同职责。候选组合按需要选择，避免为了完整性同时引入全部服务。AuthZEN 审批与 MCP 映射扩展在[工作组目录](https://openid.net/wg/authzen/specifications/)中仍列为草案，实施依据优先使用已定稿的核心接口。

## 昨日讨论对应的验收条件

| 缺口 | 必须满足的性质 | 实施/验证位置 |
| --- | --- | --- |
| 审批后配置变化、检查与签发竞态 | 等待/恢复后的有效授权与签发、派发具有确定的先后顺序；检查失败不产生新调用 | PDP + 权威状态 + Runtime 提交点；候选方案统一交错测试 |
| 撤销过度传播 | 无关主体或配置变化不终止合法任务；已撤销旧 Grant 不因恢复配置自动复活 | 按实际授权依据判断失效；事件因果边不自动当成授权依赖 |
| 局部检查拼接 | Run/Attempt/Session/Grant/Tool/参数与授权主体指向同一动作 | Runtime 绑定与审批消费；跨任务、错会话、篡改参数负例 |
| 不可信输入成为权限 | 模型输出及事件载荷只作业务输入；权限来自可信身份与策略 Provider | API/事件入口与 Harness PEP；伪造主体和授权字段负例 |
| 本地允许与远端效果混淆 | 分别记录派发提交、远端接受、效果回执与未知结果 | 端点幂等键、操作意图、回执关联；HTTP 200/相同 SHA 不直接证明新建效果 |
| 撤销与在途调用 | 本地撤销阻止后续准入；已派发调用保留事实；远端停止需要端点确认 | 调用状态机与各 Adapter 能力资格；不宣称跨系统瞬时撤销 |
| 完整中介 | MCP、原生工具、进程和网络出口均有实际控制证据 | #56/#58 各 Harness 资格；未覆盖出口登记为受限模式 |
| 形式化前提过强 | 明确保护域、权威写入路径、版本、提交点、故障和可信端点假设 | 工程形式模型及并发反例；有界状态探索与真实执行测试分别报告 |

## 当前原型与下一步

工作区的 authority 事务存储、主体隔离和工具准入修改保持为未提交的验证原型。新增 authority 模块只在显式激活的测试 scope 使用；开发服务尚未迁移或启用该授权后端。原型可检验同库线性化与不可复活语义，生产选型仍按 ADR 0008 评审。

原型范围包括本地 Runtime 策略、主体、工具登记和审批者。旧 Event dispatcher、机器身份目录、跨 Provider 事务、远端效果接受与全面出口治理尚未纳入同一保证。现有运行记录与服务保持原状。

下一步先用既有故障与形式化反例评估 OPA、SpiceDB/OpenFGA 的接入组合，产出“直接采用／适配／仍缺能力”矩阵。固定现有业务无关权限元组和执行语义，测量正常、等待后撤销、无关更新、重复派发、PDP 不可用与未知效果六组场景。完成许可、依赖和运维审查后，再确定默认实现及必要的自研范围。当前原型测试通过仅作为工程证据，不直接触发选型或部署。

验证原型的 [测试记录](evidence/e2ag-authority-prototype-20261124.json)：后端 450 项中 330 通过、120 跳过，设计契约 681 项与授权契约 263 项通过。有界模型探索 117649 条长度为 6 的轨迹；其范围为单个本地权威状态和调用。跨 Provider、远端效果及 PostgreSQL 并发资格仍需独立验证。

## 成熟 Provider 实测（2026-11-24）

[可重跑探针](../../scripts/probe_authorization_providers.py) 使用官方 OPA 1.17.0、OpenFGA 1.23.0、SpiceDB 1.57.1 容器，逐一运行独立内存存储。每个服务上限 1 CPU/512 MiB，HTTP 只绑定本机随机端口；容器结束后删除。镜像摘要与逐项结果见 [51 项实测记录](evidence/authorization-providers-20261124.json)。本轮验证 API 语义，未进行性能排名或持久恢复测量。

各 Provider 均通过 17 项检查：完整元组允许；五个元组字段各自变化拒绝；未知主体拒绝；四层分别缺失拒绝；无关主体撤权保留合法请求；关联主体撤权拒绝；等待后重新查询拒绝；恢复后的新授权允许；重复查询；服务关闭后返回传输失败。OpenFGA 固定模型 ID 并使用 HIGHER_CONSISTENCY，测试配置启用查询缓存；SpiceDB 在后续检查中携带最近写入的 ZedToken。OPA 的策略和数据通过独立测试实例更新。

已返回的 allow 值在调用方内存中仍存在，各 Provider 的新查询均拒绝已撤销请求。该结果支持“等待后重新判定”；实际派发提交仍需与撤销明确排序。恢复关系后允许新请求是正常行为，旧 Grant 是否继续失效由 Runtime 的任务授权生命周期负责。

| 职责 | 直接采用或适配 | 尚需完成 |
| --- | --- | --- |
| 完整权限元组、四层交集和工具参数判定 | OPA 原生策略/API 已实测可用；优先接入既有 PolicyEvaluator 边界 | 真实 DiOS PEP 调用、策略版本绑定、不可用时阻止派发 |
| 关系授权与对象分享 | OpenFGA/SpiceDB 原生 API 均通过本轮检查 | 持久后端、身份/资源映射、恢复和实际规模评测后确定 Provider |
| 标准互操作 | AuthZEN 1.0 Final 作为 PDP/PEP 接口参照 | OpenFGA AuthZEN 接口目前标为 [Experimental](https://openfga.dev/docs/interacting/authzen)，原生 API 的测试结果不计作标准符合性验证 |
| 审批消费、Run/Attempt/Session 绑定 | 复用现有 Runtime 状态机及成熟事务/Outbox 模式 | 对接外部授权后验证撤销与派发提交窗口；覆盖旧事件执行链 |
| 外部效果恢复 | 复用现有意图、幂等和未知结果处理 | 端点接受/回执契约与真实 Harness 验证；PDP 查询不代替外部效果证明 |

本轮核对的 [OPA](https://raw.githubusercontent.com/open-policy-agent/opa/v1.17.0/LICENSE)、[OpenFGA](https://raw.githubusercontent.com/openfga/openfga/v1.23.0/LICENSE)、[SpiceDB](https://raw.githubusercontent.com/authzed/spicedb/v1.57.1/LICENSE) 所选版本均附 Apache-2.0 LICENSE。发布时按实际分发物核对 NOTICE、依赖及商标信息。当前只使用官方镜像完成临时资格验证。

下一实施项：在既有 PolicyEvaluator 职责内接入 OPA，并用真实工具派发验证拒绝、超时和当前策略绑定。关系授权 Provider 保持独立可选；本轮结果不支持把自定义 authority 状态库直接定为默认后端。新增授权语义或自研协调机制须先说明成熟机制的具体缺口并讨论。
