# DeepSeek Harness execution profile

`dios-dsh-review --config /opt/dios/acp.json --required-tool repository_search` runs the same static review application through DSH ACP. DiOS SDK 0.1.3 supplies metadata normalization; Runtime and E2AG own authorization, approval, tool dispatch and result acceptance.

The qualified combination is `@deepseek-ai/dsh@0.2.2-rc.5`, Node 24 and `agent-client-protocol==1.0.0`. Install DSH under `/opt/dsh` in a separately built application image. Pin the resolved npm dependency lock and final image digest in the deployment record. The ordinary static-review image contains the Python application; the DSH distribution is an additional runtime dependency.

Configure the operator-owned `restricted.patch.yml` with a provider URL, model and supported limits. The example uses DSH's official `dsh-llm-pi-ai` OpenAI-compatible adapter. Mount it read-only at `/opt/dsh-profile.yml`, and mount `acp.json` at `/opt/dios/acp.json`. Models and credentials are independent of the Harness choice.

Supply the model key through the existing versioned DiOS Secret provider at `/run/secrets/model-api-key`. The mounted file must be readable by the workload UID; keep its host parent directory restricted to the Worker account. Register secret reference, version and digest in the E2AG work policy. Never place a key in either example file.

Set these operator-owned environment values:

```text
HOME=/tmp
DSH_HOME=/tmp/dsh
NARB_DISABLE_NATIVE_CACHE=1
DSH_PERMISSION_MODE=read-only
DSH_TELEMETRY_DISABLED=1
```

`NARB_DISABLE_NATIVE_CACHE=1` uses the native loader's supported switch to load installed libraries directly, so `/tmp` can retain `noexec`. Run as a non-root user with a read-only root filesystem, bounded tmpfs, memory/CPU/process limits and no customer repository mount. Configure model egress explicitly for the deployment.

The patch disables native file, shell, web, plugin, skills, subagent and autonomous task tools, as well as telemetry. DiOS supplies its controlled MCP server through `session/new`. Register `mcp__dios__repository_search` as the alias for the governed `repository_search` tool. DSH's permission callback only allows forwarding to this proxy; E2AG still decides the actual invocation.

The bridge closes sessions when the peer advertises that capability. DSH's cross-process `session/resume` does not replay history. Active-session reattachment is a separate qualification from a new isolated Worker attempt.

Real model, approval, denial, cancellation and revocation evidence: [DSH integration](../../../../docs/design/dsh-acp-56-58.md).
