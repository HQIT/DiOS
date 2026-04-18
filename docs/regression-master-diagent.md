# Master / DiAgent 回归清单

## 目标

验证以下增强在真实链路中可用且可控：

1. `SayHi -> Master` 默认入口迁移
2. `dios_admin` 受控管理能力（白名单 + HIL）
3. `reasoning` / `subagents` 能力下发
4. 事件到任务的可观测链路

## 回归步骤

### A. Master 基础

1. 打开 `Chat`，确认服务型默认入口存在 `Master`。
2. 调用 `GET /api/os/agents`，确认不存在 `SayHi`，存在 `Master`。

### B. dios_admin 能力

1. 在 Agent 编辑页设置 capabilities：
   - `dios_admin.enabled=true`
   - `dios_admin.scopes` 非空
   - `risk_policy.high_risk=hil_required`
2. 校验非法 capabilities 会被后端 `422` 拒绝。

### C. dios CLI + HIL

1. 执行安全读操作：
   - `python cli/dios request --method GET --path /api/os/agents`
2. 执行高风险操作（不带 confirm）：
   - `python cli/dios request --method DELETE --path /api/os/skills/<id>`
   - 预期返回 `pending_hil` + token。
3. 使用 token 二次确认后执行，检查 `~/.dios-cli/audit.log` 有记录。

### D. reasoning / subagents 下发

1. 给任务型 Agent 设置 capabilities：
   - `reasoning.recursion_limit`
   - `subagents[]`（name/description/prompt 必填）
2. 触发事件任务，检查生成的 `agent-task-*.json` 中有：
   - `task.recursion_limit`
   - `task.subagents`

### E. 观测链路

1. 触发一个 `git.issue.opened` 事件。
2. 在日志中串联查看：
   - `event_dispatcher`: event id + type + matched agents
   - `a2a_service`: task_id/run_id/context_id + container_id + exit status
3. 验证失败场景仍可定位到 `event_id -> task_id -> run_id`。

## 验收标准

- Master 入口稳定且不回退到 SayHi
- 高风险管理动作不经 HIL 无法落库
- `reasoning/subagents` 能从 DiOS 配置稳定下发到 DiAgent 任务执行
- 事件、任务、容器三段日志可追踪

## F. AI4R 事件驱动写作（MVP）

1. 先执行配置脚本（仅配置现有 Writer/Engineer/Reviewer，不创建新 Agent）：
   - `bash scripts/configure_ai4r_scenario.sh`
2. 校验事件目录：
   - `GET /api/os/events/catalog` 中包含：
     - `ai4r.topic.proposed`
     - `ai4r.experiment.requested`
     - `ai4r.experiment.completed`
     - `ai4r.draft.submitted`
     - `ai4r.review.completed`
3. 校验 source 能力：
   - `GET /api/os/connectors/source-patterns` 中包含 `ai4r/*`，其 `event_types` 仅为 `ai4r.*`。
4. 手动触发链路起点：
   - `POST /api/os/events/manual`
   - `event_type=ai4r.topic.proposed`
   - `source=ai4r/project-demo`
5. 在 Event Log 验证路由：
   - `topic.proposed -> Writer`
   - `experiment.requested -> Engineer`
   - `draft.submitted -> Reviewer`
   - `review.completed -> Writer`
