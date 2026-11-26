# 故障场景资格与剩余交付

F03-2.02 将原有十八项故障场景关联到可执行的底座测试、原始注入动作、完整断言和责任任务。R02 真实并发、R04 ACP 进程崩溃、R07 完整输出抢救与 R12 指定 UTF-8 分块已通过；其余十四项及六项共同治理场景仍有缺口。F03-2.02 尚未完成，WBS 总数保持 79 项。

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

## 底座与应用职责

| 范围 | 场景与剩余条件 | 承接 |
| --- | --- | --- |
| 原生执行故障 | R03 真实代理断连、R06 模型 HTTP 错误转换、R09 组合长等待、R10 隔离审计、R11 同任务详情与进度分离 | #10/#30/#32/#46/#56/#58 |
| 节点与交付 | R05 隔离节点重启、R13 实际版本切换、R14 Linux 无人值守依赖与代理恢复 | #30/#32/#51/#58；物理节点由 D01-3.02 承接 |
| 静态评审应用 | R01 分支 SHA 水位、R08 证据待验证及覆盖账本、R15 只读业务配置、R16 上下文复用、R17 质量与导出统一口径 | #43/#59；底座提供任务、授权、工具、产物与统计接口 |
| 旧系统接续 | R18 旧控制器兼容回滚、分区独占及历史身份 | #11/#60；需隔离源端和旧系统适配 |
| 共同治理 | 能力缺失、断连、撤销、乱序回调、人工接管复核及实现交接 | 原验收列出的 #30/#34/#35/#37/#38/#40/#56/#58 |

客户业务语义由独立应用承接；DiOS 提供通用能力。原生进程证据与物理跨主机证据分别记录。现有生产评审调度、历史和活跃任务保持运行。

## 实现依据

沿用 Python 标准库的 [unittest fixture、suite 与 result](https://docs.python.org/3/library/unittest.html)，复用现有测试。场景的动作、观测、断言和清理采用 [Chaos Toolkit experiment](https://chaostoolkit.org/reference/api/experiment/) 的组织原则，无新增测试框架运行依赖。

ACP 使用标准 [stdio JSON-RPC 传输](https://agentclientprotocol.com/protocol/v1/transports)。进程退出监测与管道读取分别运行，沿用仓库 `process.py` 的进程组隔离及有界清理方式；[Python subprocess 文档](https://docs.python.org/3/library/asyncio-subprocess.html) 说明了管道读写与退出等待的不同生命周期。仅确认操作系统进程退出后结束待决 RPC；查询超时仍保留原分类。

下一批补齐 R10 旧 epoch 迟到输出的隔离审计，以及 R11 同任务进度与详情上传的独立水位；原生网络、节点和应用业务断言按对应责任项继续推进。
