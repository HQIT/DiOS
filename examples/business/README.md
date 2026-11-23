# 业务资料核查工程样例

两个合成输入演示销售资料准备与项目资料引用。它们是同仓 L3 参考应用样例，资料均标注“工程样例”。客户交付系统的规则、数据和 UI 在各自应用中维护。

通用接口与分层见 [知识接入契约](../../docs/design/business-evidence-39.md)。运行需管理员配置：

1. 将 `sales-inputs.json` 或 `construction-inputs.json` 登记到 profile 的 `console.input_catalog`，input_kind 使用 `knowledge-evidence`。
2. 镜像包含 backend/app 和 backend 依赖，命令为 `python -m app.apps.evidence_brief`，`PYTHONPATH=/app`（按镜像实际包目录调整）。固定镜像 digest，network=none、read_only_root=true。
3. 启动 `python -m app.services.knowledge --catalog /absolute/catalog.json --grants /absolute/grants.json`。该服务只监听本机 13185，跨主机部署使用经过认证的受控服务入口。
4. grants 文件使用 `version:1` 与 grants 数组，每个启用项含 subject、project_id、token_sha256、documents。documents 按 `资料 ID:catalog 中 digest` 登记。原始随机凭据放在权限受限的文件，只交给工具网关的 FileSecretMounts。
5. MCP 工具注册 `search`、固定端点、credential_ref、INPUT_SCHEMA 和 knowledge.search 权限，注册版本用 FileToolCatalog 的实现摘要计算。profile 的工具别名为 knowledge_search，worker_secret_refs 为空。四层工作权限显式授予该工具，并配置独立审批凭据。
6. 使用既有 runtime CLI 启动 Worker；Console 和 `cli/dios task` 通过相同受保护 API 提交。`task samples` 查登记输入，`task submit --profile ... --sample sales-evidence-demo --goal '整理依据' --key ...` 发起任务。业务结果进入人工复核。

`catalog.json` 是轻量 Provider 的工程输入格式，包含资料 ID、项目、版本、source、locator、更新时间、active、正文和 digest。修改正文或来源后必须更新指纹及资料授权。不要把原始客户资料或凭据写入此示例目录。

自动浏览器验证：`node scripts/check_business_evidence.mjs /absolute/private-demo-directory`。验证目录提供已配置服务的 tokens.json（operator、approver）；脚本实际提交两个样例，检查审批、报告下载、刷新、旧评审输入切换及移动布局。

独立应用发布边界与后续打包见 [ADR 0011](../../docs/adr/0011-capability-only-platform-boundary.md)。DiOS 入口通过[数据化输入契约](../../docs/design/application-input-registration.md)接入，不导入此应用的业务验证器。
