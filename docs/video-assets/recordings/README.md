# 录屏 / 终端素材

| 文件 | ID | 说明 |
|------|-----|------|
| S-07-console-event-logs.webm | S-07 | Events 日志展开 + 活动总览 |
| S-08-app-shell-switch.webm | S-08 | Console ↔ Chat 切换 |
| S-10-chat-streaming.webm | S-10 | SimpleChatBot 发送消息（约 12s） |
| S-11-chat-sessions.webm | S-11 | 会话切换或新建会话 |
| S-12-dios-cli.png / .webm | S-12 | `dios agent list` 终端样式（真实输出） |
| S-13-docker-ps.png / .webm | S-13 | `docker ps` 过滤 diagent/dios |

## 重新录制

```bash
# 浏览器（需 DiOS 前端可访问，默认 3001）
node scripts/record-video-screencasts.mjs

# CLI / Docker 终端页
./scripts/record-video-terminal-clips.sh
```

环境变量：`DIOS_UI_BASE`、`DIOS_API`
