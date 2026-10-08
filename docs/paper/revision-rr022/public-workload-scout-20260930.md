# GH Archive候选项目盘点与回放方案

2026-09-30。已完成公开数据只读可行性盘点；未fork仓库、未运行下载项目的代码、未触发业务写操作、未调用模型、未改稿件或既有实验结果。工作依据论文修订skill区分业务对象、重复运行及派生变体，不以知名度或条数替代证据。

## 实际数据与结果

先固定七个候选仓库及2026-09-21/28两个周一的UTC 0、6、12、18时段，8文件中命中31条记录。因时间切片稀疏，再预先固定探索性扩展为2026-09-21 UTC全天24文件，保留第一轮结果、不按治理结果择样。两轮时段有重合，不能相加成独立样本；以下只报告完整日v2。

24个gzip文件全部下载并校验，共388,380,797字节（约388MB十进制），逐行读取1,698,954条公开事件；这是读取的数据源规模，不是论文实验样本规模。七个仓库共命中149条去重记录，其中118条属于push、PR、评审/评论或issue相关活动，合并为85个业务对象组。其余31条为星标、fork、分支增删及discussion，未计作本方案业务对象。85组只是可追溯对象数量，不是85个统计独立观测，也不是85个已运行成功的任务。

| 仓库 | 全部命中 | push/PR/issue相关记录 | 对象组 | 判断 |
|---|---:|---:|---:|---|
| microsoft/vscode | 115 | 107 | 78 | 适合开发适配器和工作流样例；含5个push、12条PR事件、36条issue事件及评论/评审 |
| langchain-ai/langgraph | 11 | 6 | 5 | 有PR和评论记录，可继续核查更长窗口，不能仅据一天确认正式规模 |
| fastapi/fastapi | 5 | 4 | 1 | PR及评审/评论集中于同一对象，不能按4个独立案例计算 |
| microsoft/autogen | 2 | 1 | 1 | 一条issue评论，另有discussion；当前规模不足 |
| deepseek-ai/deepseek-harness | 16 | 0 | 0 | 当前窗口只有14次星标和2次fork；保留候选，不据此认定项目不活跃 |
| EleutherAI/lm-evaluation-harness | 0 | 0 | 0 | 当前档案窗口未命中，非项目零活动证明 |
| HQIT/qame | 0 | 0 | 0 | 已知集成参照，当前公开窗口未命中；不作为独立留出 |

GitHub公开仓库API均可读取，当前均未标archived/disabled。许可字段初筛：DeepSeek Harness、LangGraph、lm-evaluation-harness、FastAPI、VS Code返回MIT，AutoGen返回CC-BY-4.0，Qame返回空。这里只记录API字段，不据此认定所有代码、issue正文及评论具有相同许可；发布前仍需逐项检查，不再分发用户正文或身份。

DeepSeek Harness官方仓库已核实为 https://github.com/deepseek-ai/deepseek-harness ，不是笼统的DeepSeek模型仓库。候选另包括 https://github.com/EleutherAI/lm-evaluation-harness ，二者是不同项目。选取多个仓库只增加开发协作工作负载差异，不增加独立Agent OS数量。

## 字段和接口检查

5条push均有head/before，17条PR事件均有head/base SHA，为固定版本对象绑定提供初步材料；尚未请求历史diff、核验对应提交是否仍可读，也尚未证明完整业务任务可重建。

对照现有 `backend/app/connectors/builtin/git_webhook/normalizers.py` 的显式映射，149条中53条的事件种类/动作对已有对应项，96条没有直接显式对应项。后者包含与工作流无关的31条，以及65条业务相关记录；这个数字不是运行拒绝率，更不是65个治理漏洞。正常器还可能产生fallback类型，本次仅检查已声明的显式动作映射。

