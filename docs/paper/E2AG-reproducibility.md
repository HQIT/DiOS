# E2AG 复现实验说明

## 1. 环境

- Windows PowerShell
- Python 3.12
- DiOS 基线：`feat/event-subscription-governance-20260418@72732a4`
- E2AG 研究分支：`codex/e2ag-research`

## 2. 安装依赖

在中国大陆网络环境中显式使用加速镜像，不修改用户全局配置：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install `
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple `
  -r requirements.txt
.\.venv\Scripts\python.exe -m pip install `
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple `
  pytest
```

如需安装或验证前端依赖：

```powershell
cd frontend
npm install --registry=https://registry.npmmirror.com
npm run build
```

## 3. 自动测试

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 或使用 pytest
.\.venv\Scripts\python.exe -m pytest -q
```

## 4. 实验

从仓库根目录执行：

```powershell
backend\.venv\Scripts\python.exe experiments/e2ag/run_frozen_ablation.py
backend\.venv\Scripts\python.exe experiments/e2ag/run_e2e_chain_experiment.py --repeats 30
backend\.venv\Scripts\python.exe experiments/e2ag/run_causal_audit_experiment.py --repeats 20
backend\.venv\Scripts\python.exe experiments/e2ag/run_concurrency_experiment.py --levels 8,32 --rounds 100
backend\.venv\Scripts\python.exe experiments/e2ag/summarize_independent_review.py --review <review-a.csv> --review <review-b.csv> --review <review-c.csv> --anonymized-dir experiments/e2ag/review/returned
python experiments/e2ag/run_experiment.py --repeats 5000
python experiments/e2ag/run_audit_experiment.py
backend\.venv\Scripts\python.exe experiments/e2ag/run_dispatch_benchmark.py --repeats 1000
backend\.venv\Scripts\python.exe experiments/e2ag/run_tool_gateway_experiment.py --repeats 10000
backend\.venv\Scripts\python.exe experiments/e2ag/run_mutation_experiment.py --per-operator 100
backend\.venv\Scripts\python.exe experiments/e2ag/run_http_benchmark.py --repeats 300
```

前四个脚本是当前正文主实验，分别生成冻结集 2×2 消融、480 次持久化端到端链、因果阶段定位和 SQLite 并发健壮性结果。后六个脚本保留为开发集、篡改、微基准和补充机制覆盖检查。

外部工具边界基线使用官方 OPA v1.17.0。下载对应平台的官方发布件并核验发布页 SHA-256；本轮 Linux amd64 static 发布件的摘要为 `e83da46804832578e9d9e1733dffbe4d3b5f8cc9c26eb124da9ceea4abfe189f`。在 Linux/WSL 中执行：

```bash
python3 experiments/e2ag/run_opa_tool_baseline.py \
  --opa-bin /path/to/opa_linux_amd64_static \
  --expected-opa-sha256 e83da46804832578e9d9e1733dffbe4d3b5f8cc9c26eb124da9ceea4abfe189f
```

该脚本只评估冻结集中的8个 `tools/call` 请求；另2个非工具调用协议方法明确排除。它输出功能判定和版本/哈希，不采集本地进程启动时延。

## 5. 结果文件

- `experiments/e2ag/results/summary.json`
- `experiments/e2ag/results/case_results.csv`
- `experiments/e2ag/results/audit_summary.json`
- `experiments/e2ag/results/dispatch_benchmark.json`
- `experiments/e2ag/results/tool_gateway_summary.json`
- `experiments/e2ag/results/mutation_summary.json`
- `experiments/e2ag/results/http_benchmark.json`
- `experiments/e2ag/results/frozen_ablation_summary.json`
- `experiments/e2ag/results/frozen_ablation_cases.csv`
- `experiments/e2ag/results/e2e_chain_summary.json`
- `experiments/e2ag/results/e2e_chain_runs.csv`
- `experiments/e2ag/results/causal_audit_summary.json`
- `experiments/e2ag/results/concurrency_summary_before_fix.json`
- `experiments/e2ag/results/concurrency_summary.json`
- `experiments/e2ag/results/external_qame_demogo_summary.json`
- `experiments/e2ag/results/external_governance_regression_summary.json`
- `experiments/e2ag/results/opa_tool_baseline_summary.json`
- `experiments/e2ag/review/independent_review_panel_summary.json`
- `experiments/e2ag/review/independent_review_disagreements.csv`
- `experiments/e2ag/review/returned/reviewer-{a,b,c}.csv`

论文中的数字必须以这些机器生成文件为准，不能手工选择更优的历史运行结果。

自有设施治理一致性回归使用 `deploy/e2ag-experiment/docker-compose.demogo.yml`
在 demogo 部署隔离后端，并将 `HQIT/qame` 的真实 GitHub push payload 以原
Connector 密钥签名后送入回环入口。允许路径用于确认真实 Connector、DiAgent、
PEP 与 `git-perf` 闭合；负对照的工具名和参数由
`external_governance_regression.py` 固定，不由模型生成或选择。后续复验只能由
确定性 harness 调用，不要求模型推进负向步骤。结构化摘要不包含访问令牌或
webhook 密钥。

## 6. LaTeX 论文构建

论文主源位于 `docs/paper/latex/`。使用 Codex 打包的 Tectonic 编译：

```powershell
cd docs/paper/latex
C:\Users\sugar\.codex\plugins\cache\openai-bundled\latex-tectonic\0.1.1\bin\tectonic.exe `
  --outdir build e2ag-paper.tex
```

生成文件为 `docs/paper/latex/build/e2ag-paper.pdf`。`e2ag-paper.tex` 管理题名、作者、摘要与版式，`e2ag-body.tex` 为正文；不要重新运行 Pandoc 覆盖已经人工整理的正文文件。
