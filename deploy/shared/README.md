# 共享持久服务部署

远程 Worker 通过 mTLS 网关共享任务状态与产物。PostgreSQL 保存 Runtime 任务、尝试、租约、事件、幂等回执和 E2AG 授权；持久队列沿用同一数据库中的事务提交。产物由控制面统一接收、校验和按内容存储，Worker 保留各自的离线补报目录。

SQLite 继续作为轻量默认配置。共享候选使用已经接入的 PostgreSQL，不改变应用 API、工作身份或授权语义。当前 PostgreSQL 操作按 scope 使用事务级 advisory lock，容量上限由 #54 实测确认。

```mermaid
flowchart TB
  Apps[独立数字分身平台与应用] --> API[DiOS API / Console]
  W1[Linux Worker 1\n本地 spool 与沙箱] -->|mTLS| GW[DiOS 远程执行与工具网关]
  W2[Linux Worker 2\n本地 spool 与沙箱] -->|mTLS| GW
  API --> PG[(PostgreSQL\n状态 / 队列 / E2AG / 事件)]
  GW --> PG
  API --> Objects[(受控 POSIX 持久卷\n内容对象 / 本地回执)]
  GW --> Objects
  PG --> Backup[Infrastructure 备份库]
  Objects --> Backup
```

## 部署边界

| 组件 | 持久状态及连接 | 权限 |
|---|---|---|
| PostgreSQL | Infrastructure 管理的服务；候选资格使用固定 PostgreSQL 16 镜像 | 运维配置 TLS、SCRAM、容量、复制与备份；应用不持有管理员账号 |
| API 与网关 | 同一台 Linux 控制主机，共享一个 POSIX 产物卷 | `dios_runtime` 仅 DML；网关无 Docker Socket |
| 远程 Worker | 每个 Worker 独立进程、本地 spool、实际执行容器 | 仅自己的证书、允许的 scope/profile 与模型 Secret；无数据库凭据 |
| 迁移作业 | 显式执行冻结版本迁移 | 独立 `dios_owner`；日常服务不加载其凭据 |
| 备份作业 | DB dump、产物、配置、旧服务库及离线回执 | `dios_backup` 只读数据库；文件备份由受控运维身份执行 |

产物目录包含 SQLite 补报/回执文件和文件锁，当前要求单一控制主机所有权。多 Worker 的共享通过网关完成。K8S 部署时，API 与网关须在同一个 Pod 中挂载 CSI `ReadWriteOncePod` 卷；副本数为 1，升级使用排空后替换。该访问模式约束单 Pod 挂载，存储复制和旧节点隔离由 Infrastructure 提供；`ReadWriteMany` 不能代替这些条件。[Kubernetes 持久卷规范](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)

现有 `LocalArtifacts` 使用临时文件、fsync、原子链接和 SHA-256 校验。S3 API、控制面多活、跨主机接管尚未获得资格。D01-3 继续承接 HA、隔离与故障切换；本目录不提供尚未验证的 K8S 集群安装器。

## 新实例配置

先按 [Linux 安装](../linux/README.md) 初始化**全新安装目录**，暂不运行默认迁移或启动服务。既有 SQLite 实例继续运行；副本迁移由 D01-2.02 承接。

