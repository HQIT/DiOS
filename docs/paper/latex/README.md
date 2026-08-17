# E2AG LaTeX 稿件

当前评审稿入口为 `e2ag-paper-v1.1.tex`，正文为 `e2ag-body-v1.1.tex`；`e2ag-paper.tex` 与 `e2ag-body.tex` 保留为上一版来源，不覆盖。构建产物写入 `build/`，不纳入版本控制；可交付 PDF 以版本号和日期另存于仓库根目录的 `output/pdf/`。

最新可交付文件为 `E2AG-paper-anonymous-v1.1.25-20260817.pdf`。v1.1.25 在 v1.1.24 形式化与结构修订基础上完成全文衔接审计：相关工作按“对象定义--授权执行--证据关联--综合比较”推进，授权层与部署信任边界明确为正交关系，算法、复杂度、并发实现和实验结果均增加了说明下一论证步骤必要性的语义桥。RQ2 将独立策略引擎基线与“双执行点的确定性和模型决策配对执行”分设小节；引言 motivating example 的两条反事实分支分别在来源契约和自有设施回归中显式回指。该版共18页，LaTeX 日志无未解析引用、Overfull 或错误，SHA-256 为 `B768E14588296DA3541061E5461AF72D214E0364CB42C1A94A15753ACFA5DC49`。

为确保中文字体和 Unicode 映射完整嵌入，发布 PDF 使用 MiKTeX XeLaTeX 构建两遍：

```powershell
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.tex
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.tex
```

主稿显式使用 Windows 自带的 Noto Serif SC/Noto Sans SC 文件，避免依赖查看器本机字体。发布前应使用 `pdffonts` 确认中文字体 `emb=yes, uni=yes`，并用 `pdftotext -enc UTF-8` 验证中文可复制和检索。

v1.1 按《软件学报》论文体例重组为“引言—相关工作—系统模型与问题定义—总体架构—机制—实现—实验—讨论—总结”，并在匿名评审稿中移除作者、单位、基金、致谢及可反向识别的设施标识。中图法分类号暂定为 TP309；解除匿名后再恢复作者、仓库、部署标识和投稿元数据。根据《软件学报》作者指南的常见问题，LaTeX 稿可先提交评审文件，PDF 也可先用于评审，Word 不是当前初投稿阻塞项；录用后再按官方样例转换和复核。
