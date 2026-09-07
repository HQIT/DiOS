# UI 截图素材（S-*）

1920×1080，来源：本地 DiOS `http://127.0.0.1:3001/dios/`（Playwright 抓取）。

## 重新生成

```bash
./scripts/capture-video-screenshots.sh
```

需先启动 DiOS（`docker compose` / dev compose），前端可访问。

## 文件对照

| 文件 | 对应 ID | 页面 |
|------|---------|------|
| S-01-console-agents.png | S-01 | Console → Agents |
| S-02-console-models.png | S-02 | Console → Models |
| S-03-console-mcp.png | S-03 | Console → MCP |
| S-04-console-skills.png | S-04 | Console → Skills |
| S-05-console-connectors.png | S-05 | Console → Connectors |
| S-06-console-events.png | S-06 | Console → Events |
| S-09-chat.png | S-09 | Chat（入口） |

## 仍缺（需录屏或补拍）

| ID | 说明 |
|----|------|
| S-07 | Runs / 任务执行日志（需展开一次 run） |
| S-08 | Console ↔ Chat 切换动效（OBS 录屏更佳） |
| S-10 | Chat 流式对话进行中 |
| S-11 | Chat 会话历史切换 |
| S-12 | `dios` CLI 终端 |
| S-13 | Docker 任务容器列表 |
