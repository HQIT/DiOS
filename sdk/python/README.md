# DiOS Python SDK

Versioned task, governed tool and ACP/MCP clients. Python >=3.11; no server dependencies.

The core HTTP/JSONL client has no third-party runtime dependencies. Install
`dios-sdk[acp]` for ACP workloads; this extra pins the official
`agent-client-protocol==1.0.0` SDK. The application bundle includes its resolved
dependencies for offline Linux Python 3.12 image builds.

ACP cancellation retains the original prompt until its `cancelled` response or
the bounded shutdown deadline. Pending permissions receive a cancelled outcome;
partial output is retained. Protocol acknowledgement and process exit are
separate evidence. [Cancellation contract](../../docs/design/acp-cancellation-56-58.md).

Build/install and usage: [application contracts](../../docs/design/application-packages-47.md).

Persist the logical idempotency key before submitting. Observation timeout does not cancel or resubmit a task. Approvals remain controlled by DiOS.

Authenticated delegation: `TaskClient.issue_delegation`, `delegation`, and
`revoke_delegation` use the same authenticated Runtime transport. Issuance requires
an existing parent and an explicit profile grant; the server resolves the issuer.
See [issuer identity and revocation](../../docs/design/delegation-identity-35.md).
