# E2AG LaTeX 稿件

当前评审稿入口为 `e2ag-paper-v1.1.tex`，正文为 `e2ag-body-v1.1.tex`；`e2ag-paper.tex` 与 `e2ag-body.tex` 保留为上一版来源，不覆盖。构建产物写入 `build/`，不纳入版本控制；可交付 PDF 以版本号和日期另存于仓库根目录的 `output/pdf/`。

最新可交付文件为 `E2AG-paper-anonymous-v1.1.26-20260817.pdf`。v1.1.26 完成 27 条参考文献的“来源--书目--原文证据--正文主张”逐项筛查：将 AIOS、间接提示注入原始论文和 CaMeL 更新为正式会议版本，补全 Agent survey 卷期，将 A2A 固定为 v1.0.0，并确认 AgentSpec 的 ICSE 2026、DOI 与 12 页信息。相关工作中三篇中文综述改为逐来源直接陈述，协议、能力与证据链文献的适用范围也作了收缩，避免把规范对象、时间戳链或供应链证明扩张为端到端执行授权。该版共18页，27 个引文键与27条书目一一对应；LaTeX 日志无未解析引用、Overfull 或错误，中文字体完整嵌入且具有 Unicode 映射，SHA-256 为 `EA75D005849F8409C1DE7F1829C840FBAA3531BA7F1423FBE254442A34550246`。

为确保中文字体和 Unicode 映射完整嵌入，发布 PDF 使用 MiKTeX XeLaTeX 构建两遍：

```powershell
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.tex
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.tex
```

主稿显式使用 Windows 自带的 Noto Serif SC/Noto Sans SC 文件，避免依赖查看器本机字体。发布前应使用 `pdffonts` 确认中文字体 `emb=yes, uni=yes`，并用 `pdftotext -enc UTF-8` 验证中文可复制和检索。

v1.1 按《软件学报》论文体例重组为“引言—相关工作—系统模型与问题定义—总体架构—机制—实现—实验—讨论—总结”，并在匿名评审稿中移除作者、单位、基金、致谢及可反向识别的设施标识。中图法分类号暂定为 TP309；解除匿名后再恢复作者、仓库、部署标识和投稿元数据。根据《软件学报》作者指南的常见问题，LaTeX 稿可先提交评审文件，PDF 也可先用于评审，Word 不是当前初投稿阻塞项；录用后再按官方样例转换和复核。
