# Linux 安装与后台运行

DiOS 控制面使用 Docker Compose，默认存储为 SQLite 与本地对象目录。Worker 独立运行，通过已登记配置接入 Cursor 等 Harness；客户应用独立部署。

多 Worker 共享状态的 PostgreSQL、mTLS 网关、持久产物及备份配置见 [共享持久服务部署](../shared/README.md)。

## 环境

- Linux 主机，或已经运行的 WSL Linux；Python 3.10+、Docker Engine、Compose v2。安装账户可访问 `/var/run/docker.sock`。
- 准备固定版本的后端和前端镜像。离线交付可用 `docker load -i images.tar`；从源码构建可用仓库 `backend/Dockerfile` 与 `frontend/Dockerfile`。镜像需要与本分支版本匹配。
- 使用独立、尚不存在的安装目录；路径只接受字母、数字、`_`、`-`、`.` 和 `/`。安装账户拥有目录，凭据文件权限为 `0600`。

Docker Socket 赋予控制面和 Worker 主机容器管理权限，两个组件属于可信执行管理端。Agent 工作负载继续使用独立非 root 沙箱；模型凭据与应用配置按 profile 单独登记。

## 首次安装

在仓库根目录执行，替换两项镜像名称和安装位置：

```sh
export DIOS_INSTALL=/opt/dios-instance
python3 deploy/linux/install.py init --root "$DIOS_INSTALL" \
  --backend-image dios-backend:candidate --frontend-image dios-frontend:candidate
python3 deploy/linux/install.py doctor --root "$DIOS_INSTALL"
docker --host unix:///var/run/docker.sock compose --project-directory "$DIOS_INSTALL" run --rm migrate
docker --host unix:///var/run/docker.sock compose --project-directory "$DIOS_INSTALL" up -d --wait backend frontend
python3 deploy/linux/install.py doctor --root "$DIOS_INSTALL" --running
```

`init` 将已有镜像解析为不可变 image ID，不会覆盖已有安装。`migrate` 只用于首次初始化；已有实例的升级先执行排空、备份与对应版本的升级流程。启动服务不会自动运行该迁移命令。

Console 位于 `http://localhost:3000/dios/`，API 位于 `http://localhost:8000`，均只监听主机回环地址。Access Token 保存在安装目录的 `service.env` 中，由管理员通过本机受控方式取用。远程访问通过现有身份代理/TLS 入口接入，不直接开放管理端口。

目录中的 `.env` 保存镜像、账户、端口和绝对路径；`service.env` 保存服务设置与 Access Token；`config` 保存 profiles 与 E2AG 策略；`data` 保存数据库和产物；`workspace` 保存工作目录。配置与 CLI 以只读方式挂载，数据库和对象目录独立持久化。

初始 E2AG 为 `enforce`，工作权限为空，Runtime Console 与持久调度已启用。登记执行配置、精确版本权限、工具与审批身份后再开放任务。模型服务没有安装级默认依赖。

## 后台与诊断

API/Console 使用 Compose `restart: unless-stopped`，前端等待后端健康后启动。Docker 守护进程需随 Linux 启动。WSL 的发行版启动由宿主运维负责。

```sh
docker --host unix:///var/run/docker.sock compose --project-directory "$DIOS_INSTALL" ps
docker --host unix:///var/run/docker.sock compose --project-directory "$DIOS_INSTALL" logs --tail 100 backend
python3 deploy/linux/install.py doctor --root "$DIOS_INSTALL" --running
```

`doctor` 检查 Docker 的实际 PATH、守护进程和 Compose、固定镜像、凭据权限、配置覆盖及 API/Runtime 可达性；只输出代理变量是否存在，不输出 Token 或代理地址。宿主 Shell 中的代理不会自动进入服务。镜像拉取代理配置在 Docker daemon；控制面访问外部服务的代理显式写入 `service.env`，`NO_PROXY` 包含实际内网端点；模型代理按 Harness profile 配置。

若后台与交互终端表现不同，在后台账户下用精简环境运行同一检查：

```sh
env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin /usr/bin/python3 \
  deploy/linux/install.py doctor --root "$DIOS_INSTALL" --running
```

诊断中 `compose_environment` 失败时，按列出的变量名核对当前 Shell 与 `.env`。不要将 `docker compose config` 的完整结果写入公开日志，其中可能包含服务凭据。

## 独立 Worker

在固定源码版本的 `backend` 中建立 `.venv`，按 `requirements.txt` 安装依赖。Docker 调用由 Python SDK 完成，Git、模型 CLI 等任务依赖放在对应工作负载镜像中。配置文件中的宿主路径须为绝对路径。

```sh
python3 deploy/linux/install.py worker-unit --root "$DIOS_INSTALL" --backend /opt/dios-source/backend
systemd-analyze verify "$DIOS_INSTALL/dios-worker@.service"
sudo install -m 0644 "$DIOS_INSTALL/dios-worker@.service" /etc/systemd/system/
sudo systemctl daemon-reload
```

