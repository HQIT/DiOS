#!/usr/bin/env bash
# 在逐段确认后，按 manifest 顺序拼接总片（不再重新生成各段）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SEG="$ROOT/docs/video-assets/segments"
OUT="$ROOT/docs/video-assets/DiOS-intro-from-segments.mp4"
LIST="$SEG/.concat.txt"

python3 - "$ROOT" "$SEG" > "$LIST" <<'PY'
import json, sys
from pathlib import Path
root, seg = Path(sys.argv[1]), Path(sys.argv[2])
m = json.loads((seg / "manifest.json").read_text(encoding="utf-8"))
for s in m["segments"]:
    print(f"file '{(root / s['file']).resolve()}'")
PY

ffmpeg -y -f concat -safe 0 -i "$LIST" -c copy "$OUT"
echo "完成: $OUT"
ls -lh "$OUT"
ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT"
