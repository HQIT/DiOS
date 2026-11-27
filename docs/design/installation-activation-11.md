# 恢复安装的离线启用

恢复安装在源端隔离、恢复点内外结果对账、远端重新认证和资源绑定审阅完成后，由管理员通过本地 CLI 启用。DiOS 同时解除 Runtime 与兼容服务库的全局恢复冻结，保留原任务、尝试、会话、产物和授权历史。

## 职责与依据

Infrastructure 负责停止并隔离旧控制面及执行器，阻止旧实例重新获得资源。管理员记录实际隔离证据与恢复点后的副作用核查。DiOS 负责核验本地未决状态、内容完整性、旧凭据状态及原子启用。

[Red Hat 的 fencing 管理说明](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/8/html/system_design_guide/assembly_configuring-fencing-system-design-guide)支持管理员先确认节点已隔离，再确认资源可以恢复的操作方式。当前接口采用管理员确认；自动 STONITH、云实例隔离或资源端 fencing 由基础设施提供，DiOS 不根据连通性超时推断源端已隔离。

[SQLite ATTACH 事务规范](https://www.sqlite.org/lang_attach.html)规定，文件数据库在非 WAL 日志模式下可原子提交多库变更。启用命令持有本地维护独占锁，将两个库置于 DELETE/FULL 日志模式，在同一 ATTACH 事务中解除冻结。运行时重新打开数据库后沿用其正常日志配置。

## 启用条件

- 恢复回执与管理员证据的 SHA-256 一致。
- 管理员明确确认源控制面和执行器已隔离，并提供隔离、检查点及其后续副作用、远端再认证、不可变版本路径和外部资源审阅四类记录。
- 所有 scope 的活跃或未知 Run/Attempt、工具副作用、旧服务任务及调用均已对账；补报队列为空；旧执行凭据和旧工具授权已失效。
- Runtime 和服务库完整性、外键、关联产物的摘要及大小通过验证。
- 目标 Runtime 与对象存储可取得维护独占锁，数据库与补报无并发写入。

外部记录是管理员的确认依据。CLI 校验其结构和恢复对象绑定；隔离事实由基础设施和管理员核验。证据内容不会被解释为脚本或执行命令。

## 命令

先保留冻结检查本地状态：

```sh
cli/dios runtime installation-readiness /path/to/restored-installation
```

准备受保护的 JSON 文件，`restore_receipt_sha256` 使用该目标 `installation-restore.json` 的 SHA-256：

```json
{
  "format": 1,
  "restore_receipt_sha256": "SHA256_OF_INSTALLATION_RESTORE_JSON",
  "operator": "operator:NAME",
  "source_isolation": {
    "method": "administrator-confirmed",
    "evidence_ref": "change:SOURCE_FENCING_RECORD"
  },
  "checkpoint_reconciliation": {
    "method": "administrator-confirmed",
    "evidence_ref": "report:CHECKPOINT_AND_POST_CHECKPOINT_EFFECTS"
  },
  "remote_reauthentication": {
    "method": "administrator-confirmed",
    "evidence_ref": "report:OLD_IDENTITY_REJECTION_AND_NEW_IDENTITY"
  },
  "resource_review": {
    "method": "administrator-confirmed",
    "evidence_ref": "report:REVISION_PATHS_AND_EXTERNAL_RESOURCES"
  }
}
```

停止目标安装的服务和 Worker 后执行：

```sh
cli/dios runtime installation-activate /path/to/restored-installation \
  --evidence /path/to/protected/activation-evidence.json \
  --confirm-source-isolated
```

该命令只解除恢复冻结，不启动服务、不派发任务、不清空历史，也不恢复旧凭据。后续执行继续经过当前 E2AG 策略、原有版本及授权检查。原 scope 的排空状态保持，管理员按原生命周期流程安排恢复服务。

## 崩溃与重试

启用前先持久化确认记录和预期控制版本。两个数据库在同一事务提交；任一更新失败，两层冻结均保持。提交后持久化最终回执 `installation-activation.json`。

若事务已提交而最终回执尚未写入，重试同一命令核对两库的目标模式及版本，补全原回执。不同恢复回执或不同外部证据不能覆盖原操作；重复提交不会再次递增控制版本。

操作采用离线 CLI，API 与 Console 继续提供状态观察。生产恢复仍由该安装的管理员按维护安排执行。

## 工程验证

[隔离安装验证报告](evidence/installation-activation-20261127.json)记录 15 项通过、零跳过。原生 Docker 任务在备份后完成，上传被中断；源监听器与执行器停止后，从新目录恢复，通过新证书及当前 E2AG 补回原结果，再由离线 CLI 同时解除两库冻结。原任务的 Attempt、会话和产物保持，原排队任务随后完成，再次领取为空闲。

旧证书、当前策略撤权和未决结果均阻止继续；双库更新失败同时回滚；事务提交后回执中断可重试补全；文件以持久化临时文件和原子链接发布。恢复检查使用实际 Attempt 终态 `completed`，防止已完成任务被误判为未决。

该资格覆盖单台 Linux/WSL 上的独立 Worker 进程、真实容器和 mTLS。基础设施生产隔离、未知外部副作用的人工对账及共享后端恢复分别按运维职责和 D01-2.03 执行。