主要差异包括Events API评审动作created/updated与Webhook的submitted等命名差异，以及PR merged、labeled/assigned等动作。不能把API payload当完整Webhook请求送入。应新增实验侧适配层，逐项说明字段来源、动作转换及不支持项；涉及merged、updated等语义必须核对快照/官方schema，不仅做字符串替换。来源认证使用测试信任边界单列，档案没有原始投递签名。

独立校验器重读24个gzip、核对文件SHA-256与全部事件类型计数，并逐条验证149个选中事件的原始行哈希、id、仓库和类型。选中事件时间均落在对应归档小时；五项离线单元测试验证PR/评论分组、issue、push及星标排除。检查通过不意味着数据源覆盖完整，也不代表已通过治理实验。

## 建议与下一步

当前足以进入开发样例准备，但不足以冻结三项目的大规模留出测试。优先以VS Code的探索对象开发PR/issue/push适配，Qame保留已有集成参照；LangGraph、FastAPI继续作为异项目候选。AutoGen、DeepSeek Harness及lm-evaluation-harness不为凑数量强行纳入；后续按统一规则扩大可用性窗口，若仍无足够合格对象，记录并选择同类候选，不能按治理效果换项目。

正式协议见 [公开项目事件回放协议v0.1](public-workload-protocol-v0.1.md)。其核心是项目级开发/测试隔离，业务对象组去重，历史输入与实验授权分离，正常组/受控变体分表，以及相同上下文的配对基线。本轮已见日期和对象只用于探索；不包装为未见测试样本。最终项目、时间窗、配额、适配动作、工具语义和oracle仍待检查点A冻结。

下一小步是从开发对象提供3条可人工阅读的“公开源记录→归档适配→任务对象→候选调用→独立预期”示例，先检查业务含义；正式批量执行在协议确认后进行。审批/版本失效规则来自实验政策，不声称公开项目历史上采用过同样规则。现阶段无需新增密钥、fork大量仓库或请求原项目维护者接入。

## 文件与复现

- `experiments/e2ag/rr022/public-workload-scout-v1.json`、`public-workload-scout-v2.json`：预先列明候选、时间窗口、资源上限和边界。
- `scout_public_workload.py`：只读API/归档下载和最小字段投影；原始压缩档案仅在被Git忽略的 `tmp/rr022-gharchive-scout-v1/` 缓存。输出无正文和actor身份。
- `test_public_workload_scout.py`：5项测试通过。
- `check_public_workload_scout.py`：独立逐行和哈希复核。
- `results/public-workload-scout-20260930-v1.json`、`results/public-workload-scout-20260930-v2.json`、`results/public-workload-scout-checks-20260930-v2.json`：版本化结果，不覆盖。
- v2结果SHA-256：`5bf84fb6822d99b12f9160562f2678c66e03ee9e8d95766b945e6c094701a750`。

在仓库根目录运行：

```powershell
./backend/.venv/Scripts/python.exe experiments/e2ag/rr022/test_public_workload_scout.py
./backend/.venv/Scripts/python.exe experiments/e2ag/rr022/scout_public_workload.py --manifest experiments/e2ag/rr022/public-workload-scout-v2.json --output experiments/e2ag/rr022/results/public-workload-scout-local-rerun.json
./backend/.venv/Scripts/python.exe experiments/e2ag/rr022/check_public_workload_scout.py experiments/e2ag/rr022/results/public-workload-scout-local-rerun.json --output experiments/e2ag/rr022/results/public-workload-scout-local-checks.json
```

输出已存在时拒绝覆盖；每次复跑改用新名称。远端仓库元数据及时间戳可能变化，复现应比较冻结档案和抽取结果，不要求整份报告字节相同。使用系统代理127.0.0.1:7078，没有认证令牌或模型凭据。

来源：[GH Archive](https://www.gharchive.org/)、[GitHub Events API类型](https://docs.github.com/en/rest/using-the-rest-api/github-event-types)、[Webhook签名验证](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)。官方资料用于字段和来源语义，候选数量来自本地已保存实际下载结果。
