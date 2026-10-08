# rr022：入口约束与授权关系核查

2026-10-01。对应 R2.2、R2.4 与 RQ3，为 R1.1 的 Connector 范围提供有限补充。新增离线核查器和本地回归，不修改在线授权决策，不启动模型、容器或外部工具。

## 设计与结果

| 检查 | 实际路径/输入 | 结果 | 不能推出 |
|---|---|---|---|
| 多目标准入 | 原生 dispatcher、审批状态和 A2ATask；以原生 create_task 替换容器启动 | 4项通过：全部允许创建2任务；任一违反约束则0任务且不建审批；待审批0任务；批准恢复2任务且重复批准报错 | 多任务原子分发、运行时业务成功 |
| Connector | 原生 receive_webhook、三个适配器、隔离数据库和测试密钥 | 4测试方法通过：正确凭据接入git.push；错误凭据403且无EventLog；HMAC绑定请求体而GitLab令牌不绑定；空密钥存在开发旁路 | 三个公网部署、所有来源具有同等保证 |
| 离线关系核查 | 19类构造反例，每次重算哈希链 | 19项均哈希有效、关系无效并返回预期错误类别 | 自然故障检出率、所有I4分支覆盖 |
| 持久证据复核 | 既有3个真实HTTP故障数据库的只读快照 | 3事件/3任务/3能力；3调用意图中1已观察响应、2结果未知；数据库哈希不变 | 新增业务样本、未知等于未执行 |

在 backend 目录执行 .venv/Scripts/python.exe -m unittest discover -s tests -p 'test_e2ag*.py' -v，本轮终端结果为 **99 tests / 49.164s / OK，退出码0**。这是测试方法总数，不把子用例或适配器循环累计成业务规模。新增 test_e2ag_entry_contracts.py 含8方法，test_e2ag_evidence.py 含10方法。

## 核查器与可复现证据

backend/app/services/e2ag_evidence.py 的 verify_execution_evidence 接收可信、一致快照，独立检查哈希以及准入、任务、能力、审批、意图和响应的记录关系。任务实体先于能力，但任务分发完成的日志可以晚于能力日志；不强制所有阶段名按线性流程图排列。

它不连接数据库、不改变执行路径、不重新执行历史策略或恢复原始参数，不证明历史任意时刻的能力有效性或上游业务成功。未知模式/阶段/非tools/call标为不支持，缺少外部锚定时也不能证明没有整体回滚或尾截断。

复核脚本为 experiments/e2ag/rr022/check_evidence_relations.py。输入 results/queued-storage-faults-20261001-v2.json，输出 results/evidence-relations-20261001-v2.json；复跑必须使用新输出名。v1的18类反例记录保留，本文使用v2的19类。

| 文件 | SHA-256 |
|---|---|
| backend/app/services/e2ag_evidence.py | 71db282dd5053bf7ccf83a9c8cffbc6b3190d453d6f452ea8fe31c2ed387baeb |
| backend/tests/test_e2ag_evidence.py | e8d3907b19cebf8670bd9628cb80cd5d57528808568d66a7d16e4d7bc559beb3 |
| backend/tests/test_e2ag_entry_contracts.py | c682eb124bbe0537578c75adcc32d51998d43b6d9b51e53f8fc09f298d376b79 |
| experiments/e2ag/rr022/results/evidence-relations-20261001-v2.json | fdcfb8d712609938c32a7e43ba445168d5173763fe446f83dd76c7c7540fb920 |

## A1—A3具体映射

| 代码位置 | 事实与论文处理 |
|---|---|
| api/os/events.py、event_normalizer.py、git_webhook/normalizers.py | 鉴别先于标准化/落库；空密钥有旁路。A1需要配置，不由source匹配代替 |
| api/os/a2a.py::agent_jsonrpc | message/send可直接进入a2a_service。不是所有任务均经事件准入，须限定命题路径 |
| a2a_service.py::_send_to_task_agent、e2ag_tool_gateway.py | 任务模式代理；强制模式不下发stdio/SSE。中介与禁用分开 |
| agent_runtime.py::_write_configs | 服务模式使用原有MCP配置，不在任务模式保证内 |
| docker_runner.py、api/os/skills.py | 容器网络和本地命令路径未有全局封堵证据，不宣称全系统不可绕过 |
| api/internal/e2ag_mcp.py | 持久意图针对POST工具调用；GET/DELETE不自动继承同等保证 |

新稿§5表3、§6.6.3、§6.7、§7同步以上范围。源码核查、原生集成测试和既有真实HTTP轨迹分开报告。
