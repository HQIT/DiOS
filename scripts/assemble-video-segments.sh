#!/usr/bin/env bash
# 逐段生成可独立预览的 mp4（画面 + 该段旁白），不拼接总片
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ASSETS="$ROOT/docs/video-assets"
SEG="$ASSETS/segments"
WORK="$ASSETS/.assemble-segments"
NAR="$SEG/narration"
VF='scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=#0f1117,format=yuv420p'
FPS=30

# id|type|visual|fallback_sec
SEGMENTS=(
  "01-title|img|$WORK/title-dios.png|18"
  "02-architecture|img|$WORK/architecture-layers.png|17"
  "03-nana|img|$WORK/nana-concept.png|16"
  "04-agents|img|$ASSETS/screenshots/S-01-console-agents.png|10"
  "05-models|img|$ASSETS/screenshots/S-02-console-models.png|8"
  "06-mcp|img|$ASSETS/screenshots/S-03-console-mcp.png|7"
  "07-skills|img|$ASSETS/screenshots/S-04-console-skills.png|7"
  "08-connectors|img|$ASSETS/screenshots/S-05-console-connectors.png|8"
  "09-events|img|$ASSETS/screenshots/S-06-console-events.png|9"
  "10-event-logs|vid|$ASSETS/recordings/S-07-console-event-logs.webm|11"
  "11-modes|img|$WORK/service-vs-task.png|14"
  "12-chat|vid|$ASSETS/recordings/S-10-chat-streaming.webm|16"
  "13-git-flow|img|$ASSETS/png/git-collab-sequence.png|26"
  "14-tagline|img|$WORK/tagline-agent-os.png|10"
  "15-email-flow|img|$ASSETS/png/email-collab-flow.png|9"
  "16-shell-switch|vid|$ASSETS/recordings/S-08-app-shell-switch.webm|10"
  "17-cli|vid|$ASSETS/recordings/S-12-dios-cli.webm|7"
  "18-os-analogy|img|$WORK/os-analogy-table.png|20"
  "19-ending|img|$WORK/ending-card.png|17"
)

audio_dur() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1"; }

svg_png() {
  npx -y playwright@1.49.1 screenshot --viewport-size=1920,1080 \
    "file://$1" "$2" 2>/dev/null
}

build_video() {
  local id="$1" typ="$2" vis="$3" dur="$4" vout="$5"
  if [[ "$typ" == "img" ]]; then
    ffmpeg -y -loop 1 -i "$vis" -t "$dur" -vf "$VF" -r "$FPS" \
      -c:v libx264 -preset fast -crf 23 -an "$vout" 2>/dev/null
  else
    ffmpeg -y -i "$vis" -t "$dur" -vf "$VF" -r "$FPS" \
      -c:v libx264 -preset fast -crf 23 -an "$vout" 2>/dev/null
  fi
}

mux_av() {
  local v="$1" a="$2" out="$3" dur="$4"
  ffmpeg -y -i "$v" -i "$a" -map 0:v -map 1:a -t "$dur" \
    -c:v libx264 -preset fast -crf 22 -c:a aac -b:a 192k \
    "$out" 2>/dev/null
}

ONLY="${ONLY_SEGMENT:-}"

if [[ -z "$ONLY" ]]; then
  echo "→ 生成各段 TTS"
  "$ROOT/scripts/.venv-video/bin/python" "$ROOT/scripts/generate-video-segment-tts.py" \
    || python3 "$ROOT/scripts/generate-video-segment-tts.py"
fi

if [[ -n "$ONLY" && -d "$WORK" ]]; then
  : # 保留 WORK 缓存
else
  rm -rf "$WORK"
fi
mkdir -p "$WORK" "$SEG"

if [[ -z "$ONLY" || "$ONLY" != "01-title" ]]; then
  echo "→ 转换 SVG"
  for s in tagline-agent-os architecture-layers nana-concept \
    service-vs-task os-analogy-table ending-card; do
    svg_png "$ASSETS/svg/${s}.svg" "$WORK/${s}.png"
  done
