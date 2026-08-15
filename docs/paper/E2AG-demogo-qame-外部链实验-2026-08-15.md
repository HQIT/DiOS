# demogo × HQIT/qame 外部链实验记录

本次实验使用 `HQIT/qame` 的 `e2ag-experiment` 分支和 demogo.work 上隔离部署的 E2AG 后端，验证一条真实 GitHub Push 经事件接入、任务智能体、ToolGrant、MCP PEP 到外部 `git-perf` 服务的完整路径。实验不触碰生产数据库或生产后端镜像，隔离后端仅绑定 `127.0.0.1:18081`。

最终样本为提交 `148ddf2c2c19f9adc991a2be79cde0845b4979ed`。GitHub webhook 在生产入口返回 200；同一原始 payload 经原 Connector 密钥签名后重放至隔离后端，生成事件 `868f8eab67ca`、trace `1c65e55240994b10965b49ba4791f223` 和任务 `ba0427c2e7144cb0890c31c3f3360bf0`。契约与目标策略分别在 132 μs 和 24 μs 内放行。

任务最终状态为 `completed`，收集到 1 个结果 artifact。审计链记录 5 次 `tools/call`，ToolGrant 的原子计数同为 5，授权在任务终态后变为 `revoked`；因果链追加 `a2a_task/completed`，记录退出码 0 和 artifact 数量 1。外部 `git-perf` 中出现与 before/after SHA 精确匹配的 E2AG push 记录 `6422a9dd6780`，证明副作用到达了 DiOS 进程之外的服务。

这条样本的作用是证明部署可行性与因果闭合，不用于估计攻击成功率、性能分布或外部方法优劣。生产基线与隔离 E2AG 共享 `git-perf` 状态，因此同一 commit 的 `record_review` 遇到已有评审行；E2AG 的授权与上游到达由 200 状态的工具审计记录证明，`git-perf` 仅保留一条规范化 review 行。结构化原始摘要见 `experiments/e2ag/results/external_qame_demogo_summary.json`。
