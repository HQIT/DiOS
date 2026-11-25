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

SDK 0.1.7 adds the running-Agent child task channel:

```python
import os
from dios_sdk.tools import ToolChannelClient

channel = ToolChannelClient(os.environ['DIOS_TOOL_SOCKET'])
child = await channel.submit_child('review', 'Check the registered input',
                                   sample_id='one', key='stable-logical-step')
status = await channel.child(child['id'], wait_seconds=20)
```

Reuse the key after an ambiguous response. Observe the returned Run ID until
terminal; `wait_exhausted` leaves the task running. The SDK defaults to an immediate
read; MCP `dios_task_get` defaults to a 20-second wait, with a 30-second maximum.
MCP advertises `dios_task_submit` and `dios_task_get` only for an enabled child
profile allowlist. Profile paths, parent identity and E2AG authority stay server-owned.
Parent closure requests cancellation of unfinished children. Await needed child
results before completing the parent. [Child task contract](../../docs/design/child-task-channel-35-58.md).

SDK 0.1.8 accepts either a synchronous result decoder or an asynchronous validator
in `dios_sdk.acp.execute` and its Harness wrappers. The validator completes before
the final result frame is emitted. A validation error or cancellation produces no
successful result. Use this to resolve trusted child-task evidence rather than
accepting a model's completion claim. The capability-only
[coordinator example](../../examples/runtime/acp_child_coordinator.py) runs with
Cursor, Codex or DSH and binds its report to the child's verified artifact digest.
