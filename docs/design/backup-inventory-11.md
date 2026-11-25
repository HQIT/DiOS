# 一致性备份清单

Runtime 恢复包包含一致数据库快照、被引用的本地产物、未入库工具回执，以及显式选定的配置文件。版本、Run、Attempt、工作授权、委派、子任务和部署之间的引用从快照中重新核验。外部凭据、镜像、工作目录、进程和远端会话列入恢复依赖。

## 快照与校验

SQLite 继续使用官方 Online Backup API。该机制允许并发写入并产生一致的数据库副本；复制内容对象和生成清单均依据这个副本，不再次从在线数据库读取关联。[SQLite Online Backup](https://www.sqlite.org/backup.html)

格式 3 增加 `inventory` 和 `configuration`，保留格式 1/2 的验证与恢复能力。`inventory.tables` 记录每张 Runtime 表的记录数和确定性摘要；`inventory.references` 记录通过校验的引用数量；`inventory.external` 记录需要独立恢复、重新认证或对账的依赖。

数据库完整性、外键、组件版本、Revision 内容摘要、跨作用域引用和逻辑归属错误会阻止备份发布。缺失或损坏的本地产物、配置及工具回执同样阻止验证通过。清单校验可发现传输损坏，可信来源及存储权限由部署管理负责。

配置文件以 `--config-file NAME=PATH` 显式提供。命令在数据库快照前读取配置，在发布清单前核验源文件仍与捕获内容一致；观察到变化即失败。每个文件最多 16 MiB，总量最多 32 MiB、64 个文件。配置文件集合与数据库没有共同事务；版本化执行仍由数据库内不可变 Revision 保证，跨组件配置发布需使用统一排空窗口。

```sh
backend/.venv/bin/python cli/dios runtime backup \
  --artifacts /srv/dios/objects \
  --destination /srv/backup/new-bundle \
  --config-file profiles=/srv/dios/profiles.json \
  --config-file policy=/srv/dios/work-policy.json \
  --config-file tools=/srv/dios/tools.json

backend/.venv/bin/python cli/dios runtime backup-verify /srv/backup/new-bundle
```

备份目录和配置文件使用私有权限；配置可能包含敏感内容。Secret 服务中的密钥不按引用自动抓取。恢复仅将配置放入新目录，不自动覆盖运行中的配置或解除 `restore_hold`。

## 安装级备份

`installation-backup` 将 Runtime 恢复包、兼容服务数据库、必要工作目录及 Worker 离线补报纳入同一清单。安装编排位于 `app.services`，复用 Runtime 的快照、产物与授权引用校验；内核数据模型不依赖业务应用。兼容执行记录的 `run_id` 保留原身份，与 Runtime Run 分别校验。

计划文件使用绝对路径，示例：

```json
{
  "format": 1,
  "runtime_database_url": "sqlite+aiosqlite:////srv/dios/runtime.sqlite",
  "service_database_url": "sqlite+aiosqlite:////srv/dios/service.sqlite",
  "artifacts": "/srv/dios/objects",
  "configuration": [{"id": "profiles", "path": "/srv/dios/profiles.json"}],
  "workspaces": [{"id": "agents", "path": "/srv/dios/agents"}],
  "spools": [],
  "external": []
}
```

```sh
backend/.venv/bin/python cli/dios runtime installation-backup \
  --plan /srv/dios/backup-plan.json --destination /srv/backup/new-installation
backend/.venv/bin/python cli/dios runtime installation-verify /srv/backup/new-installation
```

本地产物目录的 `.reports-<scope摘要>.sqlite` 自动收集；其他 Worker 的 spool 通过计划显式提供。补报校验原 Run、Attempt、scope、Worker 实例、epoch、事件序号和内容摘要，备份过程不发送补报。已知工作目录或 MCP 配置必须存在于捕获内容中，或通过 `external` 的 `kind`、`reference`、`recovery` 明确恢复安排。

服务数据库与 spool 使用 SQLite 原生快照，清单记录逐表摘要、Schema 和关联。各数据库分别保持一致，跨库依据持久引用核验；需要共同业务时点时，应先排空相关写入。工作目录适用于稳定输入文件，使用有界复制与复查；发现变化、软链接或特殊文件即失败，保留空目录及可执行标志。在线数据库应使用数据库快照 Provider，不能作为普通工作目录文件复制。当前文件树 Provider 使用 POSIX `dir_fd` / `O_NOFOLLOW`，Windows ACL 备份另需 Provider。[Python os](https://docs.python.org/3/library/os.html#os.open)

默认工作目录总量上限为 1 GiB，每棵树最多 20,000 个文件；可通过 `--max-tree-bytes` 调整总字节上限。服务数据库可能包含凭据，恢复包采用私有权限，应置于受控存储。完整 `installation.json` 最后发布；缺少该文件的目录视为未完成备份。安装验证可脱离来源路径独立完成。

## 覆盖范围

| 数据 | 当前恢复包 |
| --- | --- |
| Runtime 数据库与组件 Schema | 一致快照、逐表摘要和引用校验 |
| Revision、Run、Attempt、授权、委派、部署 | 保留身份与关联；校验缺失、错属和跨 scope 引用 |
| 本地产物、归档、远程上传分块、工具回执 | 依照已有持久引用复制并校验摘要 |
| 显式配置文件 | 捕获字节、源文件变化检测、私有副本和恢复 |
| 固定镜像与模型/端点 | 记录可获得的引用；镜像须独立保存，远端内部实现不可恢复 |
| 工作目录、各 Worker spool | 显式工作目录、自动本地补报及显式远端副本；内容摘要和执行身份校验 |
| 兼容服务数据库与关联 | 原生快照、逐表摘要、任务/服务归属及 scope 校验；保留独立执行身份 |
| PostgreSQL | 当前命令明确返回不支持；后续 Provider 采用原生工具 |

PostgreSQL 的原生 `pg_dump` 支持并发访问下的一致导出；跨数据库、文件和 Worker 数据仍需平台恢复清单关联。[PostgreSQL 16 pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html)

K02-3.01 的安装清单包含兼容服务库、必要 workspace 与 Worker spool，工程证据见 [安装备份验证](evidence/installation-backup-20261125.json)。K02-3.02 承接干净实例恢复和远端重认证，K02-3.03 承接升级、回退及恢复计时。恢复历史授权和补报记录后，须隔离原调度器并重新认证、对账，才可恢复执行或补报；备份校验本身不授予执行权限。

## 安装级干净恢复

```sh
backend/.venv/bin/python cli/dios runtime installation-restore \
  /srv/backup/new-installation /srv/dios-restored
```

目标目录必须不存在。命令校验全部备份内容后，恢复 Runtime、服务库、配置、工作目录和 spool。Runtime 与兼容服务均处于全局 `restore_hold`；普通 drain/resume、旧任务继续派发、工具授权、进程停止及后台自动对账均无法解除此状态。复制或迁移失败时不发布完整的 `installation-restore.json`，不覆盖原实例。

已生效的 Worker 短期凭据和兼容服务工具 Grant 在副本中撤销，保留身份、历史和原关联。配置副本保持私有权限。兼容对象中的显式工作目录及配置路径映射到新目录；不可变 Runtime Revision 保留原摘要，重定位所需新 Revision 在启用前单独核对。

补报保存在独立 `spools` 目录，保持原内容，不自动挂到正在工作的产物目录或发送。恢复清单记录映射、撤销数量、来源摘要及外部依赖。`--timeout` 控制文件捕获和最终发布的期限；已开始的数据库恢复会安全完成其当前步骤，超时后不发布安装级成功凭据。

工程证据见[干净恢复与冻结](evidence/installation-restore-hold-20261125.json)。K02-3.02 的远端重新认证、源控制面隔离、完整结果对账和安全启用仍需联合验证，依据[恢复研究约束](recovery-research-11.md)推进。
