# 应用输入登记契约

2026-11-24 · #47 接入切片 · [产品边界](../adr/0011-capability-only-platform-boundary.md)

管理员为 profile 登记 console.input_kind（应用命名的任务类型）与 console.input_catalog（控制面本地绝对路径）。客户端仍通过 API/CLI 选择 profile_id、sample_id、goal 和幂等键；路径、命令和验证器均不能由客户端注入。

目录 JSON version=1，inputs 为 1–20 个 JSON 对象；dynamic_inputs=true 时可为 0–20 个且必须提供 schema，文件最多 64 KiB、单输入最多 12 KiB。每项含 id、name、sha256；应用可定义其他字段。schema 可提供 Draft 2020-12 JSON Schema；仅支持文档内引用，外部 URI 与 $id 被拒绝。目录只含数据，DiOS 不加载应用 Python 模块。

input_digest 默认 canonical-sha256，计算去除 sha256 后的 Runtime canonical JSON 摘要；source-sha256 用于兼容原静态代码样例。requires_e2ag 默认 true；可信本地测试目录可显式设 false，仍受操作员认证和运行 profile 约束。是否使用工具、凭据及其 E2AG gate 继续由 profile 与现有执行控制点强制执行。

提交冻结输入与目录摘要。输入的业务条件由应用校验；Run 技术成功与业务验收继续分别记录。task_kind 为应用标识，历史以 origin=task-console/v1 读取。旧两类任务仅保留历史读取兼容，禁用或删除登记不删除已有历史。sample_id 和 payload.sample 为现有 API/CLI 兼容字段，公开 SDK 提供 submit/submit_input 两种提交方法，升级不重置身份。

旧静态评审 profile 激活前必须登记 examples/runtime/review-inputs.json 的部署路径；已有知识目录兼容。先补齐旧 profile 目录配置，再部署新控制面。工具策略、镜像和执行 profile 指纹不因目录路径变更而改变。活动任务消费已冻结输入；新准入采用当前目录，改变目录后复用原幂等键会发生内容冲突，需要新逻辑任务键。

已支持 input_payload 动态输入、独立应用包与 result_schema。sample_id 与 input_payload 必须恰选其一；缺失 sha256 时服务端按登记策略计算，已有摘要不一致则拒绝。result_schema 随 Run 冻结，Worker、控制面完成接口与恢复对账均校验完整结果，失败保留原始输出。详见[独立应用包](application-packages-47.md)。Console 是操作员入口；多主体资源权限由 #33/#35/#38 接续。


## 验证记录

[2026-11-24 工程证据](evidence/capability-boundary-20261124.json)：8 项新增登记/依赖测试；整体后端 298 通过、117 条件跳过；前端构建、真实 Worker/E2AG 审批及报告、CLI 目录读取、原 37 条历史保留均通过。Console 采用通用任务输入和提交文案；结构化输入以 JSON 预览，切换配置和语言保留用户目标。

复现界面验证：`node scripts/check_application_registration.mjs /private/config /private/output`；真实工具链：`node scripts/check_business_evidence.mjs /private/config /private/output`。前者对第三种类型使用模拟目录验证 UI，后者使用实际 Worker 与 E2AG；两者证据分别记录。
