# rr022 仓库归档记录（2026-10-08）

本次归档承接已提交稿v1.1.39（`2bfc5ce`），提交至`codex/e2ag-research`，不合并`main`，不修改外层视频仓库或DiAgent子模块指针。稿件交付版本为v1.1.48；本记录补充各历史版本记录中的Git状态，不改变实验结果。

## 提交范围

按实现与测试、实验及复现材料、论文交付三组提交。保留历次修订的源文件和实验记录，最终PDF仅新增v1.1.48的清洁稿、累计修改稿、逐条答复及合并稿。原有已跟踪PDF不删除。没有执行新实验、服务部署或数据库变更。

## 原始证据与本地文件

正式测量分析JSON、分轮统计、实验脚本、必要的历史业务结果和异构证据随仓库提交。逐请求原始文件、运行日志、过往预览PDF及显示检查产物共731个文件（2264963501字节）保留在原位置，不上传Git，亦未删除。其中逐请求原始证据480个，构建及运行日志180个，中间输出43个，显示检查产物28个。

[本地文件清单](local-artifact-manifest-20261008.json)逐项记录相对于仓库根目录的路径、大小和SHA-256；对应排除规则在根目录`.gitignore`。目前未建立这些原始文件的远端数据归档。新克隆可以读取统计结果和重生成论文表格；若需从逐请求数据重新计算或执行依赖这些文件的完整审计，须向作者取得清单中的相应文件，按原相对路径恢复并核对哈希。清单不替代原始证据，也不代表原始数据已随Git上传。复现外部基线还需按已有协议准备其原实现和依赖。

## 提交前检查

在当前工作环境重跑下列回归，均通过，共236项：

| 检查 | 工作目录及命令 | 通过数 |
|---|---|---:|
| 后端治理 | `backend`：`.venv/Scripts/python.exe -m unittest discover -s tests -p "test_e2ag*.py" -q` | 120 |
| 实验及统计脚本 | 仓库根目录：`backend/.venv/Scripts/python.exe -m unittest discover -s experiments/e2ag/rr022 -p "test_*.py" -q` | 90 |
| 异构适配与证据检查 | `backend`：`.venv/Scripts/python.exe -m unittest discover -s tests -p "test_heterogeneity*.py" -q` | 8 |
| 文件接入 | `backend`：`.venv/Scripts/python.exe -m unittest discover -s tests -p "test_file_ingress*.py" -q` | 14 |
| 邮件身份识别 | `backend`：`.venv/Scripts/python.exe -m unittest discover -s tests -p "test_imap_message_identity.py" -q` | 4 |

候选文本文件已检查私钥、常见API凭据、认证URL、Bearer和密码赋值模式。唯一候选为AMQP拒绝测试中的`deliberately-wrong-password`，系明确的错误密码测试字面量。未发现需移除的真实凭据；这项检查不声称排除所有可能的敏感信息。论文PDF的内容、字体、页序、链接及目视核验见[版本记录](revision-v1.1.48-20261008.md)。本次未重新执行全量外部基线实验。

源代码和文档的空白检查按`cr-at-eol`接受既有Windows换行。历史XML测试报告及PostgreSQL转储中的尾随空白按原始证据保留，不为通过格式检查改写文件。实验文件和PDF按归档前哈希核对，暂存区与工作区逐文件核对Git对象哈希；四个生成的LaTeX表文件依照仓库既有`.gitattributes`转换为LF换行，已核对除换行外字节一致。
