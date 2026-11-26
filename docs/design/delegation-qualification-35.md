# 三层委派与子任务资格

DiOS 的委派链在签发、任务准入、执行授权、工具派发和结果接收处检查权限交集、来源身份及授权代次。K03-3.02 的工程资格按下表对应原验收要求；[机器可读证据](evidence/delegation-combinations-20261126.json)包含专项结果、复用证据与代码摘要。

| 原验收 | 控制点与证据 |
| --- | --- |
| K03-3-A01 越权与提权 | 原生 OPA 拒绝权限元组扩张、动作与资源交叉拼接、错误主体/受众/作用域和其他 Work 绑定；子任务接收方的发行者身份与父任务绑定独立核验。同 actor 的其他签发者不能借用委派。 |
| K03-3-A02 停用后拒绝动作 | 三个独立认证签发者形成三层委派；中间签发者通过受控更新停用后，叶任务 Work/Grant 撤销，已批准调用未到达 MCP；恢复签发者也不能复用旧委派。既有原生 Worker 证据验证请求停止、退出确认与原 Attempt/Session/产物保留。 |
| K03-3-A03 重复与乱序 | 来源事件账本去重并保留来源时间单调上界；持久撤销跨拒绝回滚保留，重复变更不增加撤销代次。子任务账本保留父子关系与幂等身份，并发领取最后一个子任务名额不会超额。 |
| K03-3-A04 可追踪的撤销范围 | API、CLI、Console 展示来源事件及撤销审计；Worker 尚未确认退出时任务保持非终态。使用受控身份更新观测的停止请求/退出时间，传播目标仍由部署环境测量。 |
| K03-3-A05 参数、会话与并发 | 真实 Cursor 三层委派记录覆盖参数变更、审批失效和重复派发；本轮三路并发派发同一调用仅产生一次上游工具调用，替换 Session 明确返回 `SESSION_CHANGED`。子任务 Session/epoch 伪造被拒绝。 |
| K03-3-A06 三层收窄与审批资格 | 原生 OPA 的根及一至三层权限交集均有执行回执；第四层及期限扩张被拒绝。独立审批人的资格在批准与最终派发重新检查，自审批拒绝；来源变更发生在策略等待期间也无法完成子任务准入。 |

## 执行与证据范围

新增组合测试使用 Linux 临时 SQLite、真实 HTTP 认证入口、固定摘要的原生 OPA 容器及本地 MCP HTTP 服务。三个并发调用使用独立异步派发请求；上游调用次数由服务实际接收记录确认。子任务专项使用现有运行夹具验证资格与资源名额。

真实 Cursor 执行复用 [Cursor 原生控制证据](evidence/cursor-native-controls-20261126.json)。原生子任务进程与会话复用 [子任务通道](evidence/child-task-channel-20261125.json)和[独立接收方](evidence/recipient-harnesses-20261125.json)记录。各项证据分别标明模型执行、原生进程或协议夹具；历史记录中的其他 Harness 仅作为既有证据。

来源语义和受控更新保证见[撤销传播契约](revocation-propagation-35.md)。多节点来源提交与本地 Runtime 提交仍是不同事务边界；外部来源尚未送达的变更不能由本地快照推断。独立安全评审由 K03-3.03 承接。

## 复现新增组合

```bash
PYTHONPATH=backend:backend/tests DIOS_TEST_OPA=1 backend/.venv/bin/python -m unittest \
  test_delegation_combinations.DelegationCombinationTests.test_middle_issuer_revoke_fences_leaf_approval_and_new_admission \
  test_delegation_combinations.DelegationCombinationTests.test_three_level_concurrent_dispatch_consumes_one_approval -v
```

完整专项测试名称与复用文件摘要见机器可读证据。测试使用隔离库与临时容器；本项只增加测试及交付文档，沿用 K03-3.01 已部署实现。
