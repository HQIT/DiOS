# DiOS 部署到 demogo.work（路径 `/dios/`）

与 AKT `/akt/` 相同：DiOS 容器只监听 `127.0.0.1`，由 gdw Caddy `handle_path` 反代，不改动主站 `handle` 逻辑。

## 环境变量

```bash
export DIOS_ACCESS_TOKEN='<随机强密码>'
```

## 启动

```bash
export DOCKER_BUILDKIT=1
# 国内机建议加镜像（显著加速 pip）
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

docker compose -p dios \
  -f docker-compose.build.yml \
  -f docker-compose.prod.yml \
  build --build-arg PIP_INDEX_URL="$PIP_INDEX_URL"

docker compose -p dios \
  -f docker-compose.build.yml \
  -f docker-compose.prod.yml \
  up -d
```

**进一步加速：** 若仅改业务代码、依赖未变，可基于已构建镜像 `dios-backend:local`，第二次构建会命中 BuildKit pip 缓存层，通常几十秒内完成。

## Caddy 路由

```bash
bash deploy/install-dios-proxy.sh
```

## 验证

```bash
curl -sI https://www.demogo.work/dios/ | head -5
curl -s -o /dev/null -w "%{http_code}\n" -H "X-DiOS-Access-Token: $DIOS_ACCESS_TOKEN" \
  https://www.demogo.work/dios/api/os/models
```

## iframe

演示中心 `demo-embed.html?id=dios` → `/dios/`（需先部署 gdw 静态页更新）。
