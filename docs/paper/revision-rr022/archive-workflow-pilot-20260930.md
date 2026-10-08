# 历史快照与两步开发工作流小试

2026-09-30。承接[调用层配对](archive-call-pilot-20260930.md)，将三个公开开发对象接入“事件准入→原生任务创建→读取历史内容→写入对象报告”。完成36条配对运行及6条无门控控制，独立检查通过。本轮仅新增实验脚本和报告，未修改产品代码、论文或投稿PDF，也未对公共仓库写入或调用模型。按论文修订skill分别报告输入来源、共享宿主、实际执行和未覆盖项。

## 输入与实际任务

继续使用冻结的VS Code开发对象，不增加独立样本数。push和PR各按归档记录的head SHA获取README，并核验提交SHA、Git blob SHA及本地SHA-256；issue直接使用当时归档的issue对象，不查询当前状态回填。四次只读GitHub API请求均成功，原始响应和内容只保存在忽略目录，结果文件保留来源与哈希。

| 对象 | 公开event id | 固定内容 | 字节数 |
|---|---|---|---:|
| push 44113728747 | 21694813990 | head `fc0256deff8a612de2bdb07b3d748299f15f2b63` 下README | 6728 |
| PR 337123 | 15454802241 | head `a2e8b65859eb1e1d095cf8c28347734c181ac705` 下README | 6728 |
| issue 337178 | 15466644884 | 归档issue对象的规范化JSON | 6016 |

两个README字节完全相同：本轮是3个业务对象、2份不同内容，不能描述为3份不同代码快照。读取README不等于读取完整仓库、PR差异或push变更；issue快照也不等于后续完整讨论。

最小任务分两步：`snapshot.read`读取被授权对象及版本的实际内容；执行器根据工具返回字节计算SHA-256、字节数及行数，再调用`report.write`将这些字段连同对象、版本和来源事件写入隔离JSONL并fsync。因此第二步确实依赖第一步返回值，不是直接复制预期输出。它仍是内容指纹报告，不是代码审查、缺陷分析或自然语言处置质量实验。

任务政策由实验构造：来源/类型准入、允许工具、对象/版本/事件关联及报告字段约束。归档事件另加`requested_action=report.create`、`requested_tool=snapshot.read`及测试环境字段；这些不是原维护者的历史授权，也不是公开事件自带的业务意图。报告字段约束使用冻结内容计算，不能把这一小试外推为可约束任意未知分析结果。

## 三种配置与接入边界

| 配置 | 准入和任务创建 | 工具执行判定 |
|---|---|---|
| E2AG | 原生契约/策略判定和`create_task` | 原生能力签发、参数约束和调用代理 |
| E2AG-with-OPA | 独立Rego判定准入，共享DiOS任务宿主 | 独立Rego读取实际能力/调用上下文，共享签发、代理和审计 |
| DiOS准入宿主＋AgentSpec | **共享原生DiOS准入和任务创建** | 官方DSL/解释器/执行器中的独立业务谓词；不经过E2AG工具门控 |

AgentSpec固定官方提交`e6fa3902e2cfb9681f454b355691b771f70543f8`，复用此前披露的解析器谓词注册、finish兼容和工具名机械映射。其谓词独立检查当前工具的字段集合、类型及授权值；不调用E2AG判定实现。OPA复用已有独立Rego，设置误调用原生PDP即失败的检查。各配置获得相同输入和授权事实，不获得用例标签或预期判定。

AgentSpec配置中的准入拒绝是共享DiOS宿主的结果，不能算作AgentSpec独立实现跨层治理。该配置没有E2AG能力签发或原生工具调用审计，保留规则运行记录；本轮不据此评价其审计优劣。

每次从空任务表启动独立文件型SQLite，通过实际事件分发器调用原生任务创建。`send_message`被替换为只创建任务的固定入口，不启动DiAgent容器；原生任务状态仍为`submitted`。后续两步由确定性执行器完成，故“工作流完成”不等于原生Agent任务生命周期完成。调用代理由测试程序直接进入处理器，下游工具为真实loopback HTTP；不包含外部Web入口、完整MCP握手、模型规划、异步审批或撤销竞态。归档回放不验证原始Webhook签名。

## 逐组结果

每个对象运行四种场景，三种配置配对，共3×4×3=36条运行。下表单元格的分母均为3个开发对象。

| 配置 | 正常读写完成 | 换成另一快照：读取前拒绝 | 换成另一报告目标：读取后写入前拒绝 | 未声明事件类型：不创建任务 |
|---|---:|---:|---:|---:|
| E2AG | 3/3 | 3/3 | 3/3 | 3/3 |
| E2AG-with-OPA | 3/3 | 3/3 | 3/3 | 3/3 |
| DiOS准入宿主＋AgentSpec | 3/3 | 3/3 | 3/3 | 3/3 |

