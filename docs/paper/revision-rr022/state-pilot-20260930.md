# 审批与生命周期小试：执行控制与证据完整性分开验收

2026-09-30。本轮完成3个审批用例的 E2AG/AgentSpec 配对运行，以及3个生命周期用例的 E2AG 原生路径运行。9条方法—用例执行均符合预定的阻止/执行结果，但生命周期拒绝审计存在缺口，不能据此宣称整套治理已验证完成。论文正文、已提交 PDF 和后端产品代码均未改。

## 审批结果

| 共同用例 | E2AG | AgentSpec |
|---|---|---|
| A01 尚未答复 | 数据库 pending，0任务、0回执 | 原生 UserInspection 等待输入，0回执 |
| A02 请求后答 yes | approved；创建1任务，经工具代理产生1回执 | 原生询问返回后执行工具，1回执 |
| A03 请求后答 no | rejected；0任务、0回执 | 原生 skip 跳过该动作，0回执 |

6条运行初始输入的哈希完全相同。answer 在父进程单独持有，直到收到原生审批请求/状态检查点后才通过另一条管道消息送入，未预先交给被测机制。检查确认顺序为“请求→回答→结果”，回答前均无回执。两种方法收到相同工具、参数和业务规范，不接触用例编号或 oracle。

A01 在检查点后观察工作进程仍未完成，再由夹具终止。此终止只是结束观察，不解释成拒绝、异常或无限期安全保证。E2AG 核心接口此时已经返回 pending，等待答复的是夹具；AgentSpec 阻塞在原生同步询问。不能把两者混称为相同阻塞模型。

E2AG 经过真实 `dispatch_event → create_approval → decide_approval → resume_approved_event`。只替换 Agent 执行为固定规划器：调用原生 `create_task`，再签发真实任务能力、调用真实 MCP 代理，出站服务仍为内存测试替身。正常/拒绝分支不由脚本根据答复创建任务。批准后再次批准，以及拒绝后试图批准，均返回 `APPROVAL_ALREADY_DECIDED`，任务数不增加。该串行控制检查不计为并发竞态证据。

审批配置映射明确披露：夹具通过原生 Connector registry 注册 `fixture/github/qame` 的 git.push 声明；`approval.required=true` 映射到现有敏感操作规则的 `admin.report` 和 production 标签，实际工具仍是无害的 `report.create`。没有改写 E2AG 判定函数，亦未验证来源签名。AgentSpec 使用原生 `user_inspection` 规则，先执行工具集合硬约束，再执行审批规则；沿用已披露的任务结束 None 兼容适配。

本轮只验证正常授权请求的审批分支，未验证“审批期间权限变化”“审批同意能否覆盖其他硬拒绝”。E2AG 使用 SQLite 内存数据库里的已提交状态，没有测试重启恢复；AgentSpec 的 no 表示动作跳过，不代表持久化任务拒绝终态。

## 生命周期结果及审计缺口

| 用例 | E2AG 实际响应 | 工具回执 | 此次调用新增审计 |
|---|---|---:|---|
| L01 到达过期边界 | 403 / TOOL_GRANT_EXPIRED | 0 | 1条 grant_expired 状态变更，无专门 tool_call 记录 |
| L02 原生撤销后调用 | 403 / TOOL_GRANT_NOT_ACTIVE | 0 | 0；撤销操作自身有更早记录 |
| L03 已存任务与能力 trace 不一致 | 403 / TOOL_GRANT_TASK_MISMATCH | 0 | 0 |

L02 通过真实 `revoke_task_grants` 转换状态，不直接把共享谓词改成 false。L03 在能力签发后改变存储中的任务 trace，是存储关联异常，不是凭据盗用防护实验。额外正常控制在过期前一个示意时间单位调用，返回200并产生1回执，排除夹具无条件阻断。

已有审计链的哈希校验均通过，但“已有链未损坏”不等于“每次拒绝都被记录”。当前 `validate_grant` 对 inactive 与 task mismatch 提前返回，代理随即返回403，没有调用 `record_tool_decision`；expired 分支只写状态变更。这与论文拟主张的可核查拒绝证据需要进一步对齐。

**待修复项：为可安全归属的能力拒绝补齐调用级审计，并增加分支回归。**必须区分持有有效令牌但已过期/撤销/关联异常，与未知 ID 或无效令牌请求；不能仅凭请求中的 grant ID 向他人事件链写入未鉴别内容。先保留本轮原始缺口证据，修复后另存新结果，不覆盖为“原实现本来正确”。本轮没有实施该产品修复。

生命周期组尚未接入 Agent libOS 原生 MCP/能力路径；AgentSpec 也未通过新增通用谓词冒充持久化生命周期。这些是当前未完成的接入工作，不计为基线失败或成功拒绝，不形成比较胜率。

## 证据与复现

```powershell
python experiments/e2ag/rr022/run_state_pilot.py --output experiments/e2ag/rr022/results/state-pilot-NEW.json
python experiments/e2ag/rr022/check_state_pilot.py --result experiments/e2ag/rr022/results/state-pilot-NEW.json --output experiments/e2ag/rr022/results/state-checks-NEW.json
```

原始结果为 `experiments/e2ag/rr022/results/state-pilot-20260930-v1.json`，含输入、管道顺序、原生状态、回执、审计和源代码/依赖记录。`state-checks-20260930-v1.json` 保存独立核查与过期前正常控制。A01 因主动结束观察，没有完整终态报告，其检查点和终止原因单列。所有结果均为本地确定性实验，没有模型调用、真实远端执行或性能测量。

## 累计完成与下一步

14例草案中，T01/T02及A01—A03已完成 E2AG/AgentSpec 配对；L01—L03只完成 E2AG；T03—T06与E01/E02尚未运行。夹具控制、重复答复检查不扩充独立样本规模。

先补上述拒绝审计及回归，接着完成 Agent libOS 生命周期对照；参数/资源约束、完整上下文 OPA、独立工作负载与完整路径性能仍按既定计划推进。正式结果冻结之前继续保留已投稿 v1.1.39，不提前改摘要、结论或填写“审稿问题已解决”。