fi
if [[ -z "$ONLY" ]]; then
  svg_png "$ASSETS/svg/title-dios.svg" "$WORK/title-dios.png"
fi

echo "→ 逐段合成"
MANIFEST_JSON="$WORK/manifest-rows.jsonl"
: > "$MANIFEST_JSON"
for row in "${SEGMENTS[@]}"; do
  IFS='|' read -r id typ vis fb <<< "$row"
  if [[ -n "$ONLY" && "$id" != "$ONLY" ]]; then
    continue
  fi
  mp3="$NAR/audio/${id}.mp3"
  txt="$NAR/${id}.txt"
  out="$SEG/${id}.mp4"
  [[ -f "$mp3" ]] || { echo "缺少 $mp3"; exit 1; }
  ADUR="$(audio_dur "$mp3")"
  if [[ "$id" == "01-title" ]]; then
    TITLE_ANIM_MS="$(python3 -c "print(int(float('$ADUR') * 1000 + 800))")"
    export TITLE_ANIM_MS
    echo "  $id 录制标题动效 (${TITLE_ANIM_MS}ms)"
    node "$ROOT/scripts/record-title-animation.mjs"
    vis="$SEG/.cache/01-title-animated.webm"
    typ="vid"
    VDUR="$(python3 -c "print(float('$ADUR') + 0.5)")"
  elif [[ "$typ" == "vid" ]]; then
    VDUR="$(python3 -c "print(min(float('$fb')+2, max(float('$ADUR')+0.5, float('$fb'))))")"
  else
    VDUR="$(python3 -c "print(max(float('$ADUR')+0.3, float('$fb')*0.6))")"
  fi
  vtmp="$WORK/${id}-video.mp4"
  echo "  $id 旁白 ${ADUR}s / 画面 ${VDUR}s"
  build_video "$id" "$typ" "$vis" "$VDUR" "$vtmp"
  mux_av "$vtmp" "$mp3" "$out" "$ADUR"
  FDUR="$(audio_dur "$out")"
  python3 -c "import json; print(json.dumps({'id':'$id','duration_sec':float('$FDUR')}, ensure_ascii=False))" >> "$MANIFEST_JSON"
done

if [[ -n "$ONLY" ]]; then
  echo "仅重建 $ONLY → $SEG/${ONLY}.mp4"
  exit 0
fi

MANIFEST="$SEG/manifest.json"
python3 - "$ROOT" "$MANIFEST_JSON" "$MANIFEST" <<'PY'
import json, subprocess, sys
from pathlib import Path
root, jl, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
titles = {
    "01-title": "开场", "02-architecture": "定位与架构", "03-nana": "NANA 概念",
    "04-agents": "Agent 管理", "05-models": "模型池", "06-mcp": "MCP",
    "07-skills": "Skills", "08-connectors": "事件接入", "09-events": "事件订阅",
    "10-event-logs": "隔离执行与日志", "11-modes": "service / task",
    "12-chat": "Chat 对话", "13-git-flow": "Git 协作场景", "14-tagline": "分工原则",
    "15-email-flow": "邮件场景", "16-shell-switch": "Console / Chat",
    "17-cli": "CLI", "18-os-analogy": "OS 类比", "19-ending": "结尾",
}
rows = []
for line in jl.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    o = json.loads(line)
    e = o["id"]
    rows.append({
        "id": e,
        "title": titles.get(e, e),
        "file": f"segments/{e}.mp4",
        "narration_txt": f"docs/video-assets/segments/narration/{e}.txt",
        "narration_mp3": f"docs/video-assets/segments/narration/audio/{e}.mp3",
        "duration_sec": round(o["duration_sec"], 3),
    })
doc = {"version": 2, "pronunciation": "Di-OS（Di 与 OS 两组）；TTS 语速 +22%", "segments": rows}
out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"manifest: {len(rows)} segments")
PY
echo ""
echo "完成。逐段预览: $SEG/*.mp4"
echo "清单: $SEG/manifest.json"
ls -lh "$SEG"/*.mp4 | head -25
