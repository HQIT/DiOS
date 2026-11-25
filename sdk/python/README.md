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
MCP advertises `dios_task_submit`, `dios_task_get` and `dios_task_list` only for an enabled child
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

SDK 0.1.9 adds `parent_run_id` to `issue_delegation`, `submit` and `submit_input`.
The issuer binds a delegation to its live parent execution; the recipient submits
with its own authenticated client, that delegation and the same parent Run ID.
Persist separate issuance and submission idempotency keys. Cross-identity result
return requires explicit `runtime.read_result` permission on the live parent's
governed channel. [Recipient contract](../../docs/design/child-recipients-35.md).

SDK 0.1.10 and later provide `await ToolChannelClient.children(wait_seconds=20)` and MCP
`dios_task_list`. Discovery returns at most 32 direct children of the current
parent Attempt/Session/epoch, with lightweight status and no artifact payload.
The SDK defaults to immediate observation; MCP defaults to a 20-second wait,
bounded to 30 seconds. Cross-identity discovery requires `runtime.read_result`;
an inaccessible child fails the whole query without returning a partial list.
Empty waits still check the live parent and Worker identity. Use the same run ID
with `child()` to read the verified result. The coordinator's `--receive-child`
mode discovers externally admitted children; the application holds separate
issuer/recipient clients and credentials. [Native handoff](../../docs/design/recipient-harnesses-35-58.md).

## Agent-reported usage

SDK 0.1.11 emits optional JSONL `usage` frames from ACP session usage updates and
prompt-response token counters. Each frame binds the session and a stable local
sequence. Context occupancy, cumulative session cost, and reported token counters
remain separate; absent values remain unknown. Unknown metadata is excluded.

Upgrade the DiOS controller and Worker before updating Harness images to this
SDK: earlier JSONL bridges reject the new frame type. Existing Harness images
continue to run and show unknown usage when they provide no reports.
See [usage semantics and recovery](../../docs/design/model-usage-46.md).

## Versioned objects and sharing

`TaskClient.publish_file`, `object_version`, `object_versions`, `link_objects`
and `object_relations` expose immutable file versions and native session/artifact
references. SDK 0.1.13 adds `object_policy`, `set_object_policy` and
`download_object`. Policy changes require operator credentials, an expected
revision and a persistent idempotency key; an empty grant list revokes access.
Downloads verify the complete byte count and SHA-256 digest before returning.
Authorization is exact-version; original task access still applies to artifacts.
Follow `next_after` even when a filtered version page is empty.
See [object access and revocation](../../docs/design/object-access-38.md).

SDK 0.1.14 adds operator-only `object_lifecycle`, `retain_object` and
`delete_object`. Retention and deletion use optimistic revisions and stable
idempotency keys. Deletion blocks reads and preserves a tombstone; the offline
`object-collect` administration CLI performs local physical collection. Explicit derivations become unavailable
when their pinned source expires or is deleted. See
[object lifecycle](../../docs/design/object-lifetime-38.md).
