# E2AG LaTeX 稿件

当前评审稿入口为 `e2ag-paper-v1.1.tex`，正文为 `e2ag-body-v1.1.tex`；`e2ag-paper.tex` 与 `e2ag-body.tex` 保留为上一版来源，不覆盖。构建产物写入 `build/`，不纳入版本控制；可交付 PDF 以版本号和日期另存于仓库根目录的 `output/pdf/`。

最新可交付文件为 `E2AG-paper-anonymous-v1.1.2-20260815.pdf`。v1.1.2 是不改变正文和实验数字的配图修订版：图 1 的三个平面等宽对齐并使用连续跨平面箭头，图 2 同步取消整体缩放并统一主路径与证据区域边界。

为确保中文字体和 Unicode 映射完整嵌入，发布 PDF 使用 MiKTeX XeLaTeX 构建两遍：

```powershell
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.tex
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.tex
```

主稿显式使用 Windows 自带的 Noto Serif SC/Noto Sans SC 文件，避免依赖查看器本机字体。发布前应使用 `pdffonts` 确认中文字体 `emb=yes, uni=yes`，并用 `pdftotext -enc UTF-8` 验证中文可复制和检索。

v1.1 按《软件学报》论文体例重组为“引言—相关工作—系统模型与问题定义—总体架构—机制—实现—实验—讨论—总结”，并在匿名评审稿中有意移除作者、单位、基金与致谢。中图法分类号已暂定为 TP309；解除匿名后再恢复作者和投稿元数据。最终 Word 稿应在内容定版后由 LaTeX 转换，并按编辑部样例逐项复核，而不能把自动转换结果直接视作投稿定稿。
