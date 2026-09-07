# DiOS 介绍视频 — 脚本与素材清单

> 用途：产品介绍视频录制与剪辑参考  
> 建议时长：3:30–5:00（完整版）/ 约 90 秒（精简版见文末）  
> 基调：技术产品介绍，类比清晰、面向开发者与团队负责人  
> 最后更新：2026-06-04

**已生成素材包**：[`docs/video-assets/`](./video-assets/README.md)（SVG / PNG 示意图 / UI 截图 / 字幕 SRT）

---

## 一、视频信息

| 项目 | 说明 |
|------|------|
| 产品名 | DiOS（Deep Intelligent Operation System） |
| 关联概念 | NANA、DiAgent、DiFlow |
| 内置 App | Console（管理）、Chat（对话） |
| 开源协议 | MIT |
| 参考文档 | [README.md](../README.md)、[architecture.md](./architecture.md)、[event-gateway-design.md](./event-gateway-design.md) |

---

## 二、分镜脚本（完整版）

时间码为参考值，可按实际口播与画面节奏微调。

### 1. 开场（0:00–0:25）

| 时间 | 画面 | 旁白 |
|------|------|------|
| 0:00 | 标题卡「DiOS」+ 副标题「Agent 的操作系统」 | 当 AI Agent 从「聊天玩具」变成团队里真正干活的成员，你会遇到一个新问题：谁来管这些 Agent？谁来接 Git、邮件、Webhook？谁来分配模型和工具？ |
| 0:15 | 同上或淡入三层架构预览 | **DiOS** 就是为此而生的——它是 **DiFlow** 工具链里，面向 Agent 的**事件驱动操作系统层**。 |

**字幕金句（可选）**：Agent 的操作系统

---

### 2. 定位与生态（0:25–1:00）

| 时间 | 画面 | 旁白 |
|------|------|------|
| 0:25 | 三层架构图：App → DiAgent → DiOS | 可以把 DiOS 理解成 Agent 的「操作系统」。它**管资源、管调度，但不替 Agent 做业务决策**。真正执行任务的是跑在上面的 **DiAgent**——每个 Agent 就像系统里的一个进程。 |
| 0:45 | NANA 概念示意（NAS 类比图或文字卡） | 我们的 **NANA** 产品基于 DiOS + DiAgent 构建。**NANA**（Network Attached Native Agent）泛指可通过网络接入的本地化 AI Agent 设备或系统——就像 NAS 之于网络存储，是一类产品形态；**DiOS 是这套形态里的 OS 层**。 |

**字幕金句（可选）**：管资源、管调度，不替 Agent 做业务

---

### 3. 核心能力（1:00–2:10）

| 时间 | 画面 | 旁白 |
|------|------|------|
| 1:00 | Console — Agents 页 | DiOS 做六件关键的事。**第一，Agent 管理**——创建多个 Agent，配置角色、系统提示词、模型，以及 Skills 和 MCP 工具。 |
| 1:15 | Console — Models 页 | **第二，统一模型池**——多个 LLM 端点集中注册，Agent 按需选用。 |
| 1:25 | Console — MCP / Skills 页 | **第三，MCP 与 Skills**——外部工具在 OS 层统一挂载，再分配给 Agent，类似设备驱动和扩展库。 |
| 1:35 | Console — Connectors 页 | **第四，事件接入**——GitHub、GitLab、Gitea Webhook，IMAP 邮件轮询，以及通用 HTTP Webhook。 |
| 1:45 | Console — Events / Subscriptions | **第五，事件订阅与路由**——按来源、类型、字段条件，甚至 Cron，决定哪个事件交给哪个 Agent。 |
| 1:55 | 任务运行日志 / Runs 列表 | **第六，隔离执行**——匹配后在独立容器里启动 Agent，记录状态与日志——可观测、可重试、互不干扰。 |

---

### 4. 两种 Agent 模式（2:10–2:50）

| 时间 | 画面 | 旁白 |
|------|------|------|
| 2:10 | Chat App：选 Agent、多轮对话、流式输出 | **常驻型 service**：长期 HTTP 服务，支持多轮对话。在 **Chat App** 里和它聊天，像打开一个 AI 同事窗口。 |
| 2:30 | Events 触发 → 任务 Run 完成记录 | **一次性 task**：事件触发、干完即走，适合自动化流水线——可并行、可重试。同一种 DiAgent 运行时，区别只在 OS 调度：长驻 worker 还是单次 job。 |

---

### 5. 典型场景（2:50–3:50）

