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

## 当前覆盖与待完成部分

| 数据 | 当前恢复包 |
| --- | --- |
| Runtime 数据库与组件 Schema | 一致快照、逐表摘要和引用校验 |
| Revision、Run、Attempt、授权、委派、部署 | 保留身份与关联；校验缺失、错属和跨 scope 引用 |
| 本地产物、归档、远程上传分块、工具回执 | 依照已有持久引用复制并校验摘要 |
| 显式配置文件 | 捕获字节、源文件变化检测、私有副本和恢复 |
| 固定镜像与模型/端点 | 记录可获得的引用；镜像须独立保存，远端内部实现不可恢复 |
| 工作目录、各 Worker spool | 记录恢复需要，安装级收集仍待接入 |
| 兼容服务数据库与跨库关联 | 安装级备份及联合一致性校验仍待接入 |
| PostgreSQL | 当前命令明确返回不支持；后续 Provider 采用原生工具 |

PostgreSQL 的原生 `pg_dump` 支持并发访问下的一致导出；跨数据库、文件和 Worker 数据仍需平台恢复清单关联。[PostgreSQL 16 pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html)

K02-3.01 保持进行中，直到安装级清单覆盖兼容服务库、必要 workspace 与 Worker spool，并完成关联验证。K02-3.02 承接干净实例恢复和远端重认证，K02-3.03 承接升级、回退及恢复计时。三项的原有范围和工作项数量保持不变。
