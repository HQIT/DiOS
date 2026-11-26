# DiOS 下一批交付

执行基线为 [能力交付 WBS](capability-wbs.md) 和 [工作项及需求映射](capability-wbs.json)。总路线与产品边界见 [ROADMAP](../../ROADMAP.md)、[ADR 0011](../adr/0011-capability-only-platform-boundary.md)。

| 顺序 | 可验收交付 | 工作项 | 退出条件 |
| --- | --- | --- | --- |
| 1 | #31 固定版本、部署和失败回滚 | K02-1.01–.05 | 排队和运行不受后续编辑影响；实际版本可追溯；健康失败回退已知版本；外部端点能力变化触发复核；API/CLI/Console 一致 |
| 2 | #11 一致备份与干净恢复 | K02-3.01–.03 | 新增版本、委派、回执和产物纳入备份；空实例恢复引用完整；远端再认证和对账；升级恢复可计时 |
| 3 | #51 可安装、可升级的固定候选版本 | D01-1.01–.04 | 干净环境安装与受控任务通过；排空升级保留活跃任务和历史；失败回退可用；CI 构建和最小端到端可重复；发布责任人接受 |

**当前下一项：K03-2.02 E2AG 跨策略及故障资格；K03-1.02 已完成 [Linux Secret 交付](../design/evidence/secret-service-delivery-20261126.json)；K03-1.01 已完成[凭据续期与身份源轮换资格](../design/identity-lifecycle-qualification-33.md)；S02-2.03 已完成[检索授权、更新故障和受控工具资格](../design/evidence/knowledge-governance-qualification-20261126.json)；S02-2.02 已完成[双 Provider 契约与切换](../design/evidence/knowledge-provider-substitution-20261126.json)；S02-2.01 已部署[通用契约与对象直读实现](../design/knowledge-provider-39.md)；S02-1.04 已完成[对象生命周期资格](../design/evidence/object-lifecycle-qualification-20261126.json)；S02-1.03 已部署[留存、删除与本地物理回收](../design/object-lifetime-38.md)；S02-1.02 已部署[对象读取、下载与撤销](../design/object-access-38.md)；S02-1.01 已部署[统一对象版本](../design/object-versions-38.md)；W1 剩余链路等待源端隔离决策。K01-5.02 已完成[Cursor 首条路径资格](../design/cursor-recovery-upgrade-58.md)。K01-2.01 已完成[恢复组合资格](../design/recovery-combinations-30.md)。K01-1.01 已完成[生命周期工程资格](../design/evidence/lifecycle-closure-20261126.json)。D01-1.01 已完成[Linux 安装与后台运行交付](../../deploy/linux/README.md)。K01-4.01/.02/.03、K03-2.01 已完成；见[协议控制映射](../design/protocol-control-mapping.md)。** K02-3.02 等待源控制面隔离方式选择，恢复实例保持冻结；安装级备份 K02-3.01 已通过工程验证。 K02-1.01/.02/.03/.04/.05 已通过工程验证，见[版本存储](../design/evidence/runtime-revisions-20261125.json)、[执行绑定](../design/evidence/revision-entries-20261125.json)、[部署回滚](../design/evidence/deployments-20261125.json)与[外部兼容门禁](../design/evidence/endpoint-compatibility-20261125.json)。 必要的 Harness 恢复与治理资格按 WBS 依赖随本批完成。#9/#10 的已有工程证据进入结项审阅。

之后依次推进通用对象与知识、包准入与预算及配置发布、共享部署与物理跨主机容量。每批内部按工作项依赖推进，不用整个上游 Issue 尚未关闭阻断已具备条件的工程切片。跨主机完整验收由 W4 承接。

先完成 Cursor ACP 单一 Harness 的任务、E2AG 授权审批、工具、报告和恢复闭环。DSH/Codex 保留已有适配；第二来源实测与多 Harness 联合验收后置到首条路径交付后，兼容设计不引入新的首轮交付前置。

同时在制的实现结果上限为一项。每项包含交付物、原验收编号、依赖、完成验证、责任与现有依据；完成或阻断即更新，每次可部署交付报告剩余数和下一项。原验收通过后停止追加优化；新增范围单列影响，不隐式加入当前分母。

工程测试、开发部署持续执行，用户参与产品验收。客户应用分别拥有组织、岗位、业务流程与 UI，消费 DiOS 公共能力；现有持续评审继续运行，其生产接管由 #60 单独安排。
