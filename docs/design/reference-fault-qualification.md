# 故障场景资格与剩余交付

F03-2.02 将原有十八项故障场景关联到可执行的底座测试、原始注入动作、完整断言和责任任务。R02 真实并发、R03 代理断连、R04 ACP 进程崩溃、R05 节点重启、R07 完整输出抢救、R09 查询与同步故障分离、R10 迟到结果隔离、R11 进度与详情独立同步、R12 指定 UTF-8 分块、R13 运行中升级、R14 Linux 后台环境恢复及 R15 只读控制已通过；其余六项和 G05/G06 仍有缺口。F03-2.02 尚未完成，WBS 总数保持 79 项。

## 执行与结果

在 Linux/WSL 仓库根目录执行，要求已安装后端开发依赖、可用的 Docker 和 `dios-worker-fixture:local` 合成镜像：

```bash
DIOS_TEST_DOCKER=1 backend/.venv/bin/python scripts/qualify_reference_faults.py \
  --output workspace/reference-faults-run
```

ACP 场景还需指定包含当前 `acp_bridge.py` 的 Cursor 运行镜像，夹具逐次校验镜像内模块摘要。按受影响场景执行：

```bash
DIOS_TEST_DOCKER=1 DIOS_TEST_ACP_IMAGE=dios-cursor:acp-exit-20261126 \
  backend/.venv/bin/python scripts/qualify_reference_faults.py \
  --cases R04 R07 --output workspace/reference-acp-faults-run
```

`--cases` 只运行所选场景及结果记账检查；本次报告中的未选场景不计通过。既有证据通过单独的场景记录复用，不重跑无关测试。

输出目录必须不存在。所有数据库、身份策略、模型输出、Git 仓库和容器均由隔离夹具创建；清理仅作用于夹具持有的进程及其 scope 标签容器。输入采用原有 [合成资料](fixtures/reference-scenarios.json)，缺陷与修复文本生成真实 Git commit/blob 身份。业务源码只保存和读取。

`result.json` 保留每项原始动作和断言、测试选择器、结果、耗时、源码摘要、未覆盖条件与责任 Issue。相同选择器只执行一次。测试跳过、预期失败、子测试失败及缺少断言均阻止场景通过。退出码 `0` 表示全部场景及共同治理条件满足，`1` 表示测试或源码一致性失败，`2` 表示组件测试通过但完整资格仍有缺口。

[资格证据](evidence/reference-faults-20261126.json) 记录本批结果；[场景映射](fixtures/reference-faults.json) 是后续补齐断言的入口。原始十八项要求继续以 [参考场景验收](reference-scenario-acceptance.md) 为准。

## ACP 原生崩溃与结果抢救

[R04/R07 证据](evidence/native-acp-faults-20261126.json) 使用打包后的 Cursor 运行镜像、生产 ACP Bridge 和独立 Docker 进程。协议端点提供合成输出，模型请求以实际 `session/prompt` 接收次数计数，付费模型调用为零。

R04 在 `session/prompt` 未结束时向 ACP 进程发出 SIGKILL，后代进程继续持有输出管道。独立进程退出监测释放待决 RPC，清理所属进程组，保留中文部分输出、错误与 Attempt；同一 Worker 随后完成下一项任务。本次从领取到失败落库约 2.46 秒。

R07 在完整输出与退出收据持久化之后、终态提交之前 SIGKILL 独立 Worker。另一 Worker 对账并重新通过 E2AG 结果接收授权后，完成原 Run/Attempt/Session；协议请求计数保持一次。原任务数据和生产评审未参与故障注入。

## 迟到报告与详情同步

[R10/R11 证据](evidence/remote-consistency-20261126.json) 使用独立 Worker 进程、Docker 执行实例和真实 mTLS 网关。R10 将旧 epoch 的报告重复发送十次，并提交原 Attempt 已校验的产物引用；报告按内容去重隔离，原身份、中文输出和来源关系保留，当前 Attempt 的状态、版本及所有权保持不变。另一 Worker 冒用旧绑定会被拒绝。隔离记录使用现有 E2AG 决策账本，不进入成功结果。

R11 在详情接收达到 65,536 字节后注入故障，同任务的小进度仍可更新；重复和倒序序号不会使进度回退。恢复 Worker 从已确认偏移续传，最终产物完成校验，Run、Attempt、模型会话及执行进程保持原关联。模型完整输出到达后先确认 `validating` 阶段，再传输大详情。

API `GET /api/os/runtime/runs/{run_id}/evidence?section=quarantine|transfers`、CLI `evidence --section quarantine|transfers` 和控制台“迟到报告 / 详情同步”使用同一查询。两类记录仅供运营身份读取，应用身份返回受限空页。详情水位包含已接收字节、总量及校验状态；进度水位包含序号和最近有效输出时间。现有表未记录每次分块接收时间，`last_chunk_at` 返回空值。

