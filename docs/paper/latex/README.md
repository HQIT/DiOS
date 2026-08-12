# E2AG LaTeX 稿件

`e2ag-paper.tex` 是当前主稿入口，`e2ag-body.tex` 为正文。构建产物写入 `build/`，不纳入版本控制；可交付 PDF 另存于仓库根目录的 `output/pdf/`。

在 Windows PowerShell 中使用已安装的 Tectonic 构建：

```powershell
& 'C:\Users\sugar\.codex\plugins\cache\openai-bundled\latex-tectonic\0.1.1\bin\tectonic.exe' --outdir build --keep-logs e2ag-paper.tex
```

当前稿件按《软件学报》2025 年版排版样例组织中英文题名、摘要、关键词和正文，但作者、单位、基金、中图法分类号等投稿元数据仍需补齐。正式录用后的 Word 排版稿需按编辑部要求另行转换。
