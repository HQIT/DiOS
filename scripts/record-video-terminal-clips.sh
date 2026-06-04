#!/usr/bin/env bash
# S-12 CLI、S-13 Docker：用 HTML 终端页 + Playwright 录短视频
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/docs/video-assets/recordings"
TMP="$ROOT/docs/video-assets/.tmp-terminal"
mkdir -p "$OUT" "$TMP"

API="${DIOS_API:-http://127.0.0.1:8081}"
export DIOS_API="$API"

render_html() {
  local title="$1" outfile="$2" body="$3"
  python3 - "$title" "$outfile" "$body" <<'PY'
import html, sys
title, path, body = sys.argv[1], sys.argv[2], sys.argv[3]
doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#0f1117; color:#e4e6eb; font:16px/1.5 ui-monospace,Menlo,Consolas,monospace;
    padding:48px; width:1920px; height:1080px; }}
  h1 {{ color:#6c8eef; font-size:20px; margin-bottom:24px; font-family:system-ui,sans-serif; }}
  pre {{ white-space:pre-wrap; word-break:break-word; font-size:15px; }}
  .prompt {{ color:#48c78e; }}
</style></head><body>
<h1>{html.escape(title)}</h1>
<pre class="prompt">$ </pre><pre>{html.escape(body)}</pre>
</body></html>"""
open(path, "w", encoding="utf-8").write(doc)
PY
}

echo "→ 采集 CLI 输出"
CLI_OUT="$TMP/dios-agent-list.txt"
{
  echo "DIOS_API=$API dios agent list"
  echo ""
  python3 "$ROOT/cli/dios" agent list 2>&1 | head -40
} > "$CLI_OUT" || true

echo "→ 采集 docker ps"
DOCKER_OUT="$TMP/docker-ps.txt"
{
  echo "docker ps --filter name=diagent --filter name=dios"
  echo ""
  docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null \
    | head -20 || echo "(docker 不可用或无匹配容器)"
} > "$DOCKER_OUT"

render_html "dios CLI — agent list" "$TMP/s12.html" "$(cat "$CLI_OUT")"
render_html "Docker — Agent 任务容器" "$TMP/s13.html" "$(cat "$DOCKER_OUT")"

record_html() {
  local html="$1" out="$2"
  npx -y playwright@1.49.1 screenshot \
    --viewport-size=1920,1080 \
    --wait-for-timeout=500 \
    "file://$html" "$OUT/${out}.png"
}

record_html "$TMP/s12.html" "S-12-dios-cli"
record_html "$TMP/s13.html" "S-13-docker-ps"

# 用 ffmpeg 把静帧做成 5s 视频（若已安装）
if command -v ffmpeg >/dev/null; then
  for id in S-12-dios-cli S-13-docker-ps; do
    ffmpeg -y -loop 1 -i "$OUT/${id}.png" -c:v libx264 -t 5 -pix_fmt yuv420p \
      -vf "scale=1920:1080" "$OUT/${id}.webm" 2>/dev/null \
      || ffmpeg -y -loop 1 -i "$OUT/${id}.png" -c:v libvpx-vp9 -t 5 "$OUT/${id}.webm" 2>/dev/null \
      || true
  done
fi

echo "完成: $OUT"
