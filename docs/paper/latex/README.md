# E2AG LaTeX 稿件

当前作者投稿稿入口为 `e2ag-paper-v1.1.37-authors.tex`，首页信息复用未改动的 `e2ag-frontmatter-authors-v1.1.36.tex`，正文入口为 `e2ag-body-v1.1.37.tex`，公共排版规则集中在 `jos2025-submission.sty`。该样式以官网“软件学报排版样例2025年版”为视觉参照，采用约 184 mm × 260 mm 版心、9 pt 正文字号、12 pt 行距、左对齐分级标题和样例化的中英文首页信息块；DOI、卷期、收稿日期和正式页码等编辑部字段不在初投稿稿中伪造。

最新可交付文件为 `E2AG-paper-authors-v1.1.37-20260817.pdf`，共15页，SHA-256 为 `F372A8C45FEF536E1ABE6E2A2BE76493BF191C8B673BCC5EA29F6A46F874CED2`。该版将引言中并列谓语分句之间的“原生能力、为模型”校正为“原生能力，为模型”，并同步 Markdown；v1.1.36 的“副作用”术语审计结论保持不变。三遍 XeLaTeX 构建后无未解析引用或 Overfull；PDF 文本确认旧标点为0处、新标点为1处，全部字体均已嵌入并带 Unicode 映射，第2页复核未见乱码、越界、重叠或异常换行。

v1.1.33 的 DOCX 仅作为历史协作备份保留，不再同步维护。后续以 LaTeX 为唯一可维护源文件，以版本化 PDF 为投稿产物，不再进行 PDF 到 DOCX 的反向转换。

最终作者顺序为徐刚、冯骐、姚俊杰、肖宇、陈铭松、王江涛、杨世光、饶振宇、王语欣；徐刚和王江涛同时标注国家可信嵌入式软件工程技术研究中心与华东师范大学软件工程学院。英文作者名按样例采用“姓全大写在前”的形式。投稿声明使用作者自有模板，保密审查单已由作者准备，全体手写签名与最终上传均由作者处理。匿名稿停止维护，但保留既有文件作为历史产物。

日常只检查首页排版时，可编译轻量入口：

```powershell
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-frontmatter-preview-v1.1.36.tex
```

阶段版本或投稿前再对作者稿完整构建两遍：

```powershell
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.37-authors.tex
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.37-authors.tex
```

主稿显式使用 Windows 自带的 Noto Serif SC/Noto Sans SC 文件，避免依赖查看器本机字体。发布前应使用 `pdffonts` 确认中文字体 `emb=yes, uni=yes`，并用 `pdftotext -enc UTF-8` 验证中文可复制和检索。

v1.1 正文按《软件学报》论文体例重组为“引言—相关工作—系统模型与问题定义—总体架构—机制—实现—实验—讨论—总结”。当前作者稿已经恢复作者、单位和投稿元数据，中图法分类号采用 TP311；尚未确认的基金、项目和编辑部字段不写入。当前投稿系统只接收 PDF，因此 Word 不是初投稿阻塞项；录用后再按编辑部要求转换和复核。
