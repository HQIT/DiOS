# Linux 安装与升级资格

DiOS 的 Linux 安装工程资格覆盖干净安装、受控任务、自然排空、固定镜像切换、兼容 Schema 迁移、失败回退、跨 Harness 接入及后台环境诊断。发布接受沿 D01-1.04 进行。

## 验收对应

| 原验收 | 实际证据 |
| --- | --- |
| D01-1-A01 干净环境完成受控任务 | 新目录 Compose 初始化、固定镜像、显式迁移，三路原生 ACP 任务及一条排队任务完成 |
| D01-1-A02 升级和回滚保留状态 | 配置摘要、Run/Attempt/Session、API 历史和产物下载摘要保持；迁移前后各注入一次启动失败并回退 |
| D01-1-A04 跨实现交付证据 | 复用 Cursor/DSH 六个真实成功场景；八个共享源码及结果文件摘要一致 |
| D01-1-A05 后台诊断及安全排空 | 复用真实 systemd 的依赖/代理故障与修复证据；本批三路进程/会话自然排空，未强杀、未重复派发，模型请求总数为四 |

[本批工程报告](evidence/installation-qualification-20261127.json)包含镜像摘要、Schema 清单和任务身份。新增联合场景 1 项通过、零跳过；其余已通过证据按源码与验收范围复用。

## Schema 升级与回退

原版本 `a819de71a3eb92f8c1a6dc2091ba1015a2d06204` 升级至候选 `5ed239caf992165ffe928c0c4d14bf2235ff9381`，实际新增 `runtime_technical_release_schema` 和 `runtime_technical_releases`，同时安装 UPDATE/DELETE 不可变约束。迁移保留四条 Run、三条已完成 Attempt；恢复领取后原排队任务产生唯一 Attempt 并完成。

旧版本的备份清单遗漏其 Studio 表，会以 `BACKUP_SCHEMA_INCOMPATIBLE` 拒绝备份。升级前使用已固定目标镜像中的 `installation-backup` 与 `installation-verify`，支持读取旧结构而不迁移源库。本批验证源 Schema 清单和任务数量保持不变。底层沿用 [SQLite Online Backup API](https://www.sqlite.org/backup.html) 的一致快照能力。

本次 Schema 变化为已验证的增量兼容迁移，允许原版本重新运行。破坏性或不兼容迁移须另有受验收的恢复方案。干净恢复、原结果对账及恢复时间见[升级失败恢复](recovery-upgrade-11.md)。

## 复现

提供固定构建清单，`images` 包含 `old`、`new`、`bad`、`frontend` 四个完整本地镜像摘要，再执行：

```sh
backend/.venv/bin/python scripts/qualify_installation_schema.py \
  --images-file /path/to/fixed-images.json \
  --output /path/to/installation-qualification.json
```

原生安装场景使用合成 ACP 模型端点；真实 Cursor/DSH 证据见[跨 Harness 报告](evidence/reference-harnesses-20261127.json)。后台资格见[systemd 实测](evidence/unattended-cutover-20261126.json)，安装与操作步骤见 [Linux 交付说明](../../deploy/linux/README.md)。共享后端、多节点及企业容量继续按原工作项验收。
