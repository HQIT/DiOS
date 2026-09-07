#!/usr/bin/env bash
# 在 demogo 服务器执行：向 gdw-3d-bin-packing 的 Caddy 追加 /dios 路由
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GDW_DIR="${GDW_DIR:-/home/ubuntu/gdw-3d-bin-packing}"
CADDYFILE="${GDW_DIR}/deploy/Caddyfile"
MARKER="# === DiOS handle_path (auto) ==="
COMPOSE_DIR="$GDW_DIR"

if [[ ! -f "$CADDYFILE" ]]; then
  echo "未找到 $CADDYFILE"
  exit 1
fi

if grep -qF "$MARKER" "$CADDYFILE"; then
  echo "DiOS 路由已存在，跳过"
else
  cp "$CADDYFILE" "${CADDYFILE}.bak.$(date +%Y%m%d%H%M%S)"
  awk -v block="$(cat "$SCRIPT_DIR/gdw-caddy-dios.snippet")" '
    /^[[:space:]]*handle[[:space:]]*\{/ && !done && prev !~ /handle_path/ {
      print block
      done=1
    }
    { print }
  ' "$CADDYFILE" > "${CADDYFILE}.new"
  mv "${CADDYFILE}.new" "$CADDYFILE"
fi

cd "$COMPOSE_DIR"
docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile

echo "完成: curl -sI https://www.demogo.work/dios/ | head -5"
