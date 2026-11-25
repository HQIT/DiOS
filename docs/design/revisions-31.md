# 不可变运行版本

标识：K02-1.01；关联：#31、#25、#32；工程责任：Codex；状态：K02-1.01 工程验证通过，G2 评审待认领。

## 目标与对象

Revision 固定一次运行定义的配置、依赖和来源描述。记录以内容摘要寻址，后续编辑生成新 Revision，既有记录不变。它为运行绑定、部署健康门禁和失败回滚提供共同版本对象。

采用成熟机制：[Kubernetes ControllerRevision](https://kubernetes.io/docs/reference/kubernetes-api/apps/controller-revision-v1/) 将状态保存为不可变快照；[OCI Descriptor](https://specs.opencontainers.org/image-spec/descriptor/) 使用内容摘要标识并校验资源。DiOS 在既有 SQLite/PostgreSQL Provider 上实现相同的快照与摘要原则，不引入新的部署依赖。

## 数据契约

[revision-manifest.schema.json](../../contracts/v1/revision-manifest.schema.json) 定义受信发布端提交格式。

| 字段 | 语义 |
| --- | --- |
| schema_version | manifest 格式版本，当前为 1 |
| definition_id | 不含业务语义的运行定义标识 |
| implementation_version | 既定领域契约中的实现版本序号；厂商版本字符串保存在依赖描述中 |
| configuration | 受信发布端生成的公开配置快照，含内联 Prompt 与模型参数等 |
| dependencies | 必须列出 prompt/model/skill/tool/policy/image/adapter/harness 八类；没有依赖的类别使用空数组 |
| secret_refs | Secret 的不透明引用；具体值和实际授权在执行边界获取 |
| artifact_refs | 关联产物的不可变标识 |

每项依赖包含 name、reference、version、digest、visibility。同类 name 唯一。受控 Prompt、技能、工具、策略、镜像和 Adapter 引用必须带 SHA-256 摘要；至少声明 Adapter 与 image/harness 之一。内联配置本身由 config_digest 固定。

visibility 为 pinned（有摘要）、declared（仅有外部版本声明）或 opaque（外部实现版本未知）。记录这些描述不授予执行权，也不证明远端实现符合声明；消费端的内容核验、能力复核和准入由 K02-1.02/.04 承接。

manifest 采用项目现有确定性 JSON 编码：键排序、紧凑分隔、UTF-8、拒绝非有限数。content_digest 覆盖整个 manifest；config_digest 覆盖 configuration。Revision ID 同时包含 scope、definition_id 和 content_digest 的摘要，相同内容跨作用域保留独立身份。

configuration 禁止常见凭据字段、带凭据的 URL 和私钥。受信发布端负责从 Secret 服务引用构造输入，不接受普通用户直接发布任意配置。存储限制为 256 KiB，配置嵌套最多 32 层。默认查询不返回配置正文，可信本地管理员可显式请求 manifest。

## 持久性与幂等

runtime_revisions 保存不可变内容、定义内序号、发布者和数据库时间；runtime_revision_receipts 保存每次幂等请求到 Revision 的对应。发布者字段是受信调用者传入的审计标签，身份鉴别沿用调用边界。

创建在既有作用域事务内完成。相同幂等键、内容和发布者返回同一记录；参数冲突返回 IDEMPOTENCY_CONFLICT。不同请求发布相同内容复用 Revision，保留各自回执。不同内容在同一定义内按事务分配唯一递增序号。

SQLite 与 PostgreSQL 均安装 UPDATE/DELETE 拒绝触发器；服务没有修改或删除接口。SQLite 连接启用 recursive_triggers，使 [REPLACE 的隐式删除](https://www.sqlite.org/lang_conflict.html) 同样被拒绝；两后端的更新型 UPSERT 也受约束。读取核验 manifest 摘要、定义和 ID，损坏返回 REVISION_INTEGRITY_FAILED。Schema、表或不可变约束缺失时拒绝使用，不静默修复为可写快照。拥有数据库管理权限的维护操作仍需遵循备份和迁移程序。

新组件通过显式增量迁移安装，不修改既有版本化 Schema 或 Run 内容。备份识别 Revision 表、回执与约束，空实例恢复后保留版本身份及不可变性。

## 管理入口

在 backend 目录，使用已配置的 DIOS_RUNTIME_DATABASE_URL 和受信本地管理身份：

```sh
python -m app.runtime.cli --scope local migrate
python -m app.runtime.cli --scope local revision create --file manifest.json --key publish-001 --actor operator
python -m app.runtime.cli --scope local revision list agent-definition --limit 20
python -m app.runtime.cli --scope local revision show REVISION_ID --manifest
```

创建版本不启动 Worker、不调用模型、不创建或扩大 E2AG 工作授权。API/Console 发布入口与实际任务绑定分别由后续 K02-1 工作项接入，当前普通任务继续使用原路径。

## 验证与后续依赖

专项测试覆盖跨连接并发去重与序号分配、重启读取、作用域隔离、SQL 更新/删除拒绝、格式和凭据拒绝、Schema 不兼容、约束缺失、旧 Run 迁移保留、备份恢复、跨进程 CLI 及零任务派发。

K02-1.02 将任务提交与 Worker 领取绑定到 Revision；K02-1.03 接入 Deployment 激活、健康检查、排空和回滚；K02-1.04 校验外部版本变化；K02-1.05 完成 API/CLI/Console 联合资格。每项按 [WBS](../planning/capability-wbs.md) 单独记录工程证据与接受状态。

工程结果：[版本存储证据](evidence/runtime-revisions-20261125.json)。开发环境迁移与 CLI 验证通过，84 条既有 Run 身份完整保留。
