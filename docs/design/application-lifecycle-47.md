# 独立应用生命周期与替代执行

独立应用以应用契约、发行包和固定 Revision 接入 DiOS。应用升级与 Harness 替换沿用 Deployment、E2AG 和任务恢复机制；应用业务代码、数据模型与界面由上层独立维护。

## 版本与执行边界

本项使用 `static-review` 参考应用、合成源码和已具备的 Cursor ACP / DeepSeek Harness 适配。测试发行包从 0.1.4 升至 0.1.5，两个执行镜像安装同一份 0.1.5 wheel，SDK 固定为 0.1.11。该测试包用于验证独立版本升级，应用功能不变。

两种实现共享 `external.static-review/v1` 输入类型、输入/结果 Schema 和应用契约。不同 Harness 的镜像、启动命令与模型凭据分别固定在 Revision 中。Run 同时记录应用版本、契约摘要、Revision、Work 授权、Attempt、Session 和产物。

应用准入由现有兼容检查控制；工具调用经独立审批。模型凭据通过 `worker_secret_refs` 显式授权挂载，工具服务凭据留在网关。上层应用不能通过任务输入扩大执行权限。

## 生命周期

| 操作 | 生效条件 | 原任务与历史 |
|---|---|---|
| 升级 | 候选版本健康任务成功；旧版本在途任务自然结束 | 旧 Run/Attempt/Session/产物保留原 Revision |
| 停用 | 管理登记 `enabled=false` | 拒绝新提交；在途任务继续完成，历史可查询 |
| 替代执行 | 同一应用契约由另一固定 Harness Revision 完成健康任务 | 业务输入/结果契约保持一致；新的模型会话独立记录 |
| 候选失败 | 保留明确失败任务和错误证据 | 已生效版本继续提供服务 |
| 回滚 | 前一版本重新获得 E2AG Work 授权并通过健康检查 | 新任务绑定回滚目标；既有任务身份不变 |

回滚恢复固定执行配置；应用自身的数据迁移由应用发行规则处理。当前资格使用无业务数据库迁移的参考应用。

## 原生故障与并行隔离

Cursor 和 DeepSeek Harness 各完成一次真实模型任务。Worker 在完整输出和进程退出回执持久化后退出；待原租约自然过期，恢复者先经历一次观测超时，再从原产物完成结果校验。恢复不启动新模型，原 Run/Attempt/Session/应用版本保留，旧所有者的心跳被拒绝。

另一个独立应用使用独立 Profile、Revision 和 E2AG 授权执行，在版本切换期间完成。其权限变更不改变评审应用的既有授权；原版本报告按内容摘要回读验证。

该资格在隔离的 Linux/WSL 原生 Docker 控制域运行。已有开发控制域、API 和用户任务保持运行。物理跨主机、应用业务迁移，以及完整 Harness 原生工具负向矩阵分别由 W4、上层应用和 K01-5.01 承担。

## 复现与证据

`backend/.venv/bin/python scripts/qualify_application_lifecycle.py --help` 列出入口。输入为管理员控制的两个 Harness Profile、另一个应用 Profile、凭据引用文件、升级镜像摘要与全新输出目录。源码、模型凭据和私有日志不进入仓库。

运行固定输出目录会保留 SQLite、每次尝试、审批、进程回执和内容寻址产物；失败不删除历史。只对未通过阶段补跑，已成立的模型结果保持原身份。

工程报告：[application-lifecycle-20261126.json](evidence/application-lifecycle-20261126.json)。API/SDK/UI 契约与兼容拒绝沿用 [A01-1.01 证据](evidence/application-contract-20261126.json)；本项验证实际部署生命周期和两个已有执行实现的兼容。
