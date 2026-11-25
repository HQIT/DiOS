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

`dios_sdk.acp_sessions.AcpSessionInspector` inspects retained conversations in a
fresh isolated process. It negotiates `load` versus `resume`, keeps bounded history
separate from Worker output, denies client operations, and never sends a prompt.
The caller must authorize access to the retained state and isolate the process
before inspection. This API does not grant execution authority or reconnect an
in-flight turn. [Session recovery contract](../../docs/design/acp-session-recovery-56-58.md).

Build/install and usage: [application contracts](../../docs/design/application-packages-47.md).

Persist the logical idempotency key before submitting. Observation timeout does not cancel or resubmit a task. Approvals remain controlled by DiOS.

Authenticated delegation: `TaskClient.issue_delegation`, `delegation`, and
`revoke_delegation` use the same authenticated Runtime transport. Issuance requires
an existing parent and an explicit profile grant; the server resolves the issuer.
See [issuer identity and revocation](../../docs/design/delegation-identity-35.md).

SDK 0.1.6 supports `delegation_ref=...` on `submit` and `submit_input`.
Use the recipient's authenticated client and retain both the reference and the
idempotency key on retries. The server checks subject, actor, the configured
ancestor and the live authorization chain in the admission transaction.
See [delegated task admission](../../docs/design/delegated-task-admission-35.md).
