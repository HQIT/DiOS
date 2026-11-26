# Linux Secret 注入与轮换

DiOS 按工作授权中的引用、版本和目标路径提供 Secret。执行端只取得 `tool_channel.worker_secret_refs` 中的凭据；工具服务凭据留在网关。普通配置保存引用、摘要及文件路径，不保存 Secret 原文。

## 文件与身份

本地文件和 OpenBao 模板输出均由安装账户拥有。文件不得带执行位、特殊权限位或组/其他用户写权限；符号链接、非普通文件和超过 16 KiB 的内容被拒绝。父目录由同一账户拥有且不可被其他用户写入。

同 UID 的执行容器使用 0400/0600。不同 UID 的容器可使用 0444 文件，但其宿主父目录必须为 0700，容器只挂载这个文件。Docker 文件 bind mount 保留宿主文件 UID 和权限，Worker 启动前检查容器数字 UID/GID 是否可读；容器内始终只读。固定挂载禁止包含整个 Secret 目录。[Docker 文件 Secret 规则](https://docs.docker.com/reference/compose-file/container-spec/)

网关凭据、AppRole SecretID 和 Agent token sink 使用 0600，均不挂载到执行容器。服务安装账户属于可信控制面；Docker 管理权限不授予执行容器。

## 原生 Agent

采用 [OpenBao Agent 模板](https://openbao.org/docs/agent-and-proxy/agent/template/)写入文件、自动认证和续期。[Worker Agent 示例](../../examples/runtime/openbao-worker-agent.example.hcl)固定 KV 版本与输出路径，禁用备份副本，缺失字段会报错。DiOS 每次解析挂载时调用现有 KV v2 适配器复核固定版本和摘要，最多四路请求、整批五秒期限；服务端删除和服务故障不会被本地旧文件掩盖。[KV v2 固定版本接口](https://openbao.org/docs/api/secret/kv/kv-v2/)

1. 由安装账户创建 `/srv/dios/secrets`，权限 0700；配置目录沿用安装时的 0700。
2. 在 Secret 服务创建只读 AppRole，仅授权 `secret/data/model/cursor` 等实际需要的路径。写入/删除、其他路径和管理员策略均不授予 DiOS。
3. 运维私下供给 RoleID、SecretID 和 CA 文件。SecretID 的有效期覆盖约定的无人值守重启窗口；过期前由既有凭据投递机制更新。示例保留 0600 SecretID，以支持 Agent 重启后的原生重新认证。SecretID 到期后停止认证，不能自动扩大权限或续造引导凭据。
4. 将 Agent 示例中的安装根目录、HTTPS 地址、CA、KV 路径和版本替换为部署配置，保存为 `/srv/dios/config/openbao-agent.hcl`。配置中不填 Secret 原文。
5. 使用已审阅的固定镜像摘要安装 Agent，把 `compose.secrets.yaml` 复制到安装根目录。该文件为可选组件；执行以下命令只启动 Agent：

```sh
cd /srv/dios
docker compose --env-file .env -f compose.yaml -f compose.secrets.yaml up -d secret-agent
```

6. 将 [Secret 目录示例](../../examples/runtime/worker-secrets.example.json)保存为受保护配置，填入模板输出文件的实际 SHA-256。`secret_ref.version` 必须为正整数，禁止使用 latest。配置 `DIOS_RUNTIME_SECRETS_FILE` 指向该文件；容器化后端通过上述 Compose 覆盖文件只读挂载同路径 Secret 目录，宿主 Worker 直接读取宿主路径。按已有 drain 流程重启受影响服务，使环境变量生效。

后台服务使用镜像内的 Agent 命令和显式配置路径，不继承终端 PATH。Agent 无 Docker socket、管理员令牌或业务代码挂载。HTTPS/CA 在配置中指定，DiOS 的 Secret HTTP 客户端不读取终端代理环境。

## 正常轮换与故障

正常轮换先创建 KV 新版本并渲染到新路径，校验摘要，再停止领取新任务并等活跃任务自然结束。更新 Secret 目录的版本/路径/摘要、profile Secret 引用及对应 E2AG 授权；有 Revision/Deployment 的安装使用既定发布流程。启动一项受控验证任务后恢复领取。旧文件保留到回滚窗口结束，按留存策略删除；不覆盖正在运行容器挂载的文件。

回滚要求旧版本仍可用，并重新启用对应的 profile/授权版本；运行中的任务不会被改成另一版本。旧请求遇到已切换的 Secret 目录返回 `SECRET_VERSION_CHANGED`。

| 故障 | 执行行为 |
| --- | --- |
| 目录/文件权限不满足要求 | 启动前拒绝，返回权限或路径错误码 |
| 容器 UID/GID 无法读取文件 | `SECRET_RUNTIME_USER_DENIED`，不启动容器 |
| 固定 KV 版本被删除或销毁 | `SECRET_UNAVAILABLE`，即使模板文件仍存在也拒绝 |
| 服务断网、超时、sealed、403 或响应无效 | `SECRET_PROVIDER_UNAVAILABLE`，保留任务和故障记录 |
| 文件或服务返回值与固定摘要不一致 | `SECRET_CONTENT_CHANGED` |
| Secret 服务恢复 | 已配置恢复策略且尝试预算未用尽的任务按退避/单路探测自动恢复，重新核验 E2AG 和固定版本；保留原 Run 与失败尝试 |

运行中的 Worker 沿用约五秒一次的执行授权及挂载复核。凭据轮换不撤销已经发出的外部请求；紧急停用同时撤销 E2AG Work 并停用上游凭据。任务结果接收遵循 E2AG 当前权限，历史结果保留原版本关联。

## 验证

```sh
backend/.venv/bin/python scripts/qualify_secret_injection.py \
  --output workspace/secret-injection.json
```

探针使用回环 13289/13290 的独立 OpenBao Raft 实例、原生 Agent 和合成凭据，覆盖最小权限、后台重新认证、固定版本只读注入、版本轮换、删除拒绝、服务中断及恢复，以及任务/原生产物日志中的凭据原文检查。结束后清理专用容器。原生服务令牌续期及最大期限重认证复用[既有证据](../../docs/design/evidence/managed-secrets-20261124.json)；跨主机与企业 TLS/托管解封属于后续环境资格。
