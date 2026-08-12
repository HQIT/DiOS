# E2AG LaTeX 稿件

`e2ag-paper.tex` 是当前主稿入口，`e2ag-body.tex` 为正文。构建产物写入 `build/`，不纳入版本控制；可交付 PDF 另存于仓库根目录的 `output/pdf/`。

为确保中文字体和 Unicode 映射完整嵌入，发布 PDF 使用 MiKTeX XeLaTeX 构建两遍：

```powershell
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper.tex
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper.tex
```

主稿显式使用 Windows 自带的 Noto Serif SC/Noto Sans SC 文件，避免依赖查看器本机字体。发布前应使用 `pdffonts` 确认中文字体 `emb=yes, uni=yes`，并用 `pdftotext -enc UTF-8` 验证中文可复制和检索。

当前稿件按《软件学报》2025 年版排版样例组织中英文题名、摘要、关键词和正文，但作者、单位、基金、中图法分类号等投稿元数据仍需补齐。正式录用后的 Word 排版稿需按编辑部要求另行转换。
