# 工作日报 2026-07-21（DiOS / DiAgent）

## 今日目标

按 `event-gateway-design` 附录落地 **通用** Remote MCP 配置下发；澄清 DiAgent `tool_selection` 契约，使 Chat 绑定 MCP 后工具可用。不写产品专用分支逻辑。

## 今日完成

### DiAgent（`dev/tool-selection-mcp-null-contract`）

- `get_selected_tool_ids`：区分 `tool_selection is None` 与显式 `tool_ids=[]`
- `agent.py`：`None` → 加载全部已连接 MCP；`[]` → 无工具；非空 → 按名过滤 + shell/publish_event
- 基线：`chore/llm-factory-adjustments`（对齐现用 pin），语义兼容曾有的 service-mode「未选=可用 MCP」

### DiOS（`dev/remote-mcp-transport`）

- `McpServer` 增加 `transport` / `url`；CRUD 与 SQLite 启动迁移
- 新增 `mcp_config.build_diagent_mcp_servers`：输出 MultiServerMCPClient **dict**
- `agent_runtime` / A2A 写出标准配置（stdio / http / sse）；`HEADER_*` / `Authorization` → headers
- Chat：存在 `mcp_server_ids` 时不传 `tool_selection`
- 文档附录状态更新为后端已实现、Console UI 仍待

## 下一步

1. Console 远程 MCP 可填 URL / Registry Remote 可添加
2. PR 评审合入 DiOS / DiAgent 主线
