# DiOS Python SDK

Versioned task, governed tool and ACP/MCP clients. Python >=3.11; no server dependencies.

Build/install and usage: [application contracts](../../docs/design/application-packages-47.md).

Persist the logical idempotency key before submitting. Observation timeout does not cancel or resubmit a task. Approvals remain controlled by DiOS.
