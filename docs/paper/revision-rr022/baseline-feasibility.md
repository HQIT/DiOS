# 外部基线可行性小试报告

日期：2026-09-29。方向：AgentSpec 主学术基线、Agent libOS 紧邻系统补充对照、E2AG-with-OPA 策略引擎替换对照。本报告只回答“原实现能否在隔离环境运行，以及下一步如何公平接入”，不是正式比较结果。

## 已执行与结论

| 对象 | 实际执行 | 观察 | 当前结论 |
|---|---|---|---|
| AgentSpec 原实现 | 固定动作经过作者解析器、谓词、ControlledAgentExecutor 的单步执行路径；工具只写内存列表 | 补齐正确历史后 8/8 检查通过：允许、stop、skip、人工同意/拒绝、触发不匹配及原生历史依赖谓词的允许/拒绝 | 原有执行机制可运行；不是安全率或 E2AG 对比结果 |
| AgentSpec 任务完成 | 允许一次工具调用后，由固定规划器返回 AgentFinish | 原实现任务结束时 input=None，Rule.triggered 调用 input.strip() 报错；独立兼容类增加非 finish/any 规则的空值结束保护后完成，工具仍只调用一次 | 已找到最小接入适配；未修改上游源码，正式实验须披露兼容类 |
| Agent libOS 原生测试子集 | 6 个测试函数，经参数化共 8 项；覆盖资源范围、deny/ask 优先级、一次性审批绑定、过期上下文绑定及撤销权限 | 8 passed，未跳过；作者源码未修改 | 原生能力 API 和选定回归在本机可运行；尚非共同工作负载实验 |
| E2AG-with-OPA | 本轮未实现/运行 | 已明确共享可信状态和执行点、独立 Rego 判定的协议 | 仍待接入，不能报告已验证 |

Agent libOS 的 11.81 秒是 pytest 总运行时间，不是单次授权时延或论文性能结果。两组小试数量恰为 8，不能合计成 16 个独立业务样本，也不能据此比较优劣。

## 原始问题及处理记录

AgentSpec 首轮为 7/8 单步检查通过。未通过项来自本测试夹具遗漏 `is_unsafe_put` 所需要的先前成功拾取观察；这个谓词从动作历史恢复持有对象，不能只提供当前字符串。补充文档化的历史后通过，旧结果保留。该项不是 AgentSpec 安全失败，也不能用于强调 E2AG 优势。

原生任务完成报错则可定位到固定提交的 `src/rule.py` 第 28 行，输入由作者自己的 `Action.from_langchain(AgentFinish)` 构造为 None。兼容类仅在“动作是 finish、参数为 None、该规则并非 finish/any”时返回不触发，其余全部调用原逻辑。8项单步检查在启用/禁用适配时的结果一致。异步审批、持久审批终态与一次性消耗并未由本适配增加，不能把同步 `input(yes/no)` 当作已支持这些性质。

Agent libOS 首轮全部在环境初始化失败，因为本测试的 socket.connect 禁用策略误拦 Windows asyncio 的本地 socketpair。修正为只允许标准库 socketpair 创建过程中的回环连接后，8项通过；普通对外连接仍被阻断。该失败属于测试环境构造错误，不属于被测方法缺陷。

## 环境与可追溯性

- Windows，CPython 3.12.10；两个新虚拟环境均在仓库忽略的 `tmp/rr022-baselines/` 下，未修改 DiOS 原环境或系统 PATH。
- AgentSpec：`e6fa3902e2cfb9681f454b355691b771f70543f8`；ANTLR 4.13.0、LangChain 0.3.25、langchain-core 0.3.81。只安装导入并运行本路径所需依赖，不声称已复现其机器人/自动驾驶等整套环境。
- Agent libOS：`85e8ff94c1e562bfb1f891714e3bf4bed9795690`；按 pyproject 声明范围安装 core 与 pytest 依赖，并未声称按 uv.lock 完整复现。精确解析版本写入结果 JSON。
- pip 安装使用 `https://pypi.tuna.tsinghua.edu.cn/simple`；代码获取使用本机 7078 代理。安装不读取模型 API 密钥。
- 两个作者仓库均保留在本地忽略目录，不纳入论文仓库分发。AgentSpec 代码许可问题仍需在复制、分发或公开适配工件前核清。
- 测试没有模型调用、真实业务操作或远端设施操作。AgentSpec 工具只累加内存记录；Agent libOS 测试在新建本地临时工作区运行。
- AgentSpec 首次导入产生未跟踪 Python 字节码，后续禁用写字节码；上游 Python 源码无 diff。Agent libOS git status 为空。

## 脚本与结果

脚本在仓库 `experiments/e2ag/rr022/`；结果在其 `results/`：

- `agentspec-smoke-20260929-v1.json`：最初缺少历史的夹具结果和完成错误，保留用于解释排错。
- `agentspec-smoke-20260929-v2-native.json`：正确上下文下，8项单步通过，但原生完成探针失败。
- `agentspec-smoke-20260929-v3-compat.json`：同8项单步通过，完成探针通过，明确 `finish_compat_enabled=true`。
- `libos-smoke-20260929-v1.json`：断网保护导致的 Windows 环境初始化失败。
- `libos-smoke-20260929-v2.json`：修正测试网络保护后8项通过；含逐项 nodeid、控制台记录、依赖版本、源码/脚本哈希。

重跑需提供新的输出文件名，工具拒绝覆盖旧结果：

```powershell
& tmp/rr022-baselines/.venv-agentspec/Scripts/python.exe experiments/e2ag/rr022/run_agentspec_smoke.py --upstream tmp/rr022-baselines/AgentSpec --finish-compat --output experiments/e2ag/rr022/results/NEW-agentspec.json
& tmp/rr022-baselines/.venv-libos/Scripts/python.exe experiments/e2ag/rr022/run_libos_smoke.py --upstream tmp/rr022-baselines/Agent-libOS --output experiments/e2ag/rr022/results/NEW-libos.json
```

已提交 v1.1.39 PDF 的 SHA-256 复核仍为 `1A9228C691BF4E89B4B2E2D61EC47C9EFBAD822C113154BB17ACBDE7476B82BE`；本轮未修改正文和旧实验数字。

## 下一步：冻结共同场景，不直接扩大样本

| 比较层次 | 拟比较对象 | 拟控制因素 | 不可直接混计的内容 |
|---|---|---|---|
| 共同动作约束 | E2AG、AgentSpec | 同一任务上下文、实际工具及参数、同一规范；通过原生扩展机制注册规则 | AgentSpec 同步人工审批不自动等于 E2AG 持久审批状态机 |
| 能力生命周期 | E2AG、Agent libOS | 对齐主体/任务、资源、有效性和审批语义；使用各自原生执行机制 | 没有适配的外部事件入口不是一次实测安全失败 |
| 判定可替换性 | E2AG、E2AG-with-OPA | 同输入、同可信状态与事务层、同两个执行点，仅替换判定实现 | 共享 E2AG 设施不构成 OPA 独立实现了整套架构 |

需先解决 AgentSpec 自定义谓词的语法注册与结构化参数传递，保持原解析/执行机制；所有新增规则、接口适配和结束兼容分别记账。共同用例统一给充分且可信的上下文，独立解释预期结果；不再使用缺上下文输入削弱基线。

共同用例明细与协议草案现见 `paired-protocol-v0.1.md`（14项，尚未运行）。兼容适配明细与48组差分边界检查见 `adapter-audit.md`。少量配对轨迹仍待各方业务规则接入，之后才能冻结正式比较协议。本报告不承诺任何方法优于另一方法，也不称已完成 ICSE 论文复现。