生成器固定安装账户、Docker 组、Python 路径、PATH、工作目录和环境文件，不自动启动 Worker。登记 `config/profiles/cursor.json` 以及 E2AG 工作权限、工具目录、审批者、Secret 引用后，用 `sudo systemctl enable --now dios-worker@cursor` 启用。需要的 `DIOS_RUNTIME_TOOLS_FILE`、`DIOS_RUNTIME_APPROVERS_FILE`、`DIOS_RUNTIME_SECRETS_FILE` 加入受保护的 `service.env`；使用只读的安装配置目录存放文件。

CLI 默认从 `DIOS_RUNTIME_SCOPE` 读取实例范围，与 API 和后台服务使用同一队列；显式 `--scope` 优先。未配置该变量时沿用 `local`。操作已有实例时加载该实例的环境文件，或显式指定其 scope。

使用工具通道的 profile 通过 `tool_channel.worker_secret_refs` 显式列出 Agent 需要的模型凭据。本地和远程执行均遵循此列表；省略表示不向容器挂载凭据。工具网关使用的 `credential_ref` 留在网关侧，与执行端列表互斥。修改列表后重新固定 profile 和 E2AG 工作授权，排空旧任务后切换；不要通过固定目录挂载暴露整个 Secret 目录。

Secret 文件权限、OpenBao 原生 Agent 后台配置、固定版本注入和轮换操作见 [Linux Secret 交付](secrets.md)。

ACP 的管理员配置支持可选的 `preflight`，在 Harness 实际环境中、创建模型会话前检查依赖和网络。只声明该 Harness 必需的命令；Git 不属于 DiOS 控制面的必需依赖。例如：

```json
{
  "preflight": {
    "commands": ["git"],
    "network": [
      {"id": "model-route", "url": "https://gateway.example/health", "proxy_env": "HTTPS_PROXY"}
    ]
  },
  "credential_env_files": {"HTTPS_PROXY": "/run/secrets/model-proxy"}
}
```

将这两项合入现有 ACP 配置；地址替换为支持 HEAD 的受控健康端点。`proxy_env` 表示该探测必须使用指定环境变量中的代理，缺失时返回明确错误；省略则直连，不继承宿主代理。代理认证可通过既有 Secret 文件注入。探测不跟随重定向、每项最多 5 秒，最多 16 项检查；健康地址不接受内嵌凭据、查询参数或 fragment。端点可达性通过后仍需实际 Harness 会话确认模型服务可用。

依赖缺失、代理缺失或格式错误归为配置故障；连接失败进入既有网络恢复策略。诊断只包含错误代码及命令名/检查 ID。修复配置后按原任务的恢复入口继续，后续执行仍需 E2AG 授权。修改已登记配置时沿用版本固定与排空切换流程。

后台环境沿用 [systemd EnvironmentFile](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)，网络检查采用 [HTTPX 显式代理](https://www.python-httpx.org/advanced/proxies/)。[Linux 后台资格证据](../../docs/design/evidence/unattended-cutover-20261126.json) 包含真实 systemd 服务的 PATH 缺失、代理缺失、代理不可达和修复后执行，以及 journal 凭据检查。

采用 [Cursor 原生控制配置](../../docs/design/cursor-native-controls.md)和已验证镜像。每个服务实例运行一个 Worker，实际并发以 Runtime 进程、Attempt 与 Session 观测为准。计划升级或停止前先用 Runtime `upgrade drain`、`upgrade wait` 排空；systemd 配置不以超时强杀代替排空。

## 交付验证

首轮已验证全新数据目录、固定镜像启动、精简环境诊断、访问认证、Console 资源和独立重启后的数据挂载。Worker unit 通过 systemd 静态校验，真实 Cursor 执行复用既有开发环境证据。清洁安装中的任务、升级回退和恢复组合资格由 D01-1.02 承接。

[固定镜像切换证据](../../docs/design/evidence/unattended-cutover-20261126.json) 已覆盖干净 Compose 实例中的三路 ACP 原生任务、自然排空、安装备份校验、候选启动失败回退、实际旧/新版本切换，以及恢复后排队任务的原身份和成果校验。排空期间新提交明确返回 `RUNTIME_DRAINING`，原排队任务保持身份。模型输出为合成协议端点；跨 Harness、schema 变更和物理跨主机资格分别保留。

升级顺序为：固定候选镜像摘要 → 调用 `/api/os/upgrade/drain` → 等待原任务自然结束 → `settle-services` 并确认 `ready_for_cutover` → 使用 `installation-backup` 和 `installation-verify` 保存并校验备份 → 切换 `.env` 中镜像并执行 Compose `up -d --wait` → 检查历史、产物、配置及排队任务 → 携当前 `revision`、`kernel_revision` 调用 `resume`。候选启动失败时先恢复原固定镜像，保持 draining，完成诊断后再恢复领取。查询超时、活跃任务或未知执行状态均不允许进入镜像切换。

机制依据：[Compose 启动依赖](https://docs.docker.com/compose/how-tos/startup-order/)、[Docker 自动重启](https://docs.docker.com/engine/containers/start-containers-automatically/)、[Compose 环境变量优先级](https://docs.docker.com/compose/how-tos/environment-variables/envvars/)。
