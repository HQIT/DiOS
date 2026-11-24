# 三层委派：OPA 策略与 Runtime 接入

2026-11-24 · #35 / K03-3

DiOS 的委派约束以受信授权记录为依据：根授权后最多三层，每级权限和期限收窄；工具调用必须同时满足当前工作授权、四层权限、审批和委派链。组织、岗位与客户场景由上层应用配置。

## 标准与实现选择

[RFC 8693 §4.1](https://www.rfc-editor.org/rfc/rfc8693.html#section-4.1)中的嵌套 `act` 表达历史参与者。资源端只使用顶层声明及当前 actor 做访问判断，历史 actor 用于追溯。DiOS 的上游权限依据来自独立授权记录，不能从 Token 中的历史 actor 列表推导。

[Keycloak 标准交换](https://www.keycloak.org/securing-apps/token-exchange)提供稳定的 Token Exchange；委派特性仍属 Preview，访问 Token 的交换也不建立自动撤销链。现有身份与 Token Exchange 接入继续使用，三层治理通过 Runtime 授权记录与成熟策略引擎组织。

[OPA 数据与策略模型](https://www.openpolicyagent.org/docs/philosophy)支持在原生 Rego 中检查上下文和受信数据。采用现有 OPA 适配器、Data API、Bundle 版本约束和工具 PEP；增加声明式规则，不增加策略解释器或授权数据库后端。[策略组件及数据契约](../../examples/policies/opa-delegation/README.md)

## 执行逻辑

```mermaid
flowchart LR
  Authority[受信授权记录] --> Snapshot[委派链与固定工作回执]
  Snapshot --> OPA[OPA 权限与期限交集]
  Work[Runtime 工作与执行绑定] --> OPA
  OPA --> Gate[现有工具审批与派发控制点]
  Gate --> MCP[MCP 工具调用]
  Gate --> Evidence[原有授权与回执证据]
```

原生策略从当前 `authorization_ref` 定位回执，核对 Run、scope、subject 和 actor，然后检查根授权到叶节点。每级 ID、parent、depth、scope、subject、audience、状态和授权代次必须一致；完整权限元组逐级包含，期限逐级缩短且当前有效。循环、缺链、第四级、错 actor、越权字段组合和未知格式均拒绝。

工作回执固定准入时的各级授权代次。上游撤销并递增代次后，即使重新启用，旧回执仍不匹配。无关链路更新不参与本工作的决策。工具策略以逻辑交集组合，登记独立的 `dios/delegated_tools/allow` 入口；不会通过另一个宽泛 allow 分支绕过委派检查。

## 已有验证

[原生验证证据](evidence/delegation-policy-20261124.json)通过真实 OPA、SQLite Runtime 和 MCP HTTP 端点运行。根授权及一至三层链路均能经审批完成工具调用与回执；各级撤销、权限扩大、动作/资源交叉拼接、期限扩大、过期、错主体/范围/受众、错工作绑定、循环、缺链和旧授权代次均被拒绝。MCP 初始化期间撤销上游后，最终派发复核阻止工具调用。继承的策略换版、服务故障、审批人撤权与重复派发检查同时执行。

## 生命周期接入与剩余工作

策略组件已通过现有 Runtime 实际控制点验证，默认开发策略保持原配置。下一步接入受信委派的创建、受限派生、撤销及固定工作回执发布，把同一检查覆盖到工作准入、执行 Grant 续期、工具与结果接收。发布端需验证每次委派的发起者权限，绑定固定父级和代次，并限制管理凭据。

OPA 当前数据判定与 Runtime 提交分属两个服务。单调版本、数据新鲜度、发布中断恢复及跨服务严格撤销排序继续按[成熟方案矩阵](e2ag-mature-solutions-20261124.md)验收。此次测试未证明企业身份源级联撤销、物理多主机保证、生产发布或完整 #35 完成；Issue 保持开放。
