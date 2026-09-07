# 工作日报 2026-08-12

## 今日目标

本地只做开发，用 demogo 上的 DiOS 做联调测试；跑通「GitHub push → DiOS webhook → Reviewer → 远程 MCP → git-perf」而不再依赖 cloudflared 透传本机。

## 今日完成

### 1. demogo DiOS 升级与网络

- `/opt/dios` 同步含远程 MCP 的代码并重建 `dios-backend` / `dios-frontend`
- 构建 `dios-diagent-task:latest`（国内 apt/pip 镜像）
- `docker-compose.prod.yml`：backend 加入 `gdw-3d-bin-packing_default`，设置 `DIOS_DOCKER_NETWORK` 与本地 DiAgent 镜像名
- `docker_runner`：task 容器同样加入 `DIOS_DOCKER_NETWORK`，否则 Agent 访问不到 `git-perf`
- 验证：backend / task 容器可访问 `http://git-perf:8090`

### 2. demogo 运行时配置（库内，非代码）

- Connector：`git_webhook`（GitHub secret 与 webhook 对齐）
- MCP：`git-perf` → `http://git-perf:8090/mcp`（streamable_http）
- Agent：`CodeDev-Reviewer`（task）绑定 MCP，提示词沿用 push 评审 + `record_push` / `record_review`
- 订阅：`github/SUGE2016/qame` + `git.push`（曾误配 `HQIT/qame`，已改正）
- LLM：登记可用模型供 Reviewer 推理

### 3. 端到端验收

- 公网 webhook：`https://www.demogo.work/dios/api/os/events/webhook/github`（免 Access Token）
- 真实 push：`github/SUGE2016/qame` → 匹配 Reviewer → task 启动 → git-perf `/api/pushes` 出现真实记录
- 演示入口：`https://www.demogo.work/demo-embed.html?id=dios`

### 4. Console Access Token 误放行修复

- 现象：嵌入页能进 Console，但 Agents 等为空
- 根因：`/api/auth/status` 被门禁拦成 401，AccessGate 把「无 `access_token_required`」当成「不需要登录」
- 修复：middleware 公开 `/api/auth/status`；AccessGate 仅在 status 200 且未要求 token 时放行
- 已部署 demogo；需输入 `.env` 中 `DIOS_ACCESS_TOKEN` 后可见数据

### 5. 架构讨论（无代码或已入库）

- Connector 接插：Phase A 契约/registry（昨日已 commit）；与周目标、多租户正交
- Codex 类 Runtime：task 契约接近，缺 Agent 级镜像与 Adapter；真仓库需 Secret/Approval
- 多租户下 webhook secret 全局 map 是致命点；实例级回调 URL 可先做

## 涉及代码（本仓）

| 路径 | 说明 |
|------|------|
| `backend/app/services/docker_runner.py` | task 容器挂 `DIOS_DOCKER_NETWORK` |
| `docker-compose.prod.yml` | demogo 网 + DiAgent 镜像环境变量 |
| `backend/app/middleware/access_token.py` | 公开 `/api/auth/status` |
| `frontend/src/components/AccessGate.tsx` | status 非 200 不误放行 |
| `.gitignore` | 忽略 demogo 配置导出临时文件 |

（Connector 插件包与 ADR 0001 / ROADMAP 更新见 08-11 提交，不重复计入今日 diff。）

## 下一步

1. 将本日报与上述小修提交/push，保持 demogo 与仓库一致
2. 视需要固化 demogo 配置脚本（替代手工 API）
3. Connector Phase B（API 白名单改 registry）可排期，不阻塞当前演示

## 备注

- git-perf 由独立仓在 demogo 部署；DiOS 只登记可达 MCP URL
- Access Token 仅用于 Console/API，不挡 webhook
