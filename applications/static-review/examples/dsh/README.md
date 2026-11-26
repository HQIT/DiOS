# DeepSeek Harness execution profile

`dios-dsh-review --config /opt/dios/acp.json --required-tool repository_search` runs the same static review application through DSH ACP. DiOS SDK supplies metadata normalization; Runtime and E2AG own authorization, approval, tool dispatch and result acceptance. The current qualification uses SDK 0.1.19.

The qualified combination is `@deepseek-ai/dsh@0.2.2-rc.5`, Node 24 and `agent-client-protocol==1.0.0`. Install DSH under `/opt/dsh` in a separately built application image. Pin the resolved npm dependency lock and final image digest in the deployment record. The ordinary static-review image contains the Python application; the DSH distribution is an additional runtime dependency.

Configure the operator-owned `restricted.patch.yml` with a provider URL, model and supported limits. The example uses DSH's official `dsh-llm-pi-ai` OpenAI-compatible adapter. Mount it read-only at `/opt/dsh-profile.yml`, and mount `acp.json` at `/opt/dios/acp.json`. Models and credentials are independent of the Harness choice.

Install [dsh-native-guard.mjs](../../../../examples/runtime/dsh-native-guard.mjs) and its [patch](../../../../examples/runtime/dsh-native-guard.patch.yml) at `/opt/dios/` in the pinned image, or mount both read-only from protected deployment configuration. The final ACP overlay requires the guard service before initializing ACP. The official `tools.guard()` seam rejects every native tool before its body; only the DiOS MCP namespace can continue to the existing E2AG gateway. Keep alternate executors and dynamic plugin loading disabled. DSH read-only sandbox mode alone permits shell execution and is insufficient for static-review admission.

Supply the model key through the existing versioned DiOS Secret provider at `/run/secrets/model-api-key`. The mounted file must be readable by the workload UID; keep its host parent directory restricted to the Worker account. Register secret reference, version and digest in the E2AG work policy. Never place a key in either example file.

Explicitly include the model reference in `tool_channel.worker_secret_refs`; omitted references are not mounted. Tool service credentials remain in the gateway. Use the isolated network and CONNECT allowlist in `deploy/runtime-egress`, with only the configured model hostname and port 443 allowed. Worker environment settings supply the proxy; Docker client proxy defaults are not injected.

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
