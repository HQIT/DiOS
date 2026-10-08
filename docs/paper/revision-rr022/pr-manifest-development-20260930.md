# rr022阶段报告：PR变更清单开发验证与主线检查点

2026-09-30。只补完已使用的VS Code PR开发对象，不扩展仓库名单、不接触正式留出日期、不修改论文。结合上一轮[issue受理报告](issue-intake-and-bounded-content-20260930.md)，两条拟用于正式实验的确定性业务路径现已接通。

## 任务和来源

输入是已有归档事件15454802241对应的VS Code PR #337123。固定归档base `cceb427ea1f907448b93a1ea3fdeba8c7f755435`和head `a2e8b65859eb1e1d095cf8c28347734c181ac705`，共同祖先为`58d7d2545f59d6584205ab1f83a8b05e247992c5`。从共同祖先到head计算变更，不将base之后的无关主分支修改算入PR。

本轮以5次只读公开API请求取得提交关系和两份完整递归Git树，共12,724,157响应字节，未使用凭据、重试补采、当前分支替代或远端写入。每份树有23,479条条目且`truncated=false`，投影缓存为7,278,098字节。比较接口只用于取得共同祖先，不使用其最多300个文件的列表作为完整清单；递归树若截断即停止。[GitHub提交比较文档](https://docs.github.com/en/rest/commits/commits#compare-two-commits)、[Git树文档](https://docs.github.com/en/rest/git/trees#get-a-tree)。

工作流为“真实HTTP读取两树快照→固定执行器计算路径级清单→真实HTTP写入隔离报告”。根据路径、Git对象SHA、mode、type判定新增、删除或修改；目录不计作文件，重命名按删除＋新增，子模块变更单列计数，符号链接只记录对象而不跟随或执行。源码及依赖均不执行。读取快照版本是含base/head/共同祖先/两树的整个包的SHA-256，不是仅绑定head，避免同一head对应不同基准。

沿用v2参数约束，报告内容不预设正确答案，身份、对象和快照版本精确绑定。PR报告上限在运行前设为65,536个Unicode码点；正常产物实际为1556个ASCII字符。issue报告保持其2048码点上限，这是两类业务产物预算，不是按方法分别配置；正式协议须冻结这些限制并报告超出预算的正常任务，不能后验删去大PR后宣称全部可用。

## 设计、结果及独立核验

6场景×3配置共18条开发运行，以下实际计数对原生E2AG、完整上下文OPA替换、共享DiOS准入宿主中的AgentSpec均相同。

| 场景 | 创建任务 | 工具读取 | 报告写入 | 内容核验 |
|---|---:|---:|---:|---|
| 正常变更清单 | 1 | 1 | 1 | 正确：4个修改，0新增、0删除 |
| 读取对象不匹配 | 1 | 0 | 0 | 无产物 |
| 读取版本不匹配 | 1 | 0 | 0 | 无产物 |
| 写入对象不匹配 | 1 | 1 | 0 | 无产物 |
| 权限合法、内容错误 | 1 | 1 | 1 | 内容检查判错，符合控制预期 |
| 未声明事件类型 | 0 | 0 | 0 | 无产物 |

四个实际修改路径为：

```text
src/vs/workbench/contrib/chat/browser/aiCustomization/mcpListWidget.ts
src/vs/workbench/contrib/chat/browser/aiCustomization/media/aiCustomizationManagement.css
src/vs/workbench/contrib/chat/test/browser/aiCustomization/mcpListWidget.test.ts
src/vs/workbench/test/browser/componentFixtures/sessions/aiCustomizationManagementEditor.fixture.ts
```

检查器不导入清单生成函数或治理谓词。它重新核对归档原行、base/head、API响应hash、树投影和快照hash；从扁平条目重建Git树对象并核对8,908次树哈希（两份快照合计，不能当作独立样本数），再以独立SQLite连接查询计算差异清单，与实际报告逐字段比较。18份实验数据库、15条任务关联、实际HTTP落盘记录和审计链均通过，三配置输入一致。

另外3项无门控HTTP控制分别证明错读取对象、错读取版本及错写入对象能到达同一工具并执行，因而拒绝不是工具端对象不存在造成的。它们使用人工别名映射同一真实快照，不是自然存在的第二业务对象。1项合成返回值控制把after树换成before树，生成结果随之变为空清单，证明生成函数使用实际读取内容；该项只是依赖检查，不能称为真实治理运行或新来源。新增9项清单生成单元测试通过，覆盖零变更、增删改、mode变化、重命名、符号链接/子模块、目录排除、重复/无效条目、截断与Unicode路径。

本轮没有修改后端或上一轮issue驱动，PR驱动仅显式替换业务报告生成函数及子进程入口，复用已经核验的准入、任务、三方授权和HTTP夹具。源文件hash检查通过；上一轮67项后端回归是前一轮结果，不冒充本轮重新执行。

## 结论边界

两种任务分别处理归档issue文本字段与PR历史Git树，已不再只是README指纹登记；正常产物由实际输入计算，独立检查能发现权限合法但内容错误的报告。这个结果支持正式实验的实施可行性，不证明E2AG比AgentSpec或OPA更优；三配置在共同规则上的结果仍然一致。

本轮只有1个已用于开发的PR；与上一轮issue合起来是同一开发仓库的2个对象，不是45个独立样本，也不是跨项目留出。固定执行器不是模型自主决策，原生任务依然`submitted`；AgentSpec共享DiOS准入，OPA是共享宿主中的策略引擎替换。代理入口使用本地处理函数，下游是真实loopback HTTP；未测公网部署、原生完整终态或性能。不以Git树条目多、测试条数多替代独立业务或系统规模。

## 主线检查点和下一步

| 返修工作包 | 当前状态 | 还差什么 |
|---|---|---|
| E1 公平比较 | AgentSpec及完整上下文OPA开发路径已接通，共同场景结果一致 | 在固定独立工作负载上正式配对、披露适配和不支持项；不制造优势 |
| E2 独立业务与系统覆盖 | issue受理和PR清单两条开发路径通过 | 最小正式范围、未见对象、独立标签复核；其他入口/环境覆盖仍未完成 |
| E3 完整路径性能 | 本轮未推进 | 固定计时边界，包含准入、任务、签发、持久化、调用和上游；分开报告业务计算与治理增量 |
| E4 细粒度权限 | 资源/版本、参数类型/范围/长度及快照约束已开发验证 | 正式共享场景、性能代价及方法边界统一纳入论文 |

下一步不再追加同类开发小试。只在已有候选材料内收敛最小正式范围和排除清单，一次性提交检查点A供作者确认；样本采集、复核、配对按冻结协议执行。项目数不等于独立系统数，v0.2五项目方案仍暂停，不以本报告暗示批准旧范围。并行准备完整路径性能的计时边界和负载协议，不再让项目选型占满返修周期。结果冻结后统一更新论文、逐条回复与相对v1.1.39的修改痕迹稿。

## 复现和证据

从仓库根运行；所有输出使用新文件名，保留旧轨迹。准备步骤需7078代理及既有Python、OPA/WSL和AgentSpec环境。

```powershell
./backend/.venv/Scripts/python.exe -m unittest discover -s experiments/e2ag/rr022 -p test_pr_manifest_workload.py
./backend/.venv/Scripts/python.exe experiments/e2ag/rr022/fetch_pr_manifest_snapshot.py --output experiments/e2ag/rr022/results/pr-manifest-snapshot-NEW.json
./backend/.venv/Scripts/python.exe experiments/e2ag/rr022/run_pr_manifest_pilot.py --snapshot experiments/e2ag/rr022/results/pr-manifest-snapshot-NEW.json --output experiments/e2ag/rr022/results/pr-manifest-pilot-NEW.json
./backend/.venv/Scripts/python.exe experiments/e2ag/rr022/check_pr_manifest_pilot.py experiments/e2ag/rr022/results/pr-manifest-pilot-NEW.json --output experiments/e2ag/rr022/results/pr-manifest-checks-NEW.json
./backend/.venv/Scripts/python.exe experiments/e2ag/rr022/check_pr_manifest_controls.py experiments/e2ag/rr022/results/pr-manifest-pilot-NEW.json --output experiments/e2ag/rr022/results/pr-manifest-controls-NEW.json
```

`experiments/e2ag/rr022/results/`内本轮文件与SHA-256：

| 文件 | SHA-256 |
|---|---|
| `pr-manifest-snapshot-20260930-v1.json` | `65bdd8a37a4daa17b1e69cc2f864dc570c8970281abc8c47ac33c856aa2d2956` |
| `pr-manifest-pilot-20260930-v1.json` | `ac248c180e3eec426a3bfc33fc6d2bbb2107db3cef6466eeff81feb784d187c7` |
| `pr-manifest-checks-20260930-v1.json` | `f3ac7b64ee2164b588c7c0847249c9090d77e76932c8bd886352d91ef21b399d` |
| `pr-manifest-controls-20260930-v1.json` | `4e129cdce91e16c9736c0ec4cc42dd30228656ff5972d9a44900d192ef0da221` |

缓存位于独立`tmp/pr-manifest-*`，没有发布缓存或向外部项目写入。主运行保留待独立核验的原始状态，检查结果另存，不改写原轨迹。
