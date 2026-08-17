# E2AG LaTeX 稿件

当前评审稿入口为 `e2ag-paper-v1.1.tex`，正文为 `e2ag-body-v1.1.tex`；`e2ag-paper.tex` 与 `e2ag-body.tex` 保留为上一版来源，不覆盖。构建产物写入 `build/`，不纳入版本控制；可交付 PDF 以版本号和日期另存于仓库根目录的 `output/pdf/`。

最新可交付文件为 `E2AG-paper-anonymous-v1.1.24-20260817.pdf`。v1.1.24 在 v1.1.23 图形版式基础上完成形式化与全文结构修订：定义事件声明、任务创建和工具调用三个授权层及对象映射，闭合任务作用域能力的签发、使用和终止语义，定义兼顾安全、正常可用性和可问责性的治理目标，并在算法前完整定义输入、策略返回值、辅助过程与局部量。第3节三个架构小节扩展为设计动机、状态所有权、输入输出、性质及实现/实验映射；第7节改为编号化的适用范围、有效性威胁和证据保证范围。该版共17页，LaTeX 日志无未解析引用、Overfull 或错误，SHA-256 为 `4E87C8F9C4313F66C9999226D2424280E7B8D3367BAA4C2D83699EA0414FA74C`。

为确保中文字体和 Unicode 映射完整嵌入，发布 PDF 使用 MiKTeX XeLaTeX 构建两遍：

```powershell
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.tex
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.tex
```

主稿显式使用 Windows 自带的 Noto Serif SC/Noto Sans SC 文件，避免依赖查看器本机字体。发布前应使用 `pdffonts` 确认中文字体 `emb=yes, uni=yes`，并用 `pdftotext -enc UTF-8` 验证中文可复制和检索。

v1.1 按《软件学报》论文体例重组为“引言—相关工作—系统模型与问题定义—总体架构—机制—实现—实验—讨论—总结”，并在匿名评审稿中移除作者、单位、基金、致谢及可反向识别的设施标识。中图法分类号暂定为 TP309；解除匿名后再恢复作者、仓库、部署标识和投稿元数据。根据《软件学报》作者指南的常见问题，LaTeX 稿可先提交评审文件，PDF 也可先用于评审，Word 不是当前初投稿阻塞项；录用后再按官方样例转换和复核。
