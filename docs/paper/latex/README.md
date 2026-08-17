# E2AG LaTeX 稿件

当前作者投稿稿入口为 `e2ag-paper-v1.1.33-authors.tex`，首页信息为 `e2ag-frontmatter-authors-v1.1.33.tex`，正文沿用 `e2ag-body-v1.1.tex`，公共排版规则集中在 `jos2025-submission.sty`。该样式以官网“软件学报排版样例2025年版”为视觉参照，采用约 184 mm × 260 mm 版心、9 pt 正文字号、12 pt 行距、左对齐分级标题和样例化的中英文首页信息块；DOI、卷期、收稿日期和正式页码等编辑部字段不在初投稿稿中伪造。

最新可交付文件为 `E2AG-paper-authors-v1.1.33-20260817.pdf`，共15页，SHA-256 为 `309AA9A3D07502E758F07D82664B7CFCC914073AF8E70D17288342EFD0DDA5F2`。该版将中英文通讯作者标记、通讯作者姓名和邮箱统一更新为冯骐，保留27条已逐项筛查的参考文献、既有实验数字及 v1.1.32 的排版。三遍 XeLaTeX 构建后无未解析引用、Overfull 或 LaTeX 警告；全部字体均已嵌入并带 Unicode 映射，中文可复制检索，首页检查未发现换行、越界或重叠。

可编辑协作备份为 `output/docx/E2AG-paper-authors-v1.1.33-20260817.docx`，SHA-256 为 `7B2994C4B3CCDEFD8C2997A73F3D5E98E408DEEECEBA94A4DBAF5D9420B7239F`。该文件由已核验 PDF 通过 Microsoft Word 转换生成；重新用 Word 导出后为19页，逐页缩略检查并抽查首页、架构图、流程图、算法及参考文献页，未发现文字乱码、越界或图表缺失。DOCX 仅用于合作者编辑，投稿版仍以15页 PDF 为准。

最终作者顺序为徐刚、冯骐、姚俊杰、肖宇、陈铭松、王江涛、杨世光、饶振宇、王语欣；徐刚和王江涛同时标注国家可信嵌入式软件工程技术研究中心与华东师范大学软件工程学院。英文作者名按样例采用“姓全大写在前”的形式。投稿声明使用作者自有模板，保密审查单已由作者准备，全体手写签名与最终上传均由作者处理。匿名稿停止维护，但保留既有文件作为历史产物。

日常只检查首页排版时，可编译轻量入口：

```powershell
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-frontmatter-preview-v1.1.33.tex
```

阶段版本或投稿前再对作者稿完整构建两遍：

```powershell
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.33-authors.tex
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.33-authors.tex
```

主稿显式使用 Windows 自带的 Noto Serif SC/Noto Sans SC 文件，避免依赖查看器本机字体。发布前应使用 `pdffonts` 确认中文字体 `emb=yes, uni=yes`，并用 `pdftotext -enc UTF-8` 验证中文可复制和检索。

v1.1 正文按《软件学报》论文体例重组为“引言—相关工作—系统模型与问题定义—总体架构—机制—实现—实验—讨论—总结”。当前作者稿已经恢复作者、单位和投稿元数据，中图法分类号暂定为 TP309；尚未确认的基金、项目和编辑部字段不写入。当前投稿系统只接收 PDF，因此 Word 不是初投稿阻塞项；录用后再按编辑部要求转换和复核。
