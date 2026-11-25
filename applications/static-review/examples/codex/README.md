# Codex ACP execution profile

`dios-codex-review --config /opt/dios/acp.json --required-tool repository_search` runs the independent review application with Codex ACP. The qualified versions are Adapter `@agentclientprotocol/codex-acp@2.11.0`, native `@openai/codex@0.172.0`, Node 24, DiOS SDK 0.1.4 and static-review 0.1.3.

Install the two npm packages under `/opt/codex` in an independently built application image. Pin the resolved dependency lock and image digest. Mount `acp.json` read-only at `/opt/dios/acp.json`. Pass the operator-owned contents of `restricted-config.json` as `CODEX_CONFIG` and set:

```text
HOME=/tmp
CODEX_HOME=/tmp
NO_BROWSER=1
INITIAL_AGENT_MODE=read-only
```

`CODEX_HOME` must exist and be writable. `/tmp` is private to the workload and may retain `noexec`. Run as a non-root user with a read-only root filesystem, bounded tmpfs, CPU/memory/process limits, and operator-configured model egress.

Use DiOS's versioned Secret provider for an authorized account. The qualified profile mounts the existing Codex authentication file read-only at `/tmp/auth.json`; it mounts no other local Codex state. The host secret parent directory is restricted to the Worker account, while the mounted file is readable by the workload UID. Register its reference/version/digest and profile in E2AG. Credential refresh/rotation remains an operator/provider responsibility.

For an API-key provider, configure the official Codex `model_providers` and its `env_key`, and use `credential_env_files` to load that environment value from a registered secret file. The endpoint needs the complete request dialect used by the pinned Codex version. ECNU Max returned valid text and flat function calls in this qualification, but its Responses namespace tool request returned HTTP 500; that combination is not qualified for Codex governed tools. DSH + ECNU Max is qualified separately.

The restricted configuration disables shell, patch, web, image, plugin, application connector and subagent features. The controlled MCP proxy is supplied through `session/new`; register alias `mcp__dios__repository_search`. Actual dispatch and result acceptance remain in E2AG. Additional native paths require separate qualification.

The Adapter advertises `session/close`; the bridge confirms it, then bounds process teardown. In the qualified version the Adapter still required termination after EOF. A complete validated report is accepted only under the explicit `accept_complete_on_error` profile policy; the nonzero exit and raw output remain in the exit receipt. Cancellation/revocation never produces a successful result frame.

Evidence and remaining qualification: [Codex integration](../../../../docs/design/codex-acp-56-58.md).
