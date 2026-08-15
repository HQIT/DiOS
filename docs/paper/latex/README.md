# E2AG LaTeX 稿件

当前评审稿入口为 `e2ag-paper-v1.1.tex`，正文为 `e2ag-body-v1.1.tex`；`e2ag-paper.tex` 与 `e2ag-body.tex` 保留为上一版来源，不覆盖。构建产物写入 `build/`，不纳入版本控制；可交付 PDF 以版本号和日期另存于仓库根目录的 `output/pdf/`。

最新可交付文件为 `E2AG-paper-anonymous-v1.1.15-20260816.pdf`。v1.1.15 在 v1.1.14 的49例 OPA 可调用切片基础上明确三态指标：正常例仅 `allow` 计为通过，违规例的 `deny` 或 `approval` 均计为“非允许”；同时报告 OPA-Tool 在工具策略层4/15、审批层0/4的分层结果，并把摘要中的“真实持久化路径”收敛为“持久化端到端执行”。27条参考文献均有文内引用；匿名、既有模型回放和自有设施回归边界保持不变。

为确保中文字体和 Unicode 映射完整嵌入，发布 PDF 使用 MiKTeX XeLaTeX 构建两遍：

```powershell
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.tex
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.tex
```

主稿显式使用 Windows 自带的 Noto Serif SC/Noto Sans SC 文件，避免依赖查看器本机字体。发布前应使用 `pdffonts` 确认中文字体 `emb=yes, uni=yes`，并用 `pdftotext -enc UTF-8` 验证中文可复制和检索。

v1.1 按《软件学报》论文体例重组为“引言—相关工作—系统模型与问题定义—总体架构—机制—实现—实验—讨论—总结”，并在匿名评审稿中移除作者、单位、基金、致谢及可反向识别的设施标识。中图法分类号暂定为 TP309；解除匿名后再恢复作者、仓库、部署标识和投稿元数据。根据《软件学报》作者指南的常见问题，LaTeX 稿可先提交评审文件，PDF 也可先用于评审，Word 不是当前初投稿阻塞项；录用后再按官方样例转换和复核。
