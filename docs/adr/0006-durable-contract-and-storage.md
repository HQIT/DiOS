# ADR 0006：持久执行契约与企业状态存储

状态：已接受（P0 设计） · 日期：2026-11-11 · 关联：#25/#26、AP-05/08/10/11/13。

## 决策

统一领域契约版本为 1.0.0，公开路径采用 `/api/os/v1`、`/api/workforce/v1`、`/api/apps/{app}/v1`，内部执行采用 `/api/internal/v1`。保留现有 legacy API；核心结构使用 JSON Schema 2020-12 与 OpenAPI 3.1.1。

首轮标准协议固定 A2A 0.3.0 JSON-RPC/HTTPS 和 ACP v1 stdio。版本和实现能力形成独立 Profile，未来版本新增兼容路径。当前 DiAgent legacy 路径先修复已知差异再获得 A2A Profile；第三方活动取消和跨进程恢复按实测准入。

以 PostgreSQL 作为首个多节点权威状态、租约、幂等和 outbox 实现，数据库内事务领取及提交校验 epoch/version。平台数据仍通过 Repository 契约访问；单进程开发可用 SQLite。独立消息代理在负载和故障隔离需要时引入，不改变 at-least-once 与消费去重语义。

大产物进入可替换对象存储 Provider，元数据与进度事件保留独立路径。QuerySpec/as_of 固定 UI、统计与导出口径；业务覆盖账本属于 L3。E2AG 动作绑定 Run/Attempt/Session/Worker、策略与 epoch，执行意图先持久化，结果未知先对账。

## 取舍

事务库统一状态与 outbox 降低初期分布式一致性成本，承担领取竞争、热点与表增长压力。监控提交尾延迟、outbox 延迟和锁等待后决定分区或队列拆分。独立消息集群能隔离流量，增加协调与运维成本，保留为容量驱动的扩展方案。

固定旧协议 Profile 使 #57 的版本证据可复现；新版本能力通过新的 Profile 和同样的跨实现测试引入。精确协议能力与治理控制点分别准入。

## 验收与复审

[领域设计](../design/domain-api-contract-v1.md) 与 [容量设计](../design/capacity-slo-v1.md) 定义输入、公式、失败与迁移口径。#30/#32 实现事务竞争、fencing、迁移与 outbox，#56/#58 验证真实 Agent，#52/#53/#54 完成企业部署与达标证据。接受记录见 [联合交付记录](../design/review-f25-f26.md)。

出现事务库性能瓶颈、协议版本变化、对象所有权改变或 E2AG 强制执行语义变化时复审。具体产品补丁版本与生产切换由对应实现/交付任务固定。