| 时间 | 画面 | 旁白 |
|------|------|------|
| 2:50 | Git 协作序列图动画（Issue → PR → Review） | **Git 驱动的开发协作**：创建 Issue，Webhook 进 DiOS，路由主控 Agent；分派开发 Agent 写代码、提 PR；PR 创建后评审 Agent 介入；Review 再通过事件回到开发 Agent——直到合并，人只在关键节点决策。 |
| 3:20 | 文字卡或简图强调分工 | DiOS **不负责 Agent 之间的业务编排**——那是 Agent 自己的事；DiOS 保证：**外部事件能准确、安全地送到该醒来的 Agent**。 |
| 3:35 | 邮件场景示意（可选，5–8 秒） | 也可走**邮件驱动**：一封「开始写论文」，主控、实验、写作、评审 Agent 通过邮件与共享 workspace 接力。Git 靠 Issue/PR 当状态机，邮件靠 workspace 文件承载进度。 |

**字幕金句（可选）**：事件进来，Agent 醒来

---

### 6. 产品界面（3:50–4:20）

| 时间 | 画面 | 旁白 |
|------|------|------|
| 3:50 | Web UI：Console ↔ Chat 切换 | **Console** 是 OS 管理台；**Chat** 与常驻 Agent 对话，会话由应用层持久化，Agent 保持无状态。 |
| 4:05 | CLI `dios` 终端一闪（可选） | 还可通过 CLI、未来 Slack Bot 等扩展——统一走 `/api/os/*`，OS 不绑定某一种交互形态。 |

---

### 7. 设计哲学与收尾（4:20–5:10）

| 时间 | 画面 | 旁白 |
|------|------|------|
| 4:20 | OS 类比对照表（CPU→LLM、进程→Agent、Shell→App） | 用操作系统类比：LLM 是算力，Skills/MCP 是扩展，Agent 是进程，Chat/Console 是 Shell 和 GUI。内核管调度与资源，进程干业务，用户通过 App 触达——让多 Agent 从 demo 走向可部署、可治理。 |
| 4:50 | 结尾卡：GitHub、MIT、DiFlow | DiOS 基于 DiAgent，属 DiFlow 工具链，MIT 开源。无论你是搭 NANA 设备，还是落地事件驱动的多智能体协作——DiOS 提供的是：**让 Agent 像进程一样被创建、被触发、被隔离、被管理**。欢迎试用、共建。 |

**结尾卡文案建议**：

- 主标题：DiOS
- 副标题：Agent 的操作系统
- 链接：仓库地址 / 在线 Demo（如有）
- 标识：DiFlow · MIT License

---

## 三、精简版口播（约 90 秒）

适用于短视频平台，画面可只保留：标题 → 架构图 → Console 快切 → Chat → Git 场景动画 → 结尾卡。

> **DiOS 是 AI Agent 的操作系统层。**  
> 它接入 Git、邮件、Webhook，按规则把事件路由给对的 Agent，在隔离容器里自动执行。  
> 统一管理模型、MCP 和 Skills；Agent 分常驻对话型和事件触发型。  
> Console 负责治理，Chat 负责对话；真正干活的是 DiAgent。  
> DiOS 管调度与资源，不管业务编排——让多 Agent 协作可部署、可观测。  
> 属于 DiFlow 工具链，支撑 NANA 类产品。欢迎试用。

---

## 四、素材清单

以下为拍摄/剪辑所需素材总表。✅ 表示已在 `docs/video-assets/` 就绪；⏳ 表示仍需人工/录屏。

### 4.0 我能直接搞定 vs 需你配合

| 类别 | 可直接产出 | 需你配合 |
|------|------------|----------|
| 静态字卡 / 架构图 | B-01～B-02、B-04～B-07，A-04 | B-03 官方 DiFlow Logo |
| 示意图 PNG | A-01～A-03（Mermaid 导出） | 序列图动画化（剪辑里 Ken Burns） |
| UI 静帧截图 | S-01～S-06、S-09 | S-07～S-08、S-10～S-13（动效/流式/CLI） |
| 字幕 | AU-03（SRT 草稿） | 按实际口播微调时间轴 |
| 音频 | — | AU-01 旁白、AU-02 BGM |

重新导出示意图：`./scripts/export-video-diagrams.sh`  
重新抓取 UI 截图：`./scripts/capture-video-screenshots.sh`（DiOS 需在 `3001` 可访问）

---

以下为拍摄/剪辑所需素材总表。状态栏可在制作过程中自行勾选。

