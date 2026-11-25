# 统一对象版本与引用

DiOS 用 `{kind, id, version}` 表达文件、会话和产物引用。引用由控制域解释，独立于执行镜像、进程、Worker 和存储路径。上层应用通过不透明来源键建立关联，业务组织与项目模型由应用管理。

## 对象语义

| 对象 | 稳定标识 | 版本与内容 | 权限入口 |
| --- | --- | --- | --- |
| 文件 `file` | 服务端生成的 UUID | 每次成功发布形成新版本；旧版本及其内容摘要保持不变 | 管理者发布，读取与下载采用具体版本的主体授权 |
| 会话 `session` | 原 Attempt ID | v1 固定指向原 Run、Attempt、供应商 Session；`descriptor` 为空 | 沿用原 Run 的 `run.read` |
| 产物 `artifact` | 原 Artifact ID | v1 固定指向原内容摘要、大小及 Run/Attempt | 沿用原 Run 的 `artifact.read` |

会话引用表示已记录的会话身份，不包含供应商内部完整会话存储。相同供应商 Session 字符串出现在不同 Attempt 时，仍保留各自的运行上下文。已有会话和产物直接解析原记录，无须重编号或复制历史。

文件版本采用预期版本号校验和持久幂等回执。并发发布同一对象时只有一个请求能推进版本，其余返回 `VERSION_CONFLICT`；同请求重试返回原版本，修改请求复用幂等键返回 `IDEMPOTENCY_CONFLICT`。相同内容可以复用底层字节，但仍保留独立对象身份。

内容描述符包含 `digest`、`size` 和 `media_type`。API 不返回物理存储键。存储继续使用现有 `ArtifactProvider`，文件版本及其字节一并进入一致备份和恢复清单。

## 关联

`derived_from`、`part_of`、`associated_with` 将一个明确版本连接到另一个明确版本，或连接到 `{namespace, key, version}` 外部引用。关系保留创建者和时间，同关系重复提交返回原记录。关系不会跟随文件最新版本漂移，也不授予目标对象读取权限。

外部引用只保存调用方提供的来源标识，不抓取地址、不解释客户或项目含义。关系由管理者写入，主体查询按对象权限过滤；[S02-1.02 对象授权](object-access-38.md)已接入，S02-1.03 承接留存、删除与墓碑。

## API、CLI 与 SDK

| 操作 | HTTP |
| --- | --- |
| 发布文件版本 | `POST /api/os/runtime/objects/files` |
| 查询指定版本 | `GET /api/os/runtime/objects/{kind}/{id}/versions/{version}` |
| 分页查询版本 | `GET /api/os/runtime/objects/{kind}/{id}/versions?after=0&limit=50` |
| 建立关系 | `POST /api/os/runtime/objects/relations` |
| 分页查询关系 | `GET /api/os/runtime/objects/{kind}/{id}/versions/{version}/relations` |

文件 HTTP 发布接收至多 1 MiB 的 base64 内容；本地可信 CLI 通过既有 Provider 的容量限制分块读取文件。发布示例：

```bash
python -m app.runtime.cli --scope local object publish \
  --file ./notes.txt --artifacts ./objects --key notes-v1 \
  --actor operator:admin --media-type text/plain
```

后续版本增加 `--object-id <原ID> --expected-version <当前版本>` 并使用新的幂等键。`object show`、`object versions`、`object link`、`object relations` 提供对应查询和关联操作。

Python SDK 提供 `publish_file`、`object_version`、`object_versions`、`link_objects`、`object_relations`。原任务详情的 `artifacts[].object_ref` 与 `attempts[].object_ref` 提供统一引用。Console 在产物条目显示版本号，原产物与统一对象下载均接入[对象级授权和撤销](object-access-38.md)。SDK 0.1.13 同步提供策略管理和下载接口。

## 依据与验证

对象与版本分离参考 [S3 Versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/versioning-workflows.html)；内容摘要、大小和类型参考 [OCI Descriptor](https://specs.opencontainers.org/image-spec/descriptor/)；显式来源与派生关系参考 [W3C PROV-O](https://www.w3.org/TR/prov-o/)。这些语义映射到 DiOS 现有存储和权限机制。

[工程证据](evidence/object-versions-20261126.json)覆盖 SQLite/PostgreSQL 并发、幂等、历史版本、重启、备份恢复、真实身份分享与撤销，以及执行配置更换后的原引用保持。发布镜像与开发 API、CLI、安装后的 SDK 和浏览器界面分别验证。原有任务、尝试和产物身份全部保留。
