# E2AG LaTeX 稿件

当前评审稿入口为 `e2ag-paper-v1.1.tex`，正文为 `e2ag-body-v1.1.tex`；`e2ag-paper.tex` 与 `e2ag-body.tex` 保留为上一版来源，不覆盖。构建产物写入 `build/`，不纳入版本控制；可交付 PDF 以版本号和日期另存于仓库根目录的 `output/pdf/`。

最新可交付文件为 `E2AG-paper-anonymous-v1.1.29-20260817.pdf`。v1.1.29 保留 v1.1.26 对 27 条参考文献的“来源--书目--原文证据--正文主张”逐项筛查结果，并按投稿页面要求在中文题名后增加“人工智能操作系统及其安全专刊”标注，同时更新 PDF 元数据。该版共18页，27 个引文键与27条书目一一对应；LaTeX 日志无未解析引用、Overfull 或错误，首页渲染无截断或重叠，SHA-256 为 `2946D175678045645C7D7FE375A4304F5A53AA89B74BC6160889BAF2F8D36F7E`。

非匿名作者信息草稿为 `e2ag-paper-v1.1-authors.tex`，对应预览文件为 `E2AG-paper-authors-v1.1.30-20260817.pdf`，SHA-256 为 `F0F67EBABCA833B1C1164EA43F6A6B254DF8CB11F2B80A27484F1851791AF484`。该稿暂按徐刚、冯骐、姚俊杰、陈铭松、王江涛、饶振宇、肖宇、杨世光、王语欣排列，待合作者确认后再调整；徐刚和王江涛同时标注国家可信嵌入式软件工程技术研究中心与华东师范大学软件工程学院，陈铭松的邮箱为 `mschen@sei.ecnu.edu.cn`、单位为华东师范大学软件工程学院。原五人作者声明已失效，作者顺序确认前不生成签署版声明。保密审查单已由作者准备，全体手写签名与最终上传由作者处理。

为确保中文字体和 Unicode 映射完整嵌入，发布 PDF 使用 MiKTeX XeLaTeX 构建两遍：

```powershell
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.tex
xelatex -interaction=nonstopmode -halt-on-error -output-directory=build e2ag-paper-v1.1.tex
```

主稿显式使用 Windows 自带的 Noto Serif SC/Noto Sans SC 文件，避免依赖查看器本机字体。发布前应使用 `pdffonts` 确认中文字体 `emb=yes, uni=yes`，并用 `pdftotext -enc UTF-8` 验证中文可复制和检索。

v1.1 按《软件学报》论文体例重组为“引言—相关工作—系统模型与问题定义—总体架构—机制—实现—实验—讨论—总结”，并在匿名评审稿中移除作者、单位、基金、致谢及可反向识别的设施标识。中图法分类号暂定为 TP309；解除匿名后再恢复作者、仓库、部署标识和投稿元数据。根据《软件学报》作者指南的常见问题，LaTeX 稿可先提交评审文件，PDF 也可先用于评审，Word 不是当前初投稿阻塞项；录用后再按官方样例转换和复核。
