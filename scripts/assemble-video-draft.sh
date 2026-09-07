#!/usr/bin/env bash
# 将 video-assets 合成为初版预览片（旁白 + 分镜画面）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ASSETS="$ROOT/docs/video-assets"
WORK="$ASSETS/.assemble-work"
OUT="$ASSETS/DiOS-intro-draft.mp4"
AUDIO="$ASSETS/audio/AU-01-narration.mp3"
SUBS="$ASSETS/subtitles/video-intro-zh.srt"

rm -rf "$WORK"
mkdir -p "$WORK/segments"

VF='scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=#0f1117,format=yuv420p'
FPS=30

audio_dur() {
  ffprobe -v error -show_entries format=duration -of csv=p=0 "$AUDIO"
}

# SVG -> PNG
svg_png() {
  local svg="$1" png="$2"
  npx -y playwright@1.49.1 screenshot \
    --viewport-size=1920,1080 \
    "file://$svg" "$png" 2>/dev/null
}

img_seg() {
  local id="$1" img="$2" dur="$3"
  local out="$WORK/segments/${id}.mp4"
  echo "  静图 $id (${dur}s)"
  ffmpeg -y -loop 1 -i "$img" -t "$dur" -vf "$VF" -r "$FPS" \
    -c:v libx264 -preset fast -crf 23 -an "$out" 2>/dev/null
}

vid_seg() {
  local id="$1" vid="$2" dur="$3"
  local out="$WORK/segments/${id}.mp4"
  echo "  录屏 $id (${dur}s, 截断)"
  ffmpeg -y -i "$vid" -t "$dur" -vf "$VF" -r "$FPS" \
    -c:v libx264 -preset fast -crf 23 -an "$out" 2>/dev/null
}

echo "→ 转换 SVG"
for s in title-dios tagline-agent-os architecture-layers nana-concept \
  service-vs-task os-analogy-table ending-card; do
  svg_png "$ASSETS/svg/${s}.svg" "$WORK/${s}.png"
done

echo "→ 生成片段"
# 时长合计约 236s，与旁白对齐
img_seg 01-title           "$WORK/title-dios.png"           18
img_seg 02-architecture    "$WORK/architecture-layers.png"  17
img_seg 03-nana            "$WORK/nana-concept.png"         16
img_seg 04-agents          "$ASSETS/screenshots/S-01-console-agents.png"     10
img_seg 05-models          "$ASSETS/screenshots/S-02-console-models.png"      8
img_seg 06-mcp             "$ASSETS/screenshots/S-03-console-mcp.png"         7
img_seg 07-skills          "$ASSETS/screenshots/S-04-console-skills.png"      7
img_seg 08-connectors      "$ASSETS/screenshots/S-05-console-connectors.png"  8
img_seg 09-events          "$ASSETS/screenshots/S-06-console-events.png"      9
vid_seg 10-event-logs      "$ASSETS/recordings/S-07-console-event-logs.webm" 11
img_seg 11-modes           "$WORK/service-vs-task.png"     14
vid_seg 12-chat            "$ASSETS/recordings/S-10-chat-streaming.webm"     16
img_seg 13-git-flow        "$ASSETS/png/git-collab-sequence.png"             26
img_seg 14-tagline         "$WORK/tagline-agent-os.png"    10
img_seg 15-email-flow      "$ASSETS/png/email-collab-flow.png"                9
vid_seg 16-shell-switch    "$ASSETS/recordings/S-08-app-shell-switch.webm"   10
vid_seg 17-cli             "$ASSETS/recordings/S-12-dios-cli.webm"            7
img_seg 18-os-analogy      "$WORK/os-analogy-table.png"    20
img_seg 19-ending          "$WORK/ending-card.png"         17

echo "→ 拼接视频轨"
CONCAT="$WORK/concat.txt"
: > "$CONCAT"
for f in "$WORK"/segments/*.mp4; do
  printf "file '%s'\n" "$f" >> "$CONCAT"
done
ffmpeg -y -f concat -safe 0 -i "$CONCAT" -c copy "$WORK/video-only.mp4" 2>/dev/null

DUR="$(audio_dur)"
VID_DUR="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$WORK/video-only.mp4")"
PAD="$(python3 -c "print(max(0, float('$DUR') - float('$VID_DUR')))")"
echo "→ 混合旁白 (音频 ${DUR}s, 画面 ${VID_DUR}s, 补足 ${PAD}s)"
if python3 -c "exit(0 if float('$PAD') > 0.5 else 1)"; then
  VF_PAD="${VF},tpad=stop_mode=clone:stop_duration=${PAD}"
  ffmpeg -y -i "$WORK/video-only.mp4" -vf "$VF_PAD" -an "$WORK/video-padded.mp4" 2>/dev/null
  VIN="$WORK/video-padded.mp4"
else
  VIN="$WORK/video-only.mp4"
fi
ffmpeg -y -i "$VIN" -i "$AUDIO" \
  -map 0:v -map 1:a \
  -t "$DUR" \
  -c:v libx264 -preset fast -crf 22 \
  -c:a aac -b:a 192k \
  "$WORK/muxed.mp4" 2>/dev/null

# 可选烧录字幕（若字幕与旁白不完全对齐可关掉）
if [[ -f "$SUBS" ]]; then
  echo "→ 烧录字幕"
  ffmpeg -y -i "$WORK/muxed.mp4" \
    -vf "subtitles=${SUBS}:force_style='FontName=Noto Sans CJK SC,FontSize=22,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,Outline=2,MarginV=40'" \
    -c:v libx264 -preset fast -crf 22 -c:a copy \
    "$OUT" 2>/dev/null || cp "$WORK/muxed.mp4" "$OUT"
else
  cp "$WORK/muxed.mp4" "$OUT"
fi

echo ""
echo "完成: $OUT"
ls -lh "$OUT"
ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 "$OUT"
