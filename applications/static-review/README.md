# static-review

Independent reference application. Owns its application semantics; imports only the public dios-sdk.

Build, deployment, versioning and rollback: [application contracts](../../docs/design/application-packages-47.md).

See application.json for protocol requirements and pyproject.toml for console entry points. Credentials and customer data are external configuration.

Harness entry points are `dios-cursor-review` and `dios-dsh-review`. Both use the same input validation, evidence rules and report format. See the [DSH profile](examples/dsh/README.md) for the second-source configuration.