```bash
DIOS_TEST_DOCKER=1 backend/.venv/bin/python scripts/qualify_reference_faults.py \
  --cases R10 R11 --output workspace/reference-remote-consistency-run
```

预览环境 `13481/13482` 保持 `restore_hold`。前端与后端使用独立项目网络，后端另接存储 Provider 网络，防止共享网络中的同名服务使请求路由至其他实例。浏览器验证包含实际 API、原生测试响应的展示、权限受限、空态、失败刷新恢复和移动布局。

## 模型链路断连与错误分类

[R03 证据](evidence/network-outage-20261126.json) 使用 Toxiproxy 2.13.0、独立 Worker 和 Docker 进程。合成 HTTP 模型流经生产 ACP Bridge 输出首段后，代理实际关闭至少六十秒；每十秒检查原 Attempt、Worker、Session、epoch、执行进程和租约。另一 Worker 先对账再领取，领取结果为空。代理恢复后原任务完成，prompt 计数为一次，部分中文输出及原 E2AG 授权关联保留。

测试采用支持只读流续接的合成 Harness；Cursor 自有网络重连策略需要单独的供应商验证。固定 Toxiproxy 镜像摘要用于故障注入，管理端口只绑定主机回环地址，各次测试创建独立网络并清理自身资源。其 [HTTP API](https://github.com/Shopify/toxiproxy#http-api) 提供 `enabled=false/true` 的连接关闭和恢复操作。

```bash
docker pull ghcr.io/shopify/toxiproxy@sha256:5d3dae09bc185dcc5ff6cf43abf6062a4a720aff0c4306a471e773acdcd107aa
DIOS_TEST_DOCKER=1 DIOS_TEST_ACP_IMAGE=dios-cursor:acp-exit-20261126 \
  backend/.venv/bin/python scripts/qualify_reference_faults.py \
  --cases R03 --output workspace/reference-network-run
```

R06 仍有供应商错误转换缺口。当前打包 Cursor 的 `processPrompt` 会捕获部分上游异常并输出普通文本，随后 `handlePrompt` 返回 `end_turn`；具体镜像与模块摘要记录在 R03 证据中的 `cursor_R06_gap`。代码检查尚未覆盖供应商真实 429/额度响应。DiOS 已有分类退避、单路探测和成功恢复并发，接入这些机制需要可核验的机器可读错误来源；普通模型输出不作为自动重试分类依据。后续采用供应商明确支持的错误通道或经过资格验证的上游修复。

## 固定版本升级与回退

[R13 证据](evidence/unattended-cutover-20261126.json) 使用 Linux 安装器创建独立 Compose 实例，并在旧后端镜像中运行三个受 E2AG 授权的 ACP Worker。三组进程、实例和会话身份独立，实际并发为 3。输出到达后开始排空，已有排队任务保留 Run 身份，新提交明确返回 `RUNTIME_DRAINING`；活跃任务自然完成后校验安装备份。启动失败的候选镜像回退至原版本，随后切换到新的固定镜像，接口仍保持 draining。

新版本通过 API 读取四个任务，核验 Attempt、Session、授权输入、配置摘要和逐个下载产物的摘要。恢复领取后，排队任务沿用原身份执行，一共四次 prompt。后台 Worker 通过 `DIOS_RUNTIME_SCOPE` 领取安装实例的任务；显式参数覆盖与默认 `local` 均保留。此前的[单活跃任务证据](evidence/installation-cutover-20261126.json)保留；三路组合以本次证据为准。

执行此场景时设置 `DIOS_TEST_UPGRADE_OLD`、`DIOS_TEST_UPGRADE_NEW`、`DIOS_TEST_UPGRADE_BAD`、`DIOS_TEST_FRONTEND_IMAGE` 和 `DIOS_TEST_ACP_IMAGE`，再以 `--cases R13` 运行资格入口。`UPGRADE_BAD` 必须为隔离测试的启动失败镜像。原生产部署不参与测试。

版本切换使用 [Docker Compose 既有服务重建机制](https://docs.docker.com/compose/how-tos/production/)，排空和恢复使用 DiOS 原有版本检查。此证据覆盖所列兼容 schema 镜像对；涉及 schema 变化、其他 Harness 与跨主机恢复的资格继续由对应任务承接。

## 查询超时与同步独立性

[R09 证据](evidence/observation-recovery-20261126.json) 使用独立远程 Worker、mTLS 控制面、生产 ACP bridge 和真实 Docker Unix socket 查询。会话持续发送活动事件但不结束 prompt；阻断查询响应使 SDK 触发读取超时，控制台从等待模型转为观测过期，实际并发显示未知。期间租约续期和 E2AG 复核继续，另一 Worker 领取返回 idle。查询恢复后，原会话恢复可观察状态。

随后单独阻断报告接收，小进度保留在 Worker spool，进程观测和授权继续。恢复后按原序号补报，最终同一 Run、Attempt、Session 成功，一共一次 prompt。撤销、策略改变和执行期限回归均保持停止执行；范围错误和明确权限拒绝不会进入观测重试。

执行使用 `--cases R09` 及 `DIOS_TEST_DOCKER=1 DIOS_TEST_ACP_IMAGE=<当前桥接镜像>`。探测可用性与生命周期的区分参考 [Kubernetes 探针](https://kubernetes.io/docs/concepts/workloads/pods/probes/)，有界读取使用 [Docker SDK 的超时配置](https://docker-py.readthedocs.io/en/stable/client.html)。复用现有租约、E2AG、spool 和控制台状态。

## 共同治理资格

六项原共同治理要求以 G01—G06 固定编号，记录在 `reference-faults.json` 的 `governance_cases`。入口支持单独选择：

```bash
DIOS_TEST_DOCKER=1 backend/.venv/bin/python scripts/qualify_reference_faults.py \
  --governance G01 G04 --output workspace/reference-governance-run
```

[共同治理证据](evidence/common-governance-20261126.json) 累计覆盖三项：G01 通过已有应用契约声明可确认取消要求，远端缺失能力时在准入前拒绝；G02 复用源码未变化的 R09 原生观测恢复证据；G04 重放同一真实远程回调十次，穿插十次旧阶段回调，Run 版本、Attempt 序号不重复推进，对应阶段事件仅一条。所有权切换后原有迟到结果隔离和来源审计仍通过。

[G03 撤权证据](evidence/tool-revocation-20261126.json) 使用独立 MCP HTTP 进程与生产 `McpHttpTransport`、`ToolInvocations`、E2AG 授权账本。正向控制先完成一次真实合成文件读取和一次向回环接收端的 HTTP 外发。随后分别在派发前、两个工具均等待 MCP 初始化时撤销运行中授权：已审批调用与新请求均返回 `AUTH_REVOKED`，工具端 `tools/call` 和实际副作用计数不再增长。拒绝记录关联原 Run、Attempt、Session、lease epoch、authorization epoch、授权引用和固定权限版本；已有审批保留历史，未消费为执行权。

此项通过生产 Store API 建立运行状态，验证工具实际控制点；模型请求为零。协议服务只读取合成文件并向本机隔离接收端外发。按 [MCP Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) 区分初始化与 `tools/call`，通过原有派发前重检完成验证，无新增授权机制。

共同治理累计 G01—G04 通过。G05/G06 的 WorkItem、人工决定、复核和业务交接属于上层平台，底座提供稳定任务身份、取消、授权及结果关联。完整资格继续保留这些条件。G03 可通过 `--governance G03` 单独执行。

每次报告分别记录本次结果和未满足条件；未选择的场景不计为本次通过。累计结果引用已有证据并校验其相关源码摘要。全量入口同时选择十八项故障与六项共同治理，退出码 0 要求全部满足。

## 隔离节点真实重启

[R05 证据](evidence/node-reboot-20261126.json)使用既有隔离 Linux VM、生产远程 Worker、真实 mTLS 控制面和 E2AG。持久任务运行时执行 [libvirt 硬重启](https://www.libvirt.org/manpages/virsh.html#reset)，确认内核 `boot_id` 改变。systemd 自动启动两路 Worker，按服务端原有租约到期与对账流程处理任务。

已完成任务的身份、状态和产物保持原记录；执行中的部分输出保留为 `RESULT_INCOMPLETE`，释放执行槽位；已产生完整输出但尚未确认的任务沿原 Attempt/Session 经 E2AG 复核后恢复成功；排队任务沿原 Run 身份在新启动周期执行。四项任务各保留一次尝试。旧 Worker 的心跳和报告重放均返回 `STALE_OWNER`，最终实际并发为零。

运行前准备独立测试 VM、已加载的固定后端和合成执行镜像、当前 `backend/app` 归档、mTLS 测试网络及 SSH 身份，按 [节点配置示例](fixtures/node-reboot.example.json)填写。入口为 `DIOS_TEST_DOCKER=1 DIOS_TEST_REBOOT_CONFIG=<配置文件> --cases R05`。配置中的 `allow_reset` 明确选择测试节点；夹具核对 domain UUID、无运行中容器、HA 服务停止和共享卷未挂载，再执行故障。缺少配置时跳过测试，场景不计通过。

本次全部 248 个运行时 Python 文件与执行节点内容一致，约 66.7 秒内完成租约到期及任务对账，精确耗时见证据。测试服务及所属容器已清理，VM 和隔离网络恢复关闭。单宿主 VM 资格与 D01-3.02/.03 的物理故障域、复制及 RPO/RTO 资格分别记录。

## 只读评审控制

[R15 证据](evidence/readonly-review-20261126.json)覆盖固定 DSH 配置上的四类实际请求：写源码、Python 编译、npm 包脚本和越权外发。沿用 [DSH 原生控制](dsh-native-controls-58.md) 的执行前 guard，四类请求均在工具执行前拒绝。编译及包脚本在同镜像、同非 root 身份的独立正向控制容器中成功，网络关闭；真实 Harness 的副作用目录未生成对应文件，源码摘要保持不变。

写入与外发复用已有真实模型记录；编译与包脚本由一次新的 DSH 会话验证。两组记录分别保留 Run、Attempt、Session、执行进程、E2AG 授权、工具审批、派发和回执绑定。被拒绝的原生操作没有计为成功成果。测试使用合成源码及专用镜像，安装 npm 仅用于包脚本探针。

保留资料可通过 `DIOS_READONLY_EVIDENCE=<编译包脚本资料目录>`、`DIOS_DSH_CONTROL_EVIDENCE=<原生控制资料目录>` 和 `--cases R15` 核验，无需再次调用模型。缺少资料时跳过该测试，R15 保持未通过。资格限于报告中的固定 DSH/SDK/guard 组合；允许的模型服务仍会收到提交内容。

## 底座与应用职责

R14 使用隔离的 systemd user 服务运行生产 ACP bridge。依次移除 Git PATH、移除代理、配置不可达代理，分别得到依赖、代理配置与网络错误；前三次均未创建模型会话。恢复文件配置后，带认证的代理 HEAD 检查和一次合成 ACP 会话完成，journal 中没有注入凭据。测试入口设置 `DIOS_TEST_SYSTEMD=1 DIOS_TEST_DOCKER=1`，选择 `--cases R14`。预检配置与错误分类见 [Linux 后台配置](../../deploy/linux/README.md)。

| 范围 | 场景与剩余条件 | 承接 |
| --- | --- | --- |
| 原生执行故障 | R06 模型 HTTP 错误转换；R09 已通过 | #10/#30/#32/#46/#56/#58 |
| 节点与交付 | R05 隔离节点重启与 R14 Linux 后台环境恢复已通过 | #30/#32/#51/#58；物理节点由 D01-3.02 承接 |
| 静态评审应用 | R01 分支 SHA 水位、R08 证据待验证及覆盖账本、R16 上下文复用、R17 质量与导出统一口径 | #43/#59；底座提供任务、授权、工具、产物与统计接口 |
| 旧系统接续 | R18 旧控制器兼容回滚、分区独占及历史身份 | #11/#60；需隔离源端和旧系统适配 |
| 共同治理 | G01 能力缺失、G02 断连、G03 撤销组合、G04 回调已通过；G05 人工接管复核、G06 实现交接待补齐 | 原验收列出的 #30/#34/#35/#37/#38/#40/#56/#58；平台承接 WorkItem 语义 |

客户业务语义由独立应用承接；DiOS 提供通用能力。原生进程证据与物理跨主机证据分别记录。现有生产评审调度、历史和活跃任务保持运行。

## 实现依据

沿用 Python 标准库的 [unittest fixture、suite 与 result](https://docs.python.org/3/library/unittest.html)，复用现有测试。场景的动作、观测、断言和清理采用 [Chaos Toolkit experiment](https://chaostoolkit.org/reference/api/experiment/) 的组织原则，无新增测试框架运行依赖。

ACP 使用标准 [stdio JSON-RPC 传输](https://agentclientprotocol.com/protocol/v1/transports)。进程退出监测与管道读取分别运行，沿用仓库 `process.py` 的进程组隔离及有界清理方式；[Python subprocess 文档](https://docs.python.org/3/library/asyncio-subprocess.html) 说明了管道读写与退出等待的不同生命周期。仅确认操作系统进程退出后结束待决 RPC；查询超时仍保留原分类。

迟到报告采用 [AWS 死信队列](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html) 的隔离与排查思路；进度与详情沿用 [Azure 队列负载调节](https://learn.microsoft.com/en-us/azure/architecture/patterns/queue-based-load-leveling) 的生产和消费解耦原则，复用现有 SQLite/Provider、离线 spool 和分块协议。预览网络采用 [Docker Compose 自定义网络](https://docs.docker.com/compose/how-tos/networking/) 的服务分区方式。

控制面 HA、旧主隔离及 R05 隔离节点重启已具备验证证据；R06 保留供应商错误来源缺口，应用和旧系统迁移断言按对应责任项推进。
