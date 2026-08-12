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
python experiments/e2ag/run_experiment.py --repeats 5000
python experiments/e2ag/run_audit_experiment.py
backend\.venv\Scripts\python.exe experiments/e2ag/run_dispatch_benchmark.py --repeats 1000
backend\.venv\Scripts\python.exe experiments/e2ag/run_tool_gateway_experiment.py --repeats 10000
backend\.venv\Scripts\python.exe experiments/e2ag/run_mutation_experiment.py --per-operator 100
```

五个脚本分别生成安全性/消融结果、审计篡改结果、含 SQLite 审计落库的控制面微基准、运行时工具网关消融结果和固定种子合成变异结果。

## 5. 结果文件

- `experiments/e2ag/results/summary.json`
- `experiments/e2ag/results/case_results.csv`
- `experiments/e2ag/results/audit_summary.json`
- `experiments/e2ag/results/dispatch_benchmark.json`
- `experiments/e2ag/results/tool_gateway_summary.json`
- `experiments/e2ag/results/mutation_summary.json`

论文中的数字必须以这些机器生成文件为准，不能手工选择更优的历史运行结果。
