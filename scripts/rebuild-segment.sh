#!/usr/bin/env bash
# 仅重建指定分段，例如: ./scripts/rebuild-segment.sh 01-title
set -euo pipefail
ID="${1:?用法: rebuild-segment.sh 01-title}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export ONLY_SEGMENT="$ID"
"$ROOT/scripts/assemble-video-segments.sh"
