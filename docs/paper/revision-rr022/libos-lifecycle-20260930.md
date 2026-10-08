# E2AG / Agent libOS 生命周期配对小试

2026-09-30。已完成正常授权、过期、撤销3例配对，并加入1个到期前正常控制。执行结果相同，未体现阻断效果优势；观察到限定入口下拒绝审计的差异，需要进一步检查其可配置性和集成成本。本轮没有修改上游源码、后端产品代码或论文正文。

## 设计与结果

继续使用 `common-cases-v0.1.json` 的 T01、L01、L02。两个方法使用相同工具名、参数、允许集合和时间/授权状态输入，编号与预期标签仅由父进程核对。工具均是同一内存回执函数，没有模型决策或外部业务写入。每次运行独立进程与内存数据库。

| 场景 | E2AG | Agent libOS |
|---|---|---|
| T01 正常能力、未到期 | 允许，1条回执 | 允许，1条回执 |
| L01 now=expires_at | 拒绝，0回执 | 拒绝，0回执 |
| L02 能力已原生撤销 | 拒绝，0回执 | 拒绝，0回执 |
| 到期前1个示意时间单位，夹具控制 | 允许，1条回执 | 允许，1条回执 |

逐对输入哈希和实际回执内容一致。拒绝时 libOS 的工具 provider 调用计数为0，没有外部效果记录；允许时其真实 MCP primitive 完成调用，provider 产生1条回执。最后一项是边界控制，不增加独立业务样本规模；8条执行不是8种独立工作负载。

## 实际路径与适配披露

E2AG 使用上一轮修复后的真实能力签发、SQLite 状态和 `proxy_mcp_call`。libOS 固定作者提交 `85e8ff94c1e562bfb1f891714e3bf4bed9795690`，使用 `Runtime.process.spawn`、`capability.grant/revoke`、`mcp.register_server` 和 `mcp.call_tool`。没有用共享 Python 布尔函数替代其授权判定，也没有先按预期拒绝再返回结果。

libOS 以 Host 初始化原生进程和 MCP 工具能力，权利为 read，工具名称按固定映射使用 `report_create`/`report_preview`，保留原业务名与参数生成回执。这是无外部状态修改的报告夹具，不声称验证了写操作或业务数据访问。任务/事件上下文虽然随共同输入提供，本轮只对齐有效期和撤销；不能据此声称 libOS 实现了 E2AG 的 Event→Task→Grant 绑定。

外部 provider 继承作者固定版本的协议测试替身，仅增加内存回执；授权、状态转换、资源与数据流前置检查、原生审计均保留。协议回执中的字节数、时长来自测试替身，**不是网络流量或性能测量**。使用作者 Manifest v2 测试路径，不据此验证它的全部传输版本。

共同示意时间映射到固定时刻。libOS 只替换能力评估器的时钟输入与签发时间输入，原生过期比较和状态转换保持不变；审计时间仍是实际墙钟，不据此计算运行时延。E2AG 过期后将状态更新为 expired；libOS 仍保存 active 标志但通过 expires_at 拒绝，两者状态表示不同，不强制改成同一种状态机。

## 拒绝审计的局部差异

本次 L01/L02 调用中，E2AG 各新增1条 tool_call/deny，含明确原因。libOS 抛出 `CapabilityDenied`，当前进程完整审计查询在调用前后没有新增条目。正常调用则有 `capability.authorize`、`primitive.mcp.call` 等原生审计，不能将上述现象概括为“libOS没有审计”。

源码定位：`agent_libos/primitives/mcp.py` 的 `_authorize_call_visibility`（约8601行）先调用能力授权接口，无权时提前抛出异常；`capability/manager.py` 的 `authorize` 默认 `audit=False`。本轮未调整这些默认行为。`store.list_audit()` 无 limit 且不带筛选，已核对调用前后总数，排除分页遗漏。

这可以作为“该固定配置的调用拒绝证据路径不同”的观察，尚不能当作整体安全性优势或论文主要创新。E2AG 在上一轮修复后才补齐同类记录，前后证据已披露；对方也可能通过合理的 Host 层集成增加审计。正式比较需要允许这种明确披露的集成，并考察跨层证据归属、覆盖和代价，而不是通过默认配置缺口寻找胜率差距。

## 失败与修正记录

v1：夹具把 libOS 的地址改成不可解析的 `receipt.invalid`，而其原生 MCP 前置检查即使使用替身仍会解析地址，导致2条正常控制出现配置错误。此轮保留，不算授权失败；也不能只凭其中负例已拒绝就宣布基线接通。

v2：改回作者夹具的数字回环地址后8条执行符合预期。没有启动监听服务，工具 provider 不打开这个地址。随后核查发现只冻结评估时间会使能力记录的 issued_at 与实验 expires_at 时钟不一致。

v3：同时对齐能力签发时间输入，再完整重跑，8条执行仍符合预期。新增检查确认 issued_at<=expires_at。仅修正本地夹具，上游 checkout 保持干净，三个版本原始记录均保留。

## 证据与复现

```powershell
python experiments/e2ag/rr022/run_libos_lifecycle_pilot.py --output experiments/e2ag/rr022/results/libos-lifecycle-pilot-NEW.json
python experiments/e2ag/rr022/check_libos_lifecycle_pilot.py --result experiments/e2ag/rr022/results/libos-lifecycle-pilot-NEW.json --output experiments/e2ag/rr022/results/libos-lifecycle-checks-NEW.json
```

阶段依据：`experiments/e2ag/rr022/results/libos-lifecycle-pilot-20260930-v3.json`，SHA-256 `8c76a806208d75b1a8f0ba588a3a9b4297a1c666c9469ade325a797fe10f21dc`。独立核查记录为 `libos-lifecycle-checks-20260930-v1.json`，包含输入一致性、实际回执、原生 provider 次数、能力状态与审计增量验证。结果附实现、夹具及依赖哈希/版本。

L03“存储中的任务 trace 与能力不一致”未映射为 libOS 的主体隔离，不计入对方失败或通过。独立工作负载、参数/资源约束、完整上下文 OPA及性能仍未完成。下一步进入参数/资源共同规则接入，并继续核查绑定对象与证据语义；暂不据小试结果改写论文优势结论。
