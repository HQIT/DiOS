# 对象留存、删除与来源失效

对象生命周期绑定具体版本。保留期内拒绝删除，保留期限只能延长；显式删除或到期后阻断内容读取，保留原对象身份、内容摘要、Run/Attempt 关联和来源边。底层内容的物理回收仍待完成。

## 状态和约束

| 状态 | 含义与读取行为 |
| --- | --- |
| `active` | 按当前对象 ACL 和原任务权限读取 |
| `deleted` | 已提交删除标记；所有内容读取入口拒绝 |
| `expired` | 到达配置的 `expires_at`；读取即时失效，无需等待清理任务 |
| `source_unavailable` | 显式 `derived_from` 来源链存在已删除或到期版本；派生成果读取拒绝 |

`retain_until` 是最短保留期限，`expires_at` 是可选到期时刻。后者不得早于前者。保留期结束后，对象可以删除；未配置 `expires_at` 的对象不会自动到期。原生任务尚未终结时，拒绝显式删除它的产物或会话引用。

每次修改使用期望修订号与幂等键；并发修改只能有一个成功。已删除、到期的版本不能恢复为有效版本，重放旧请求也不会改变状态。可以为同一文件发布新的独立版本，旧的来源边仍固定到原版本。

## 读取与来源传播

统一对象下载、原产物下载、预览、Markdown 下载和 E2AG 子任务结果读取均检查生命周期。正在读取的流在后续块检查中观察到删除或到期后停止发送。原任务完成状态保持历史事实，结果内容可能已不可访问。

来源失效沿已登记的 `derived_from` 关系传递。`associated_with` 和外部不透明引用不自动传播删除。读取即时检查来源，不依赖异步队列追赶。新增派生关系拒绝闭环；遍历超过深度 64 或 4096 个节点时停止并拒绝读取。

```mermaid
flowchart LR
    A[来源 v1 已删除] --> B[派生 v1 不可读取]
    B --> C[后续派生不可读取]
    D[同摘要独立对象] --> E[维持自身权限与生命周期]
    F[来源 v2] --> G[独立生命周期]
```

共享内容通过摘要复用，但删除标记按对象版本记录。因此，删除一个对象不会破坏相同摘要的独立对象。管理者可通过生命周期接口查询墓碑、摘要和原任务关联；备份恢复保留这些状态与来源边。

## 管理接口

以下接口限定管理者使用，基于 `/api/os/runtime/objects/{kind}/{id}/versions/{version}`：

| 方法与后缀 | 请求 |
| --- | --- |
| `GET /lifecycle` | 返回修订号、留存和到期时刻、直接或派生失效状态、原因及原始关联 |
| `PUT /retention` | `expected_revision`、`idempotency_key`、`retain_until`、可选 `expires_at` |
| `POST /delete` | `expected_revision`、`idempotency_key`、`reason` |

Python SDK 0.1.14 提供 `object_lifecycle`、`retain_object`、`delete_object`。CLI 提供 `object lifecycle`、`object retain`、`object delete`，统一使用 `--version`、`--expected-revision`、`--key` 和 `--actor`。

普通读取接口对不可用对象返回 `NOT_FOUND`，沿用 `no-store`。生命周期管理接口保留墓碑信息；原 Run、Attempt 和产物 ID 不重建。

## 物理回收待完成项

S02-1.03 继续保持未完成。当前删除是可核验的访问失效与墓碑记录，`content_collection=retained` 明示底层内容保留。

物理回收采用成熟的标记清扫方法，必须在写入隔离期间完成全部引用检查。检查范围包含文件版本、原生产物、历史归档、远程上传块、工具回执及恢复材料；引用和幂等回执保留稳定身份。清扫与备份恢复还需要验证中断恢复、重复运行，以及重新发布同摘要内容时的完整性。

这部分继续归属当前 S02-1.03；完整生命周期资格归属 S02-1.04，不新增 WBS 分母。

## 依据与验证

[S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)区分版本保留期限与删除标记。[W3C PROV-DM](https://www.w3.org/TR/prov-dm/)提供实体失效和派生关系的基础语义。[CNCF Distribution](https://distribution.github.io/distribution/about/garbage-collection/)采用共享内容标记清扫，并要求清扫期间只读或停止服务。DiOS 复用这些机制的职责划分，生命周期采用当前数据库和已有授权检查。

[本批工程证据](evidence/object-lifetime-20261126.json)记录数据库、读取入口、E2AG 子任务通道、备份恢复及已部署 API/CLI/SDK 验证。
