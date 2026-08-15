# E2AG 60 例冻结威胁矩阵独立复核说明

## 复核目的

本复核用于降低作者构造威胁矩阵的标签偏差。复核人应未参与 60 例语料构造，并在查看作者标签映射前独立完成盲表。

## 交付给复核人的文件

仅发送：`experiments/e2ag/review/frozen_review_blind.csv`。

不要发送：`frozen_review_author_map.csv`，也不要让复核人先查看 `frozen_cases.jsonl` 中的 `class`、`layer` 和 `scenario` 字段。

## 填写字段

- `reviewer_class`：只能填写 `benign` 或 `attack`；
- `reviewer_governance_layer`：只能填写 `benign`、`contract`、`policy` 或 `approval`；
- `reviewer_expected_enforce_decision`：只能填写 `allow`、`deny` 或 `approval_required`；
- `reviewer_confidence_1_5`：填写 1–5；
- `reviewer_notes`：说明不确定点或判定依据；
- `reviewer_name`：60 行使用同一复核人姓名；
- `reviewed_at`：使用 `YYYY-MM-DD`。

判定时依据表中的 CloudEvent 字段和 `target_governance_json`，独立判断 source–type 声明权、目标 Agent 对 source/type/action/tool 的允许范围，以及生产敏感动作是否应进入人工审批。不评价自然语言提示词是否“恶意”，只评价结构化请求在 E2AG 规则下应得到的治理结果。

## 汇总与分歧处理

复核人返回填写完成的盲表后运行；每份返回表使用一个 `--review` 参数，公开归档使用匿名输出：

```powershell
backend\.venv\Scripts\python.exe experiments\e2ag\summarize_independent_review.py `
  --review <review-a.csv> `
  --review <review-b.csv> `
  --review <review-c.csv> `
  --anonymized-dir experiments\e2ag\review\returned
```

脚本校验60例 ID、冻结输入字段、完整性和枚举值，自动识别 UTF-8/GB18030 编码，输出逐人作者一致率、两两 Cohen's kappa、多评审者 Fleiss kappa、原始文件 SHA-256 和分歧表。匿名返回表只保留 Reviewer A/B/C。所有分歧必须记录最终标签、修改原因、处理人和日期。若冻结集发生修改，应重新计算语料 SHA-256、重跑 RQ1，并同步正文、Markdown、LaTeX 和 PDF。
