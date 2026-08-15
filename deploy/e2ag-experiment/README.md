# demogo E2AG isolated experiment backend

This deployment is intentionally separate from the public `/dios/` instance.
It binds only to `127.0.0.1:18081`, uses a separate Docker volume and
workspace, and joins the same service network as `git-perf` so that the MCP
side effect remains external to DiOS.

The experiment copies the existing DiOS database once before the first start.
That preserves the Connector, Agent, model, subscription and MCP configuration
without sharing subsequent event, task, grant or audit rows. Never copy the
database while the source SQLite connection is being written; use SQLite's
online backup API or stop the source briefly.

Build and start on demogo:

```bash
cd /opt/dios-e2ag
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
docker compose -p dios-e2ag \
  -f deploy/e2ag-experiment/docker-compose.demogo.yml \
  build
docker compose -p dios-e2ag \
  -f deploy/e2ag-experiment/docker-compose.demogo.yml \
  up -d
```

Verify from the server:

```bash
curl -s -H "X-DiOS-Access-Token: $DIOS_ACCESS_TOKEN" \
  http://127.0.0.1:18081/api/os/events?limit=1
```

The backend is not exposed through Caddy. Paired events are replayed from the
server against the loopback webhook endpoint, while `git-perf` remains the real
remote MCP service on `http://git-perf:8090/mcp`.

The one-shot task image must already exist on demogo as
`dios-diagent-task:latest`; the backend deliberately fails fast instead of
implicitly pulling a missing image. Use `docker save`/`docker load` only when
the local image is absent or an explicit compatibility check fails. Task
containers join `gdw-3d-bin-packing_default`, reach the governance proxy at
`http://dios-e2ag-backend:8000`, and use the shared `skills/` and `cli/`
directories under `/opt/dios-e2ag/workspace`.

The MCP proxy implements the complete Streamable HTTP path used by DiAgent:
POST requests, the optional SSE GET channel, DELETE session cleanup, redirect
following, and SSE-preserving `tools/list` filtering. A terminal task must
produce a matching `a2a_task/completed|failed` audit entry, a revoked grant,
and (when emitted by DiAgent) a collected `task_result.md` artifact.

The external negative check is a system-level governance-consistency
regression, not model-driven scenario generation. Its tool name and arguments are fixed in
`experiments/e2ag/external_governance_regression.py`. Any future replay must
invoke that vector through a deterministic test harness; a model must not be
asked to generate, select, or advance the negative steps.