### 4.1 品牌与静态图形

| ID | 素材名称 | 规格建议 | 来源/制作方式 | 用于分镜 | 状态 |
|----|----------|----------|---------------|----------|------|
| B-01 | DiOS Logo / 主标题字标 | PNG/SVG，透明底，≥1920 宽 | `video-assets/svg/title-dios.svg` | 开场、结尾 | ✅ |
| B-02 | 副标题字卡「Agent 的操作系统」 | 同上 | `video-assets/svg/tagline-agent-os.svg` | 开场、结尾 | ✅ |
| B-03 | DiFlow 标识 | PNG/SVG | 官方或设计 | 结尾、生态段 | ⏳ |
| B-04 | NANA 概念示意（NAS 类比） | 信息图 16:9 | `video-assets/svg/nana-concept.svg` | 定位段 | ✅ |
| B-05 | 三层架构图（App / DiAgent / DiOS） | 16:9，可动画分层 | `video-assets/svg/architecture-layers.svg` | 定位、哲学段 | ✅ |
| B-06 | OS 类比对照表 | 表格图 16:9 | `video-assets/svg/os-analogy-table.svg` | 哲学段 | ✅ |
| B-07 | 结尾信息卡模板 | 16:9 | `video-assets/svg/ending-card.svg` | 收尾 | ✅ |

### 4.2 录屏 — Console App

> 环境：本地或 Demo 部署已启动；建议 1920×1080、60fps 或 30fps；浏览器无多余书签栏。

| ID | 素材名称 | 操作步骤 | 时长建议 | 用于分镜 | 状态 |
|----|----------|----------|----------|----------|------|
| S-01 | Console — Agents 列表与详情 | 打开 Agent 列表 → 点开一个 Agent（含 prompt、model、skills） | 15–25s | 核心能力 §1 | ✅ 静帧 |
| S-02 | Console — Models | 展示 LLM 端点列表与新增/编辑（可只演示 UI） | 10–15s | 核心能力 §2 | ✅ 静帧 |
| S-03 | Console — MCP Servers | MCP 配置列表 | 8–12s | 核心能力 §3 | ✅ 静帧 |
| S-04 | Console — Skills | Skills 列表与绑定 Agent | 8–12s | 核心能力 §3 | ✅ 静帧 |
| S-05 | Console — Connectors | Webhook / IMAP 等事件源配置 | 12–18s | 核心能力 §4 | ✅ 静帧 |
| S-06 | Console — Events / Subscriptions | 事件列表、订阅规则（含 Cron 如有） | 15–20s | 核心能力 §5 | ✅ 静帧 |
| S-07 | Console — Runs / 执行记录 | 一次 task 触发的 run 状态、日志展开 | 15–25s | 核心能力 §6、task 模式 | ☐ |
| S-08 | App Shell 切换 | 顶栏 Console ↔ Chat 切换 | 5–8s | 产品界面 | ☐ 建议录屏 |

### 4.3 录屏 — Chat App

| ID | 素材名称 | 操作步骤 | 时长建议 | 用于分镜 | 状态 |
|----|----------|----------|----------|----------|------|
| S-09 | Chat — Agent 选择与新建会话 | 左侧 Agent 列表 → 选 service Agent | 8–12s | service 模式 | ✅ 静帧 |
| S-10 | Chat — 流式对话 | 发送一条任务型问题，展示 SSE 流式输出 | 20–40s | service 模式 | ☐ 需录屏 |
| S-11 | Chat — 会话历史 | 切换历史会话、消息回显 | 10–15s | 可选 | ☐ |

### 4.4 录屏 — CLI / 部署（可选）

| ID | 素材名称 | 操作步骤 | 时长建议 | 用于分镜 | 状态 |
|----|----------|----------|----------|----------|------|
| S-12 | `dios` CLI | `dios agent list` 或 profile 配置一闪 | 5–10s | 产品界面 | ☐ |
| S-13 | Docker 任务容器 | `docker ps` 见 diagent 任务容器启动/退出 | 10–15s | 隔离执行（可选） | ☐ |

### 4.5 动画与示意图

