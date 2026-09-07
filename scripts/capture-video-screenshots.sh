#!/usr/bin/env bash
# 抓取 DiOS UI 截图到 docs/video-assets/screenshots/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${DIOS_UI_BASE:-http://127.0.0.1:3001/dios}"
OUT="$ROOT/docs/video-assets/screenshots"
mkdir -p "$OUT"

shot() {
  local id="$1" path="$2" file="$3"
  echo "→ $id ($path)"
  npx -y playwright@1.49.1 screenshot \
    --viewport-size=1920,1080 \
    --wait-for-timeout=3000 \
    "$BASE/$path" "$OUT/$file"
}

shot S-01 "#/console/agents" S-01-console-agents.png
shot S-02 "#/console/models" S-02-console-models.png
shot S-03 "#/console/mcp" S-03-console-mcp.png
shot S-04 "#/console/skills" S-04-console-skills.png
shot S-05 "#/console/connectors" S-05-console-connectors.png
shot S-06 "#/console/events" S-06-console-events.png
shot S-09 "#/chat" S-09-chat.png

echo "完成: $OUT"