1. Infrastructure 创建专用空数据库 `dios`，提供带正确 DNS SAN 的服务器证书和 CA。只开放受控来源的 `hostssl ... scram-sha-256`，拒绝明文。数据库跨信任域连接必须校验证书链和主机名。[PostgreSQL TLS](https://www.postgresql.org/docs/current/libpq-ssl.html)
2. 在受保护环境中提供 `DIOS_PG_OWNER_PASSWORD`、`DIOS_PG_RUNTIME_PASSWORD`、`DIOS_PG_BACKUP_PASSWORD`，以数据库配置管理员运行 `psql -X -v ON_ERROR_STOP=1 -d dios -f deploy/shared/roles.sql`。该脚本面向专用实例，角色名固定，重名时失败。后续迁移必须以 `dios_owner` 创建对象，才能继承默认权限。
3. 将 PostgreSQL CA 放到安装目录 `config/pg-ca.pem`。将下列值写入权限 `0600` 的 `service.env`，替换安装路径、DNS 和密码；密码按 URL 规则编码。保留安装器的其余设置。

```dotenv
DIOS_RUNTIME_DATABASE_URL=postgresql+asyncpg://dios_runtime:ENCODED_PASSWORD@postgres.internal.example:5432/dios?ssl=verify-full
PGSSLROOTCERT=/opt/dios-shared/config/pg-ca.pem
```

SQLAlchemy 的 asyncpg 驱动使用 `ssl=verify-full`；`pg_dump/psql` 使用 libpq 的 `sslmode=verify-full`。两者的参数名称不同。[asyncpg 连接参数](https://magicstack.github.io/asyncpg/current/api/index.html)

4. 单独创建 `migration.env`，只写使用 `dios_owner` 的 `DIOS_RUNTIME_DATABASE_URL`。仅 `migrate` 服务叠加该文件。生产服务使用 Secret 挂载或现有受控配置供给；本地 `.env` 不提交版本库。
5. 复制 `compose.shared.yaml` 到安装目录；在 `.env` 中配置 `DIOS_GATEWAY_PORT=9443` 和 `DIOS_RUNTIME_SCOPE=SHARED_SCOPE`。该 scope 同时设置 API 与网关 CLI，须与 Worker 绑定一致。默认只监听回环；跨主机时将 `DIOS_GATEWAY_BIND` 设为受控网卡地址，并配置防火墙。Gateway 直接终止 mTLS，负载均衡使用 TCP 透传。
6. 按 `gateway.example.json` 创建 `config/gateway.json`，替换所有绝对路径、audience 和服务器证书。登记 `machines.json` 中的客户端证书 SHA-256、有效期、worker_id、slots、scope/profile；`remote-profiles.json` 指向控制面固定 profile。每台 Worker 单独签发证书，配置 `worker.example.json`，服务端和客户端 audience 一致。
7. 使用已有 E2AG 工作策略、工具、审批和 Secret 目录。未配置工具时可以使用 `{"version":1,"tools":{}}`、`{"version":1,"approvers":[]}`、`{"version":1,"secrets":{}}` 创建对应文件；空策略不授予工作权限。

在安装目录执行：

```sh
docker compose -f compose.yaml -f compose.shared.yaml config --quiet
docker compose -f compose.yaml -f compose.shared.yaml run --rm migrate
docker compose -f compose.yaml -f compose.shared.yaml up -d --wait backend frontend gateway
```

API 的 `DIOS_DATABASE_URL` 仍指向本卷上的旧服务库 `service.sqlite`；它与 Runtime PostgreSQL 分开备份。旧库保存的目录等状态并未自动迁移到 PG。已有 Linux `doctor` 包含 SQLite 专用检查，不作为共享后端的资格结论。

在远程 Linux Worker 上使用固定版本后端和本地沙箱运行时：

```sh
env -u DIOS_RUNTIME_DATABASE_URL -u DIOS_DATABASE_URL \
  /opt/dios-source/backend/.venv/bin/python -m app.runtime.cli \
  --scope SHARED_SCOPE remote-worker --profile /opt/dios-worker/config/profile.json \
  --control-file /opt/dios-worker/config/control.json \
  --artifacts /opt/dios-worker/spool --poll-interval 0.5
```

工作目录为 `/opt/dios-source/backend`。生产服务管理器固定该目录、Python 路径、凭据、PATH 和代理；每个实例的 spool 路径唯一。profile 的镜像与挂载路径必须在该执行机可用。使用工具通道时只将 `worker_secret_refs` 指定的模型 Secret 提供给 Worker，工具服务凭据留在网关。

## 备份与恢复顺序

`pg_dump` 给出数据库的一致性快照，产物文件和配置需要同一停写窗口。[PostgreSQL 备份说明](https://www.postgresql.org/docs/current/backup-dump.html)

1. 关闭新任务入口，使用现有 `upgrade drain`、`upgrade wait` 等待任务、工具、补报和结果校验完成。保留离线 Worker 的未决记录；观测超时不视作排空完成。
2. 停止 API、网关、调度和所有数据库/产物写入者，禁止同时运行迁移、GC 或人工 SQL。记录 scope、Run/Attempt/Session/Artifact 数量与 ID、每个 Worker spool 水位、镜像和配置摘要。
3. 用权限 `0600` 的 libpq service/pass 文件配置 `dios-backup`，包含 `sslmode=verify-full`、CA、专用备份账号和 passfile。执行：

```sh
pg_dump --dbname=service=dios-backup --format=custom --no-owner --no-acl \
  --file=runtime.dump
tar -cpf data.tar -C "$DIOS_INSTALL/data" objects service.sqlite
sha256sum runtime.dump data.tar > SHA256SUMS
```

4. 同时归档受保护配置、证书引用、固定镜像、应用/CLI 版本，以及保留的 Worker spool。备份上传独立故障域，使用现有备份系统的加密、访问控制、保留周期和回读校验。模型/API 密钥只进入受控 Secret 备份，不出现在报告中。
5. 在隔离空环境按 `roles.sql` 重建账号；以 `dios_owner` 运行 `pg_restore --single-transaction --exit-on-error --no-owner --no-acl --dbname=service=dios-owner runtime.dump`。默认权限重新授予运行和备份角色。还原产物、旧服务库和所需配置，核验 ID、外键、授权、全部引用的 SHA-256 和 spool 水位。
6. 恢复期间保持入口关闭和派发暂停。确认原控制面隔离、旧执行身份失效、当前策略和 Secret 版本有效后，再执行既有恢复授权流程。源端隔离方式仍属于 K02-3.02 的待决条件。

D01-2.01 验证只读账号能产生 PG custom dump；D01-2.03 验证整套备份在干净环境恢复。数据库复制、WAL 归档与卷快照可缩短恢复窗口，RPO/RTO 按容量与 HA 资格报告填写实测值。

## 工程资格

```sh
backend/.venv/bin/python scripts/qualify_shared_services.py \
  --output workspace/shared-qualification-NEW
```

需要已有 `postgres:16-alpine` 和 `dios-worker-fixture:local` 镜像、Docker、OpenSSL。脚本创建独立 PG 容器/卷、三类账号与 TLS；实际 CLI 网关和两路 mTLS Worker 完成任务、产物上传、幂等提交及 E2AG 验证，不调用模型。输出目录含私密连接资料，权限为 `0700`，不要上传整个目录。容器、数据卷和证据保留用于复核；停止或删除前依据 `installation.json` 定位本次资源。

数据库采用 [PostgreSQL License](https://www.postgresql.org/about/licence/)，分发时保留许可文本。Docker 镜像须按不可变 ID/摘要固定并核对其 SBOM。CSI/备份产品由 Infrastructure 选定，其许可证和商用服务条款单独登记，不成为 DiOS 的业务或供应商依赖。