正常场景各产生一次读取及一次真实报告写入；快照错配场景零工具到达；报告目标错配场景读取一次、写入零次；未声明类型场景任务与工具到达均为零。共创建27个任务，事件—任务—trace关联核验一致。未观察到基础设施失败，三种配置结果相同，不支持E2AG效果优势结论。

“另一快照”用现存目录中的另一对象替换`resource+version+source_event_id`整个三元组，是一个逻辑对象替换，不是单字段消融。“另一报告目标”仅改变写入resource。负向请求均为预设治理回归变体，不是自然发生的越权事件；36次运行也不是36个独立业务样本。

另外绕过治理，对三个对象分别读取另一快照、写入另一报告目标，共6条控制全部实际执行成功。说明被拒绝的目标存在、端点本身不会兜底授权。控制还将读取响应替换为固定的不同字节，验证报告哈希和计数随实际响应变化；该依赖检查不是新增真实样本或治理成绩。

## 独立核查与证据

独立结果检查器不导入治理判定实现，重新读取来源、工具账本和36个SQLite数据库，核验缓存内容哈希、Git blob、历史SHA、issue归档行、三方相同输入、任务关联、实际读写次数、报告内容，以及原生审计链的序号/前驱哈希/记录哈希。原生E2AG/OPA调用分别核对实际200/403、能力关联及调用记录；AgentSpec核对官方执行过程触发的谓词次数与判定。全部通过。无门控控制脚本另行验证端点与读写依赖，不替代独立检查器。

| 产物（均在`experiments/e2ag/rr022/results/`） | SHA-256 |
|---|---|
| `archive-snapshots-20260930-v1.json` | `d838d8d42679b1515b5fb2dc892eb42628d135a8a1f64915507e3ecdacd68743` |
| `archive-workflow-pilot-20260930-v1.json` | `0417e4688ea94bb1007d42debd2ab1d9b0f31b5884af7364fcd83324aeb98f4c` |
| `archive-workflow-pilot-checks-20260930-v1.json` | `5c3157531484c09e5148ddc0cf56638ba9ba6ea4f1bf6a3274c52480bdd01588` |
| `archive-workflow-controls-20260930-v1.json` | `e4f5eb18b2c5c9e28e15282cce9fe947e1dc427bab4d44e8d26602ec1177eee8` |

本轮脚本为`fetch_archive_snapshots.py`、`run_archive_workflow_pilot.py`、`check_archive_workflow_pilot.py`、`check_archive_workflow_controls.py`，均在`experiments/e2ag/rr022/`。原始内容缓存位于`tmp/rr022-archive-snapshots-v1/`，执行账本和数据库位于`tmp/rr022-baselines/archive-workflow-*/`及`archive-workflow-control-*/`；这些路径被Git忽略，不随正文或源码自动发布。

本轮重新运行后端E2AG回归57项全部通过（从backend目录运行，202.690秒）；归档适配/oracle 20项、盘点5项测试也全部通过。`git diff --check`通过。测试耗时包含测试夹具等开销，不作为治理性能结果。

从仓库根目录运行如下命令可使用既有快照缓存复跑；需要已有AgentSpec隔离环境及WSL Java/OPA。更换输出名，旧结果禁止覆盖；不下载项目代码或执行其脚本。

```powershell
./backend/.venv/Scripts/python.exe experiments/e2ag/rr022/run_archive_workflow_pilot.py --output experiments/e2ag/rr022/results/archive-workflow-local-rerun.json
./backend/.venv/Scripts/python.exe experiments/e2ag/rr022/check_archive_workflow_pilot.py experiments/e2ag/rr022/results/archive-workflow-local-rerun.json --output experiments/e2ag/rr022/results/archive-workflow-local-checks.json
./backend/.venv/Scripts/python.exe experiments/e2ag/rr022/check_archive_workflow_controls.py experiments/e2ag/rr022/results/archive-workflow-local-rerun.json --output experiments/e2ag/rr022/results/archive-workflow-local-controls.json
```

## 对返修的意义与下一步

相比上一轮，本轮消除了“只预载任务”和“只写元数据、不读实际内容”两项小试缺口，证明这条限定路径可接通。但仍只有单项目开发对象、两个不同内容、确定性指纹任务，没有取得异项目正式留出、原生Agent完成状态或性能证据，不能关闭R1.1/R2.3等外部有效性意见。

下一步应先将开发实现整理为可冻结协议：列出未用于调试的项目与时间窗口、按对象去重及内容重复统计、历史快照缺失排除规则、最小工作流产物和三方共享宿主边界，给出正式矩阵及独立标签复核表。先明确指纹任务能支持的主张，以及是否需要增加一条真正的业务处理任务；不能直接放大相同检查次数来替代业务覆盖。作者确认检查点A后再采集和运行正式留出集，结果冻结后统一修订论文。
