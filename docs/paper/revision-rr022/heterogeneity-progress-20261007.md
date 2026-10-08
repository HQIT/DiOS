# 精简异构验证：文件入口资格检查

执行范围：[v0.3 覆盖矩阵](heterogeneity-protocol-v0.3-20261007.md)。状态：资格检查，尚未执行 R0—R5 正式比较；论文与既有冻结结果未改。

## 已完成

新增独立实验目录 `experiments/e2ag/heterogeneity/`，没有安装后台文件监听服务或修改生产 Connector 注册表。文件适配器读取真实 ready 文件，校验信封和受控目录路径，固定来源为 `file/<connector_id>`、类型为 `file.received`；载荷不得覆盖这些字段。投递 ID 映射为规范事件 subject，以适配现有去重键；原业务标题保存在 deliverysubject，数据字段保持不变，另保存规范内容摘要。此映射必须在后续各接入配对中披露和检查，不称逐字输入相同。

入口通过实际订阅匹配与 dispatch_event 进入原有准入逻辑。实验声明只注册精确的 `file/fixture` 来源与 `file.received` 类型，不放宽核心策略。测试替换了 a2a_service.send_message 为真实任务记录创建，而没有启动 Agent/调用工具；因此只能证明输入与准入衔接。

最终共 22 项通过、0 失败、0 跳过：14 项新增文件资格测试（10 项输入/文件测试、4 项原生准入测试）及 8 项已有入口/审批回归。新增测试覆盖完整发布前后、非法路径、目录与硬链接、重复 JSON 字段/非法数值/大小、来源绑定、字段保持、标识和内容摘要，以及正常任务创建、来源拒绝、待审批和重复扫描。使用临时真实文件及内存 SQLite，跨会话重复扫描不是跨进程持久恢复。

初次开发执行为 8 项通过、4 项初始化错误：临时 ConnectorManifest 缺少必填 label，尚未到达准入；补齐后原 12 项通过，再追加 2 项文件/数值检查并连同已有 8 项回归执行，得到上述最终结果。没有修改预期判定使失败通过。

机器记录包含测试日志、Python 版本及适配器/测试/后端源码 SHA-256：
`experiments/e2ag/heterogeneity/results/file-qualification-20261007-v1.json`。

重跑命令（须给新输出名，脚本拒绝覆盖既有结果）：

```powershell
.\backend\.venv\Scripts\python.exe -X utf8 experiments/e2ag/heterogeneity/run_file_qualification.py --output experiments/e2ag/heterogeneity/results/file-qualification-NEW.json
```

## 未完成及下一步

同 ID 不同内容目前仅能用摘要识别，尚未实现持久冲突处理；交付终结、故障恢复、目录 ACL 实证、符号链接/重解析点实际用例与真实工具调用尚未通过本轮检查。文件发布后不再改写、目录由可信管理员配置是明确前提，当前读取检查不证明能抵御任意并发文件系统篡改。

下一步补文件投递的持久接收/冲突/恢复语义并接真实工具端，随后推进 AMQP、stdio 与 PostgreSQL。AMQP、工具传输和数据库尚未有新增运行结果。完成各项资格检查后冻结正式版本，按矩阵配对，不继续增加接入种类。