| ID | 素材名称 | 内容要点 | 规格 | 用于分镜 | 状态 |
|----|----------|----------|------|----------|------|
| A-01 | Git 协作事件流 | Issue → webhook → 主控 → 开发 → PR → 评审 → 修改 → merge | 序列图动画 16:9，15–30s | 典型场景 | ✅ PNG |
| A-02 | Event Gateway 总览 | 外部系统 → Gateway → Agent 路由（简版） | 静图或 10s 动画 | 可选 | ✅ PNG |
| A-03 | 邮件协作流程 | 主控 → 实验 → 写作 → 评审 → 通知 Human | 静图 8–12s | 典型场景（可选） | ✅ PNG |
| A-04 | service vs task 对比 | 左右分屏：Chat 长连接 vs 事件触发单次 Run | 图示 10s | Agent 模式 | ✅ SVG |

> **A-01 可直接参考**：[event-gateway-design.md](./event-gateway-design.md) 中「场景一」Mermaid 序列图，导出为 SVG/视频。

### 4.6 演示数据与环境（前置准备）

| ID | 准备项 | 说明 | 状态 |
|----|--------|------|------|
| P-01 | DiOS 运行环境 | `docker compose` 或 Demo 站点可访问 | ☐ |
| P-02 | 至少 2 个 Agent | 1× `service`（Chat 用）、1× `task`（事件触发用） | ☐ |
| P-03 | 已配置 LLM Model | 录 Chat 时需可用模型与 API Key | ☐ |
| P-04 | 示例 Connector | GitHub/Gitea Webhook 或 Generic Webhook 测试地址 | ☐ |
| P-05 | 预置订阅规则 | task Agent 订阅 `issue.created` 等，便于演示 Runs | ☐ |
| P-06 | 可选：seed 场景脚本 | 参考 `scripts/seed_codedev_scenario.sh` 一键灌数据 | ☐ |
| P-07 | 访问令牌 | 若启用 `DIOS_ACCESS_TOKEN`，录屏前在 UI 登录 | ☐ |

### 4.7 音频与字幕

| ID | 素材名称 | 说明 | 状态 |
|----|----------|------|------|
| AU-01 | 旁白干音 | 按第二节脚本录制，48kHz / WAV 或高质量 AAC | ☐ |
| AU-02 | 背景音乐 | 低调、无歌词，不压过人声 | ☐ |
| AU-03 | 字幕文件 | SRT/VTT，含第二节金句与产品名英文 DiOS | ✅ 草稿 |

### 4.8 版权与合规

| ID | 检查项 | 状态 |
|----|--------|------|
| L-01 | 录屏中无真实 API Key / Token 露出 | ☐ |
| L-02 | 第三方 Logo（GitHub/GitLab 等）符合商标合理使用 | ☐ |
| L-03 | 音乐/字体可商用或已授权 | ☐ |

---

## 五、素材与分镜对照表

| 分镜段落 | 时间码 | 必备素材 | 可选素材 |
|----------|--------|----------|----------|
| 开场 | 0:00–0:25 | B-01, B-02 | B-05 预览 |
| 定位 | 0:25–1:00 | B-05, B-04 | B-03 |
| 核心能力 | 1:00–2:10 | S-01～S-07 | S-04 |
| Agent 模式 | 2:10–2:50 | S-09, S-10, S-07 | A-04, S-11 |
| 典型场景 | 2:50–3:50 | A-01 | A-03, A-02 |
| 产品界面 | 3:50–4:20 | S-08 | S-12 |
| 哲学与收尾 | 4:20–5:10 | B-06, B-07 | B-03, S-13 |

---

## 六、制作检查清单（成片前）

- [ ] 旁白时长与画面时长对齐（±5s）
- [ ] 敏感信息已打码（Token、内网地址、密钥）
- [ ] 产品名统一：**DiOS**（非 Dios / dios 除非指 CLI）
- [ ] 概念表述与 README / architecture 一致
- [ ] 结尾含：一句话定位 + 链接 + 开源协议
- [ ] 字幕校对：NANA、DiAgent、DiFlow、Console、Chat

---

## 七、附录：金句与关键词

| 类型 | 文案 |
|------|------|
| 主 Slogan | Agent 的操作系统 |
| 辅助 | 事件进来，Agent 醒来 |
| 辅助 | 管资源、管调度，不替 Agent 做业务 |
| 生态 | NANA：Network Attached Native Agent |
| 技术关键词 | 事件驱动、容器隔离、MCP、Webhook、CloudEvents |

---

## 八、相关仓库路径（录屏导航）

| 功能 | 前端路由 / 说明 |
|------|-----------------|
| Console | `#/console`（Agents / Models / Connectors / MCP / Events 等子页） |
| Chat | `#/chat` |
| 架构说明 | `docs/architecture.md` |
| 事件场景 | `docs/event-gateway-design.md` |
| 部署 | `deploy/README.md` |
