# 工作日报 2026-04-21（WIP）

## 今日目标

围绕「Master2 委托 AI4R 组写作」链路，补齐可观测性并定位为何 Writer / Engineer / Reviewer 未形成事件闭环。

## 今日完成

### 1. 活动总览（甘特）能力落地

- 新增全局活动甘特接口：`GET /api/os/events/activity-gantt`
- 新增活动总览视图：按智能体分行（纵轴）+ 统一时间轴（横轴）
- 甘特条支持状态颜色和行为点（start / running / end / error / artifact）
- 活动总览新增按单日期过滤（`date=YYYY-MM-DD`）

### 2. AI4R 链路问题定位（基于运行证据）

- 现象确认：仅 `ai4r.topic.proposed -> Writer`，未出现后续 `Engineer/Reviewer` 触发
- 关键证据：
  - `Writer` 的 A2A task 持续失败，错误为 `GraphRecursionError`
  - 对应 `task.log` 多次显示 `Recursion limit ... reached without hitting a stop condition`
- 结论：当前不是订阅路由问题，而是 Writer 执行阶段未完成、未发布后续事件

### 3. 通用事件发布机制（非 AI4R 专属）接入

- 新增通用工具 `publish_event`（DiAgent 内置工具）
- 发布前校验：
  - `event_type` 必须存在于 `/api/os/events/catalog`
  - `source` 必须符合 `/api/os/connectors/source-patterns` 且事件类型可用
- 接入路径：
  - task 模式内置工具加入 `publish_event`
  - service 模式支持 `tool_selection` 选择 `publish_event`
- Chat 侧默认工具选择从 `shell` 扩展为 `shell + publish_event`（有 skills 的 service agent）

### 4. 脚本与配置修复

- 修复 `scripts/configure_ai4r_scenario.sh` 的 stdin/JSON 解析问题（可稳定执行）
- AI4R 场景 prompt 更新为“必须调用 `publish_event` 推进事件”
- AI4R 三个 agent 的 reasoning 参数提升（recursion_limit / max_tool_rounds）

### 5. 其它稳定性修复

- 修复模型映射解析：支持按 `name / id / model` 匹配（避免 task 配置 `models` 为空）
- task runner 支持透传 `max_tool_rounds` 到运行配置

## 当前状态（WIP）

- 可观测性层面已具备完整排障能力（可直接看到各智能体任务时间线）
- 通用事件发布能力已具备，但 AI4R 实际闭环尚未打通
- 当前主阻断仍是 Writer 在执行阶段递归耗尽，导致无法稳定发布后续事件

## 下一步

1. 引入通用“确定性事件推进”策略（配置驱动，不绑定 AI4R）
2. 降低对 LLM 自主收敛的依赖，确保事件链路可预测推进
3. 复测并验收完整链路：`topic -> experiment -> draft -> review`

