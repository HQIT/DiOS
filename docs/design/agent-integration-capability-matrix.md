# F01-3：智能体接入能力矩阵与首轮验证方案

日期：2026-11-11 · 关联：[F01-3 #57](https://github.com/HQIT/DiOS/issues/57) · 状态：能力勘察完成。执行实现、协议兼容整改和生产准入由 #25/#27/#30/#34/#35/#56/#58 承接。

## 选择结论

第一组为 **DiAgent 当前任务路径 + Cursor 原生 ACP**：覆盖既有执行语义与第三方 Harness，便于暴露平台任务/会话映射和恢复差异。DiAgent 路径须先通过规范符合性检查，当前 A2A 风格 API 不直接计为完整标准 A2A 实现。

第二组采用 **dsh 原生 ACP** 验证相同平台契约能替换 Harness；**Codex ACP Adapter** 作为另一实现和模型选择对照；Claude Agent ACP 纳入兼容矩阵。Agent 和模型分别配置，不固定 Windows、Cursor 或某一个供应方。

持续静态评审作为首轮业务样例，要求独立 Worker/Session、只读执行、完整产物校验和恢复。详见 [评审接入设计](continuous-code-review-assessment.md)。

## 证据等级与版本

“实测”仅覆盖列出的探测；“源码”表示当前实现可读；“文档声明”表示供应方契约；“待验证”表示本地未验证；“不支持”限于明确指定的版本/表面。initialize 中返回的能力是**实测取得的声明**，对应功能仍需执行验证。

| 对象 | 版本/定位 | 当前证据与许可 |
| --- | --- | --- |
| DiOS / DiAgent 路径 | 当前 `63b2e1c`；最新远端研究分支 `e7723ba` 的关键任务服务相同 | 源码/隔离函数检查；本机默认 service 镜像 ID `sha256:72bd16289ce1d21e7b8b7bcbf0469080c20aff9a60b239f2c4ac4f1cc404d92d`，无 RepoDigest；可分发构建及依赖许可由 #31/#44 固定。 |
| Cursor | 本机固定版本 `2026.11.05-86047df` | 实测版本、ACP initialize/认证、新会话、ask 模式、合成代码输出和同进程 load；厂商服务及部署条款另行核定。 |
| dsh | 官方 master `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`；根包 `0.2.2-rc.1` | 文档/源码快照，MIT；运行包和 lockfile 后续固定。 |
| Codex 本机 | `0.167.0-alpha.3.2` | 实测版本、App Server 帮助与本地 JSON Schema 导出；未启动模型执行。 |
| Codex ACP Adapter | `agentclientprotocol/codex-acp`，`ab7a968e982a95d8a765c1e6c6682a520d4f7638`；包字段 `2.6.0` | 文档声明；Apache-2.0；依赖 Codex `^0.166.0`，实际探测需锁定解析版本。旧 zed-industries 仓库已归档。 |
| Claude Agent ACP | `agentclientprotocol/claude-agent-acp`，`51f514d73c8bfd786979c84adb7a8517dbc22695`；包字段 `0.106.0` | 文档声明；Apache-2.0；SDK `0.3.322`。CLI/服务使用权与 Adapter 许可分别核定。 |

远端包字段是指定提交的源码版本，不表示已安装或已验证的发布组合。开源 Adapter 许可不自动覆盖模型服务、商标或厂商二进制再分发。

## 接口与能力矩阵

| 维度 | DiAgent 当前路径 | Cursor ACP | dsh ACP | Codex | Claude Agent ACP |
| --- | --- | --- | --- | --- | --- |
| 入口 | 源码：DiOS task/service；内部容器或 HTTP | 实测入口：`agent acp`，stdio JSON-RPC | 声明：`dsh --profile acp`，stdio，ACP v1 | 本机 App Server；ACP 由 Adapter 转换 | SDK/CLI 驱动的 ACP Adapter |
| 认证 | 平台入口 Token；执行环境另配 | 实测 cursor_login 成功；第二进程未取得成功认证结果 | 声明：ACP authenticate 无服务端认证；依赖可信本地控制者和模型凭据 | 声明：ChatGPT/API key/网关；实际账号待测 | 声明：供应方认证；无人值守路径待测 |
| 任务/会话 | 源码：A2ATask 与 AgentRuntime；缺 Attempt | 实测 new、ask、含输出会话同进程 load；空会话 load 返回参数错误 | 声明 new/list/resume/close，跨进程持久会话 | Schema 有 thread/turn；Adapter 转为 ACP session | 声明 ACP；版本能力须协商 |
| 更新与输出 | 服务路径当前非流式；任务读日志/文件 | 实测 session/update 与 end_turn，合成代码评审 JSON 可解析 | 声明提交后的语义更新；不暴露原始 provider delta | Schema/Adapter 声明流式 item/turn 与工具事件 | 声明工具、权限和输出事件 |
| 产物 | 源码内嵌 JSON/文本文件；缺验收 | 平台须从协议输出归集和校验 | 平台保存更新/产物；原始日志接入另定 | 平台保存事件/产物；不依赖可视界面 | 同左，需验证 SDK 返回完整度 |
| 取消 | 源码主要修改记录状态，执行中断未闭环 | 声明 session/cancel；真实终止/迟到结果待测 | 声明 cancel/close 及所属活动排空；范围和后续自发活动需测试 | Schema 有 turn/interrupt；ACP 映射待测 | 声明取消；子进程/子任务收敛待测 |
| 恢复 | 当前进程内轮询无持久恢复保证 | 同进程含输出会话 load 成功；跨进程、活动中断连、优雅退出仍待验证 | 声明 session/resume；不支持 session/load/历史重放 | Schema 有 thread/resume；Adapter 历史和重连语义待测 | v1/v2 能力不同，选定版本测试恢复/重放 |
| 权限控制 | 已有部分 MCP grant；Docker 挂载 rw | 声明 request_permission；ask/plan 模式需强制只读测试 | 声明一次 allow/reject；stdio MCP 命令和 HTTP 地址由可信 Client 授权 | Adapter 声明审批/sandbox/permission profile；本地 Schema 有 read-only | 声明工具权限；实际控制范围待测 |
| MCP | 当前平台已有配置与工具入口 | 实测声明 HTTP/SSE；文档还说明 stdio；逐项待测 | 声明 stdio/HTTP；不支持 SSE；仅消费 MCP tools | 声明 stdio/HTTP；具体版本待测 | 声明 Client MCP；具体传输待测 |
| 资源/并发 | Docker 生命周期基础；平台配额/独立尝试缺失 | 来源历史曾有独立 PID/Session；本轮不代表当前三路 | 声明单连接多会话；独立进程要求由 Supervisor 保证 | 本机仅 Schema；独立进程与会话尚待验证 | 本地无已验证安装；待验证 |
| 用量/成本 | 缺统一归因 | 精确费用与缺失场景待测 | 声明 context usage；成本仍须供应方数据 | Adapter 声明 token usage；计费口径待测 | 用量与费用完整性待测 |
| 本轮准入状态 | 平台基线候选，需契约整改 | 首个第三方候选；控制面已探测 | 第二个原生 ACP 候选；尚未安装探测 | 后续对照候选；桥接组合待探测 | 兼容候选；尚未本地探测 |

协议与 Harness 的恢复能力不能代替平台持久队列、租约和结果验收。dsh 的多会话支持也不能满足“三个独立进程”的业务验收，Worker 模式须单独配置。

## 已取得的本地探测证据

Cursor 在隔离临时空目录中启动固定版本，发送唯一 `initialize` 请求，2.36 秒返回 ACP v1；返回 loadSession、session list、MCP HTTP/SSE、image prompt 与 cursor_login 声明。探测未调用 authenticate、session/new 或 session/prompt；关闭输入后进程退出码 0。固定入口脚本 SHA-256 为 `1f2b237e37b3af72657c7160446079edc348faaff3363f4649479e0b1b5248cc`。

[Cursor 初始化证据](evidence/cursor-initialize-20261111.json) 与 [Codex Schema 摘要](evidence/codex-schema-20261111.json) 保存可复核摘要。Codex Schema 包含 thread/resume、turn/interrupt 与只读权限字段，说明本机协议定义存在；尚未验证实际服务调用、账号、模型输出或只读执行。

当前 Windows PATH 未发现 dsh、claude 命令；这不覆盖其他主机、WSL 或独立安装目录。未安装依赖、触发自动更新或改动现有执行机配置。

当前 A2A 路径已完成 [隔离函数兼容勘察](a2a-contract-survey.md)：按 0.3.0 固定基线，11 项中 3 项满足、8 项存在缺口，涉及 Card、Task、请求校验、上下文、终态取消与能力声明。当前路径保留为 legacy 候选；HTTP/鉴权和真实执行验收尚未覆盖，目标协议版本在 F01-1 固定。

补充实测：[空会话控制面](evidence/cursor-session-20261111.json)、[合成代码与含输出会话](evidence/cursor-model-session-20261111.json)。模型收到一段内联合成 Python 函数，无生产源码；返回 issue/condition/fixed_code JSON，结束原因为 end_turn。同进程 load 成功，第二进程未取得成功认证结果；原探针没有保留第二次认证错误详情，跨进程恢复保持待验证。退出码 1 出现在有界探针关闭之后，不能作为优雅排空证据。模型使用厂商当前默认选择，本轮未记录精确型号，不作为质量基准。

E2AG 原分支 10 项、产品候选快照 54 项隔离测试通过，含 13 项真实 stdio 子进程检查。详见 [E2AG 接入与扩展设计](e2ag-agent-integration.md)。第三方 Agent 内置工具尚未取得 E2AG 联合控制证据，不能凭 ask 模式或网关测试认定全部只读能力已通过。

## 统一接入契约的必要调整

接入登记至少保存 `agentImplementation`、`agentVersion`、`adapterVersion`、`protocolVersion`、启动参数/端点、认证 Secret 引用、能力快照、控制边界、许可和配置摘要。运行记录分别保存 Run/Attempt/Worker/Session/Artifact，模型与 Harness 版本独立记录。

恢复能力拆为 `reconnect`、`resumeSession`、`replayHistory`、`queryInFlight`、`deduplicateSubmit` 和 `cancelAcknowledgement`。dsh resume 不重放历史，Cursor load 的运行中行为未确认；不能压缩为一个 `supportsResume` 布尔值。

观测拆为协议心跳、语义输出、模型最终结果和 Worker 存活。dsh ACP 只暴露提交后的更新，不能从事件稀疏直接推断模型停摆。供应方内部重试不可见时明确记录 unknown，不填为零。

每个协议能力保留声明值、实测值、证据时间与兼容范围。ACP v2/厂商扩展按显式协商启用，第一轮优先已核对的 v1 交集；完整 A2A 版本和 Agent Card/任务/流/取消规范在符合性探测中固定。MCP 是工具契约，Skills 是能力内容与说明，Plugins 是可交付扩展包；三者不承担调度所有权。

## 可执行验证样例与准入门槛

样例采用独立临时工作区中的合成两版本仓库：V1 含一项明确边界检查缺陷，V2 修复该缺陷；另外两个分支共享相同文件但有不同调用上下文。输入固定 SHA、文件清单、只读检索接口和 JSON 结果 Schema，不使用生产源码。

| 样例 | 操作与证据 | 通过条件 |
| --- | --- | --- |
| C0 控制面 | 版本/摘要、initialize、能力协商与关闭 | Cursor 已完成 initialize 子项；各选定实现仍需完整能力探测。 |
| C1 身份与结果 | 提交固定样例，记录 Run/Attempt/Process/Session，保存输出 | 身份可追溯；位置、可达条件、影响与证据通过验证。 |
| C2 独立并发 | 三路独立进程，每路独立 Session | PID/创建时间/Session 与实时活动证明实际并发。 |
| C3 只读拒绝 | 请求写源码、调用构建/脚本、越权访问 | 控制点拒绝，源码摘要不变；权限事件与动作结果一致。 |
| C4 取消与断连 | 取消运行中工作，断开/重连，观察迟到输出 | 取消请求/确认/未知分开，既有工作不被重复提交。 |
| C5 崩溃恢复 | 结束测试子进程、重启 Worker、完整输出后断状态提交 | RPC 收敛，身份连续；完整产物优先恢复校验。 |
| C6 额度和网络 | 模拟暂态、限流、quota，再恢复 | 退避、半开单路和并发恢复有状态/时间证据。 |
| C7 协议符合性 | A2A 任务状态/产物/取消及 ACP 会话/权限/不支持请求 | 规范、版本、能力缺失和扩展均有明确结果。 |
| C8 替换 Harness | 相同业务输入由 Cursor 与 dsh/Codex 执行 | 平台业务身份与覆盖口径一致，差异进入能力矩阵。 |

A2A 首选验证当前 DiAgent 暴露路径；若规范符合性缺口阻塞，使用固定版本的标准参考 Agent 作为协议对照，DiAgent 保留为 legacy Adapter，不能伪称标准兼容。协议模拟器可验证错误与状态处理，真实模型的质量、费用和执行治理仍需真实 Agent 样例。

## 完成验收与后续责任

F01-3 的三项验收均已有交付：五类 Agent 的版本化矩阵与证据等级；至少两个不同来源的首轮组合及 A2A/ACP 可执行验证方案；数据外发、凭据、控制边界、选型依据和未知项。研究结论为 Cursor 原生 ACP 优先、DiAgent 通过 legacy 路径保留并整改 A2A、dsh 作原生 ACP 替换对照，Codex/Claude 通过版本化 Adapter 后续验证。

| 未覆盖项 / 缺口 | 责任任务 | 退出标准 |
| --- | --- | --- |
| A2A 8 项差异、目标标准版本与 Schema | #25/#56 | 固定版本，严格客户端互操作、HTTP/鉴权及状态语义通过。 |
| Cursor 活动中取消、跨进程恢复、真实三路并发 | #30/#56/#58 | 独立进程/会话证据、取消确认、重启对账和无重复派发。 |
| E2AG 内置工具控制、授权映射、续期及 Attempt 绑定 | #27/#33/#34/#35/#58 | 实际拒绝、撤销、续期不扩权、旧 epoch 不可复用。 |
| dsh/Codex/Claude 运行组合及许可准入 | #31/#44/#56 | 固定可分发版本、凭据和控制配置；按相同样例测试。 |
| 跨实现质量、覆盖与生产接管 | #58/#59/#60 | 故障集、独立质量对照、覆盖账本、历史与切换验收。 |

以上事项保留为实现与准入门槛，#57 完成表示接入选择和能力调查形成结论。E2AG 的不足继续通过 Adapter、沙箱、策略和证据能力扩展，详见对应改进表。

随后收口 F01-1 的持久运行和接入契约；与 F02-1 一起确定只读和外发边界。K01-2 优先实现所有权、恢复和排空，K01-4/K03-2 接通协议和控制点，K01-5 使用持续评审故障集联合验收。生产源机切换安排在这些门槛通过之后。

## 一手来源

- [Cursor ACP](https://cursor.com/docs/cli/acp)：入口、会话、权限及厂商请求。
- [dsh ACP 固定提交契约](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/acp/acp/README.md)；[启动 Profile](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/bundle/acp-app/README.md)。
- [Codex ACP 固定提交](https://github.com/agentclientprotocol/codex-acp/blob/ab7a968e982a95d8a765c1e6c6682a520d4f7638/README.md)：App Server 桥接、权限和事件。
- [Claude Agent ACP 固定提交](https://github.com/agentclientprotocol/claude-agent-acp/blob/51f514d73c8bfd786979c84adb7a8517dbc22695/README.md)：SDK/CLI 适配与协议版本。
- [A2A](https://a2a-protocol.org/latest/)；[ACP Agent 名录](https://agentclientprotocol.com/get-started/agents)。
