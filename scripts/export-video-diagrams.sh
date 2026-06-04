#!/usr/bin/env bash
# 将 docs/video-assets/mermaid/*.mmd 导出为 PNG（1920×1080）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MMD_DIR="$ROOT/docs/video-assets/mermaid"
OUT_DIR="$ROOT/docs/video-assets/png"
mkdir -p "$OUT_DIR"

run_mmdc() {
  if command -v mmdc >/dev/null 2>&1; then
    mmdc "$@"
  else
    npx -y -p @mermaid-js/mermaid-cli mmdc "$@"
  fi
}

for f in "$MMD_DIR"/*.mmd; do
  base="$(basename "$f" .mmd)"
  echo "→ $base.png"
  run_mmdc -i "$f" -o "$OUT_DIR/$base.png" -b transparent -w 1920 -H 1080
done

echo "完成: $OUT_DIR"
