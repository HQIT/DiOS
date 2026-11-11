# DiOS AUTHZ-1.0.0

设计契约及可执行参考语义，详见 [授权设计](../../../docs/design/authorization-contract-v1.md)。

- authorization.schema.json：20 个类型；DecisionContext 是服务端解析后的内部输入。
- openapi.json：8 项生命周期接口；核心接口引用 contracts/v1/openapi.json 的 1.1.0 权限元数据。
- permission-matrix.json：39 项接口动作与资源校验规则。
- profile.json：期限、收窄、撤销与控制点证据门槛。
- decision-cases.json / examples.json：允许、拒绝、审批和结构校验样例。

在仓库根目录运行 `backend/.venv/bin/python scripts/validate_authorization_contract.py`。依赖 jsonschema，检查失败返回非零退出码。参考决策器验证契约语义，真实认证、沙箱与事务并发由 #33/#34/#58 验证。
