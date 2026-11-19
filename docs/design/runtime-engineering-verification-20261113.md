# Runtime 工程验证与开发环境

日期：2026-11-13。范围：#30/#33/#34/#56 累积开发改动，基于 c705de0 的当前工作区。

## 验证结论

后端运行 293 项测试，292 项通过，1 项跳过，失败和错误均为 0。启用了隔离 PostgreSQL 16、真实 Docker Worker、SQLite 持久化、工作授权、工具审批、回执恢复及实际 mTLS 连接；唯一跳过项是在 PostgreSQL 测试类中运行 SQLite 专用备份。

三路并发由三个独立 Worker PID、容器 PID 和会话 ID 证实。完整证据见 [后端结果](evidence/runtime-engineering-20261113.json)。

前端在 Node 25.1.0 下完成 TypeScript/Vite 生产构建，9 项工作台测试通过。浏览器完成发起样例评审、等待审批、独立凭据批准、真实 Docker 执行、报告预览及 Markdown 下载；中文与源码引用保持完整。另覆盖无凭据访问、错误审批凭据、拒绝调用、取消后进程退出、前一任务产物清空、服务失败提示、共享导航及 390px 移动视口。见 [主流程](evidence/runtime-browser-20261113.json)、[错误与取消流程](evidence/runtime-browser-errors-20261113.json)。

## 修复项

- 测试报告将跳过项中的 TestCase 对象转换为测试 ID 与原因，恢复 JSON 导出。
- 导航在窄屏下独立横向滚动，页面不再被导航撑宽。
- 任务详情更新同步至列表；旧列表响应不能覆盖新版本状态。
- 列表服务失败提示增加 alert 语义。
- 前端提供 .nvmrc；Docker 构建上下文排除本地虚拟环境和 SQLite 文件。

## 开发环境

WSL 原工作区运行开发 API、前端、独立 Worker 和合成 MCP 工具服务。入口：`http://localhost:13082/dios/#console/tasks`。独立数据和日志在 `workspace/validation-20261113`；操作员及独立审批测试凭据在该目录的私有 `tokens.json`。原业务库、历史记录和现有评审执行机保持原状。

开发执行镜像 `dios-backend:validation-20261113` 使用已有依赖镜像加当前源码构建，`pip check` 通过。标准 Dockerfile 首次下载 PyPI 元数据遭遇截断；本次复用了现有依赖层。无缓存重建及依赖版本固定继续属于交付验证。

合成 Agent 使用真实 Docker 进程、Worker、E2AG 和工具通道，评审输入及回答为测试样例。Cursor/Codex/CC/DSH 真实模型端点、两台物理主机、Windows 后台执行及生产迁移仍按 #58/#60 专项验证。mTLS 网络测试在本机回环地址执行。

## 复现入口与责任

后端：`scripts/check_runtime_foundation.py`，通过 `DIOS_TEST_POSTGRES_URL` 和 `DIOS_TEST_DOCKER=1` 启用隔离基础设施。前端：`npm run build`、`npm run test:workbench`。浏览器：`scripts/check_runtime_console.mjs`、`scripts/check_runtime_console_errors.mjs`，要求开发验证环境运行及私有凭据目录存在。

构建、自动化测试、开发部署和技术验证由开发方持续完成；用户参与产品/业务验收。回上海后的验收安排不暂停工程验证。#33/#34 仍保持 In Progress，短时凭据自动签发/续期、完整跨主机控制面和真实 Agent 资格验证继续推进。
