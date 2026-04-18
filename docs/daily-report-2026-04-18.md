# 工作日报 2026-04-18

## 今日目标

围绕「Main / Dev / Reviewer 三智能体协作开发」做端到端排障，恢复聊天可用性，并确保流程回归到业界通用做法（prompt + skills + 事件订阅），避免非共识方案污染代码。

## 今日完成

### 1. 核心故障定位与修复

- 定位 `SayHi` / `CodeDev-Main` 无响应的主因：请求链路在特定组合下触发 `RemoteProtocolError`，导致流式中断。
- 修复 deepagents 请求组装问题：避免多 system message 组合触发兼容性问题。
- 恢复 `SayHi` 可正常流式回复（验证通过）。
- 恢复 `CodeDev-Main` 可正常流式执行并返回结果（验证通过）。

### 2. Chat 前端稳定性修复

- 修复「发送后输入框锁死」：增加可中断与错误可见处理，确保失败时可恢复交互。
- 修复「刷新后默认焦点回 SayHi」：保留最近选中 agent。
- 修复「Skills 按钮点击白屏」：`SkillsEditor` 调用不存在的 `api.getSkillsCatalog()`，改为 `api.listSkills()` 并增加容错。

### 3. 行为边界问题复盘

- 发现 `CodeDev-Main` 曾出现“本地改文件但未推送 GitHub”的行为。
- 结论：这是 agent 职责边界与工具约束问题，不应靠岗位名硬编码解决。
- 已按要求回退非通用的取巧方案，不保留“按 agent 名称写死行为”的代码。

## 架构原则确认（本次明确）

后续仅采用行业常见、可扩展方案：

1. `system_prompt` 约束岗位职责边界
2. `skills` 控制能力范围
3. `subscriptions` / webhook 驱动协作流转
4. 避免按具体 agent 名称写死逻辑

## 当前状态

- Main / Dev / Reviewer 三智能体链路具备继续联调基础。
- 前端关键阻塞（Skills 白屏、输入锁死）已解除。
- 代码已回到「通用机制 + 配置驱动」方向，未保留命名硬编码分支。

## 下一步建议

1. 收敛并固化 Main / Dev / Reviewer 三个系统提示词（强调 Main 只分发，不直接改代码）
2. 跑一次完整 E2E：Chat 需求 -> Main 建 Issue -> Dev 提交 PR -> Reviewer 审核 -> Main 通知
3. 对异常分支补充回归用例（流式中断、工具调用失败、无 webhook 回调）

## 当日晚间补充（继续联调）

### 4. Dev/Reviewer 链路恢复

- 修复 DiAgent 任务模式中流式后重复 `ainvoke` 的问题，避免重复执行导致 `GraphRecursionError`。
- 重建 task 镜像并复测后，`CodeDev-Developer` 从 `failed(container exit code 1)` 恢复为 `completed`。
- 事件链路验证通过：`git.issue.opened -> Dev`、`git.pull_request.created -> Reviewer` 均可触发。

### 5. Connector 与 Subscription 约束统一

- 新增后端统一能力解析：按已启用 Connector 生成可订阅 `source_pattern` 与对应 `event_types`。
- 新增接口：`/api/os/connectors/source-patterns`，前端不再硬编码来源与事件能力。
- `subscriptions` 创建/更新增加强校验：
  - `source_pattern` 必须属于 Connector 命名空间
  - `event_types` 必须是该 source 的子集
- 结果：脚本直调 API 也不能绕过约束，Topology 不再出现游离来源。

### 6. 控制台 UI 收敛

- `SubscriptionEditor`：来源切换后事件列表联动刷新，并保持已选状态一致。
- `Topology`：订阅侧滑窗改为只读，不再在拓扑页创建/编辑订阅，避免与 Agent 页功能冲突。
- `EventLogList`：自动刷新开关状态持久化到 `localStorage`，避免每次重置为开启。

### 7. Master 管理能力与 HIL 交互升级

- Master 已切换到受控管理角色：通过 `dios_admin` skill + `dios cli` 管理 DiOS 资源。
- 修复容器边界：不修改 DiAgent 代码，改为 DiOS 在启动容器时挂载 `/workspace/cli` 与 `/workspace/skills`。
- `dios request` 增加 `--confirm-token`，支持“仅凭 token 确认并执行挂起动作”，避免 payload 漂移导致反复确认。
- Chat 流式链路新增结构化 `event: hil` 事件，前端不再从自然语言文本里猜 token。

### 8. Chat 体验与可观测性增强

- 修复 Send 按钮偶发无效问题（事件对象误传）。
- 删除会话改为更大按钮 + 二次确认，降低误操作。
- 流式显示增强：展示工具调用参数摘要、重复调用计数、调用耗时与结果摘要。
- HIL 卡片增强：显示 method/path、倒计时、过期禁用、确认防重入、标准化反馈文案。
- 接入 Markdown 渲染（GFM）+ 代码高亮 + 代码块复制按钮（前端依赖同步更新并重建容器）。

### 9. AI4R 事件驱动写作（MVP 基线）

- 在事件目录中增加 `ai4r.*` 标准事件类型（topic/experiment/draft/review）。
- 内部 source 扩展 `ai4r/*`，并由后端约束其可选事件为 `ai4r.*` 子集。
- 新增 `scripts/configure_ai4r_scenario.sh`：仅通过 `dios cli` 配置已有 Writer/Engineer/Reviewer 的提示词与订阅关系（不创建新 Agent）。

### 10. 下一步（已确认）

- 增加 AI4R 交付物下载能力：在 UI 支持按项目下载 agent 产物（如 outline、experiment plan、draft、review）并保留基础元数据。

