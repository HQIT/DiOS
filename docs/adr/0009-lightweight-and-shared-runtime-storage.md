# ADR 0009：轻量控制服务与共享事务服务分档选型

日期：2026-11-11 · 状态：接受部署与接口方向；候选按档位进入产品化验证。

承接 ADR 0008。E2AG 与运行时契约要求原子状态转换、持久确认、幂等、fencing 和证据完整性，不要求 SQL 或独立数据库服务。

轻量控制服务作为正式部署路线，由同一主机内的嵌入式事务存储承载权威小状态，通过 API 管理跨主机 Worker。SQLite WAL 与 LMDB 均通过本轮原子操作和故障检查；SQLite 优先验证既有查询/历史工具复用，LMDB 保留为事务 KV 替代。SQLite 100/s 到达的一轮出现较长尾延迟，默认实现冻结前需验证公平有界写入队列、混合负载与备份恢复。

共享事务服务优先将 PostgreSQL 对象粒度实现接入正式 Runtime。其独立 Run 吞吐优于当前 Workspace 串行参考实现；授权根、共享审批和撤销仍需显式共同排序，不能仅按 Run 分割而破坏跨对象原子性。MySQL、etcd、FoundationDB 和 TiDB 按实际部署需求与后续证据评估。

任务/尝试/会话、租约、幂等及 InvocationCommitter 接口保持一致。日志、大产物与管理查询按用途分层；轻量部署可把事务与查询合并在一个进程，扩展部署可拆分。仅因模块职责独立不要求引入多个基础设施产品。

产品化门槛包括：完整 E2AG/PEP、真实 Worker、升级排空、历史映射、备份恢复和断网 spool；多节点档另验同步复制、仲裁、旧主隔离、RTO/RPO 及长期混合容量。单节点结果用于实现选择，企业 SLO 仍需 #54 实测。#32 保持 In Progress，G2 独立评审待安排。

依据：[实验报告](../design/provider-selection-32.md)、[机器可读结果](../design/evidence/provider-summary-20261111.json)、[容量假设](../design/capacity-slo-v1.md)。

## 后续阶段性选择

[ADR 0010](0010-lightweight-runtime-default.md) 确认 SQLite 为轻量 Runtime 当前默认，有界 FIFO 写入与基本混合查询已验收。共享状态与高可用部署按实际故障模型独立验收。下一步推进历史/API 投影、离线 spool 与恢复交付。
