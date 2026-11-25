# 不可变运行版本

标识：K02-1.01；关联：#31、#25、#32；工程责任：Codex；状态：K02-1.01/.02 工程验证通过，G2 评审待认领。

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

创建版本不启动 Worker、不调用模型、不创建或扩大 E2AG 工作授权。Console 可注册已发布 Revision；版本发布、健康门禁和回滚管理界面由后续 K02-1 工作项接入。已有未绑定 Revision 的任务继续使用原路径。

## 验证与后续依赖

专项测试覆盖跨连接并发去重与序号分配、重启读取、作用域隔离、SQL 更新/删除拒绝、格式和凭据拒绝、Schema 不兼容、约束缺失、旧 Run 迁移保留、备份恢复、跨进程 CLI 及零任务派发。

K02-1.02 将任务提交与 Worker 领取绑定到 Revision；K02-1.03 接入 Deployment 激活、健康检查、排空和回滚；K02-1.04 校验外部版本变化；K02-1.05 完成 API/CLI/Console 联合资格。每项按 [WBS](../planning/capability-wbs.md) 单独记录工程证据与接受状态。

工程结果：[版本存储证据](evidence/runtime-revisions-20261125.json)。开发环境迁移与 CLI 验证通过，84 条既有 Run 身份完整保留。

## 固定版本执行接线（K02-1.02）

CLI 可将现有执行 Profile 发布为 dios-execution-profile-v1 快照。Docker 镜像标签在发布时解析为镜像内容 ID，公开配置和 Secret 绑定描述分别保存；Adapter 源码摘要随快照固定。加载时核验整个快照与当前 Adapter，版本不符返回明确错误。A2A 保存端点配置，并将远端实现和内部模型版本标为 opaque。

```sh
python -m app.runtime.cli --scope local revision publish-profile --profile profile.json --key publish-001 --actor operator
python -m app.runtime.cli --scope local enqueue --revision-ref REVISION_ID --key task-001 --file input.json --policy-file policy.json
python -m app.runtime.cli --scope local worker --revision-ref REVISION_ID --artifacts ./artifacts --policy-file policy.json --once
```

Run 在提交事务内绑定 revision_ref 和 revision_digest，核验 Profile 摘要及 Secret 引用；客户端输入不能直接指定这两个受控字段。幂等回执同时匹配 Revision，不能把不同版本的提交合并为同一个任务。

Worker 按 Profile 摘要和 Revision 领取任务，恢复扫描使用同一 Revision 范围。不匹配任务保持排队，等待相应版本 Worker。原 --profile 入口继续处理未绑定 Revision 的任务；固定版本 Worker 通过 --revision-ref 从数据库重新加载启动配置，不受配置文件后续编辑影响。远端不可见实现保留 opaque 标记。

E2AG 策略按当前授权决定执行。FileWorkPolicy 的 Profile 行可增加 revisions 字典，以 Profile 摘要为键保存并存版本的授权行。当前版本仍使用原行，历史版本只允许显式列出的摘要。选中行单独计算策略摘要，添加新版本不会改变旧版本既有 Grant；删除历史行、修改其权限或停用主体仍会阻止后续受限动作。快照不复制执行权。

### Console 和输入契约

发布 Profile 时，console.input_catalog 的受信文件被读取、校验并以内联 input_catalog_data 保存，样例、动态输入 Schema 和结果 Schema 同属该 Revision。发布后编辑或删除原文件不影响历史版本。

Console 的 profiles_dir 中可使用以下激活文件，id 必须与同一 scope 下的 Revision 定义一致：

```json
{"id":"agent-definition","revision_ref":"rev-<sha256>","enabled":true}
```

普通 API 调用者仍选择 profile_id，不能提交配置、命令或自行提升版本准入。服务在同一提交事务绑定 Revision，并把版本纳入幂等摘要。切换激活文件后，新请求使用新版本；已排队任务保留原绑定。定时计划的提交摘要包含版本，变化时返回 SCHEDULE_BINDING_CHANGED。列表和详情返回 revision_ref。

### 远程 Worker 与工具控制

网关的受信 profiles 文件在已有 path 旁增加 revisions 准入列表；只服务固定版本时可以省略 path：

```json
{"version":1,"profiles":{"agent-definition":{"enabled":true,"revisions":["rev-<sha256>"]}}}
```

机器身份、定义准入、版本准入与当前 E2AG 授权分别校验。版本摘要或导出文件本身不授予执行权。远程任务领取、Attempt 操作、恢复扫描和持久回执均绑定 Revision；移除准入后拒绝该版本后续请求。工具通道从所属 Run 取得版本，沿用人工审批、派发前授权复核和原调用回执。

远程执行机通过受信运维路径接收公开 Revision 导出和相应 Docker 镜像，不需要数据库凭据：

```sh
python -m app.runtime.cli --scope local revision show REVISION_ID --manifest > revision.json
# 将 revision.json 与对应镜像提供到执行机，再运行：
python -m app.runtime.cli --scope local remote-worker --revision-file revision.json --control-file control.json --artifacts ./artifacts --once
```

执行机校验 scope、内容摘要、配置摘要、Revision ID 和 Adapter 版本。镜像供给使用现有 Docker save/load 或镜像分发机制；缺失镜像明确失败。网关登记和 E2AG 策略需要显式保留仍有未完成任务的版本，不能用新版本 Worker 领取旧版本任务。

### 受控挂载与内容供给

发布时，受控只读挂载中的代码、Prompt 和技能通过 [Docker 构建上下文](https://docs.docker.com/build/concepts/context/) 与 [COPY](https://docs.docker.com/reference/dockerfile/#copy) 打入派生镜像，并取消对应宿主机 bind mount。Revision 同时保存各挂载的内容摘要、大小及条目数，执行绑定派生镜像的内容 ID。原目录后续编辑、删除或迁移不改变既有执行内容。

业务输入挂载须在 Profile 的 input_mount_targets 中显式列出目标路径，沿用原数据供给与权限管理；默认把挂载视为受控实现依赖。输入挂载不得遮蔽已打包代码路径。受控目录复制前清除镜像中同路径的原内容，保持 bind mount 原有的目录覆盖语义。

当前 Linux/WSL 发布器限总计 64 MiB、10,000 个条目，拒绝符号链接、特殊文件、读取期间变化和被系统临时目录或镜像 Volume 遮蔽的目标。派生构建关闭网络，拒绝 ONBUILD，基础镜像需具备 rm；更大或不兼容的实现可先用标准镜像流水线打包，再以镜像内容 ID 发布。镜像保留与分发纳入部署运维，不能清理仍被未完成任务引用的版本。

工程证据：[首次执行绑定](evidence/execution-revisions-20261125.json)、[入口与内容绑定](evidence/revision-entries-20261125.json)。部署激活、健康失败回滚和外部声明变化门禁继续由 K02-1.03/.04 承接。
