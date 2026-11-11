# DiOS v1 设计契约

版本：1.0.0。目标接口和状态来源；运行实现由 P1/P2 任务交付。

- `domain.schema.json`：分层对象与严格请求 Schema；每个定义包含 x-owner。
- `openapi.json`：核心跨层接口、参数、权限动作和错误。
- `states.json` / `state-cases.json`：状态转换与正负验收样例。
- `examples.json`：身份、授权绑定和输入输出样例。
- `protocol-profiles.json`：选定版本、能力差异和后续验收责任。
- `capacity-profiles.json`：两档容量假设，费用及性能实测保持未知。

在仓库根目录执行：

```bash
backend/.venv/bin/python scripts/validate_design_contracts.py
python scripts/model_capacity.py
```

设计：[领域/API](../../docs/design/domain-api-contract-v1.md)、[容量/SLO](../../docs/design/capacity-slo-v1.md)。Schema 校验和状态样例检查用于设计一致性；数据库竞争、真实权限和模型恢复由对应实现验收。
