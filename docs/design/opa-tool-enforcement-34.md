# OPA 工具策略执行

2026-11-24 · #34 · 成熟 PolicyEvaluator 适配

DiOS 在已有工具授权与审批链中调用 OPA Data API。四层权限、任务/尝试/会话绑定、审批单次消费、调用意图和回执继续由现有 Runtime 执行；OPA 可进一步限制工具参数。策略服务拒绝、不可达、超时、响应缺失或版本不符时，不提交新的派发。

## 接入配置

可信工具登记可增加 policy_evaluator。该配置纳入工具实现摘要，重新计算 permission.resource_version，并通过既有工作策略授予完整新权限元组。API、CLI 本地 Worker 和远程工具网关都使用同一个 ToolInvocations 控制点，未配置的工具沿用既有内置策略。

```json
{
  "policy_evaluator": {
    "kind": "opa",
    "url": "http://127.0.0.1:13186/v1/data/dios/tools/allow",
    "bundles": {"/policy": "dios-tools-v1"},
    "timeout_seconds": 2
  }
}
```

可选 bearer_token_file 是宿主机绝对路径，只在发送授权请求时读取，内容不进入输入、审计或模型。远端使用 HTTPS；明文 HTTP 仅允许回环地址。客户端关闭代理环境继承与重定向，限制输入/响应为 64 KiB，整个判定请求默认 2 秒、可配置 0.05—5 秒。OPA 返回 result 必须为布尔值；undefined、字符串 true 和错误响应均拒绝。

依据 [OPA 官方 Data API / provenance](https://www.openpolicyagent.org/docs/rest-api#provenance)，客户端请求 provenance=true，并核对完整 Bundle 名称/修订集合。服务新增、缺失或换版均拒绝当前登记。审计只保存决策、输入摘要、Bundle 修订和可选 decision_id，不保存原始响应或服务凭据。

Bundle 修订是可信发布端声明的版本标识，发布流程必须保证同一修订对应固定内容。远程 Bundle 可采用 OPA 自带签名验证，见 [官方 Bundle 验证](https://www.openpolicyagent.org/docs/management-bundles#signature-verification)。当前开发示例采用只读本地挂载，并用 OPA 内置 API 授权限制为指定查询和 health；未做远端签名发布资格。

## 执行与证据

请求创建、人工审批、预检及最终派发均重新执行策略。最终判定后，重新检查租约、Grant、会话和调用有效期，防止等待策略服务时过期仍继续执行。策略证据和派发意图在既有事务中保存；写入失败会回滚，审批不消费，工具不调用。成功返回的工具响应仍按既有回执链校验和归档，查询与重试不重复派发。

[示例策略](../../examples/policies/opa-tools/tools.rego) 使用完整权限元组交集和搜索参数长度限制，业务系统可在自己的 Bundle 中增加规则。企业、项目、岗位和客户业务语义保持在上层应用。

## 已知保证与后续验收

本适配保证在 OPA 判定时使用它返回的 Bundle 版本，并在本地提交前复核 Runtime 执行状态。OPA 判定返回至本地派发提交之间的跨服务撤销排序仍需专门协调；当前文件策略重读也不提供原子更新保证。这两项继续属于 #34 的严格撤销验收，不能凭 HTTP 查询或版本字段宣称完成。

此前自定义 authority 状态库及相关测试已从运行源码移出，完整副本保存在本地 workspace/authority-prototype-20261124，生产默认实现未采用。旧 Event dispatcher、机器身份目录和远端效果接受端仍需分别接入既定授权语义。

## 复现

```bash
cd backend
DIOS_TEST_OPA=1 .venv/bin/python -m unittest discover -s tests -p 'test_runtime_opa_integration.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_runtime_policy_evaluator.py' -v
```

真实资格使用固定摘要的官方 OPA 1.17.0 容器、SQLite Runtime 和独立 MCP HTTP 测试端点。包括成功审批/执行/回执/重复派发、参数拒绝、审批后换版、MCP 初始化期间换版和 Provider 停止；超时场景注入有界延迟。单元测试补充重定向、错误/过大响应、缺失 provenance 与不匹配修订。企业策略设计、物理多主机、远端签名发布及完整出口治理继续按 #33/#34/#58 验收。

## 开发环境验证

[工程证据](evidence/opa-tool-enforcement-20261124.json)：后端 451 项中 331 项通过、120 项按条件跳过；其中官方 OPA 集成 6 项、适配器 5 项通过。前端构建通过。开发环境完成 SDK 提交及浏览器提交、独立审批、真实 Worker 执行、策略记录和 Markdown 下载；切换前 50 条历史全部保留，验证后共 52 条，活跃任务为零。OPA 管理写接口拒绝访问。
