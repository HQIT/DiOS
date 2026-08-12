# E2AG reproducible experiment

This directory contains the deterministic first-stage evaluation for E2AG.
It deliberately excludes any LLM call so that contract and policy effects can
be measured independently from model stochasticity and network latency.

## Run

For dependency installation in mainland China, use the explicit mirrors in
`docs/paper/E2AG-reproducibility.md`.

From the repository root:

```powershell
python experiments/e2ag/run_experiment.py --repeats 5000
python experiments/e2ag/run_audit_experiment.py
backend\.venv\Scripts\python.exe experiments/e2ag/run_dispatch_benchmark.py --repeats 500
backend\.venv\Scripts\python.exe experiments/e2ag/run_tool_gateway_experiment.py --repeats 10000
backend\.venv\Scripts\python.exe experiments/e2ag/run_mutation_experiment.py --per-operator 100
backend\.venv\Scripts\python.exe experiments/e2ag/run_http_benchmark.py --repeats 300
```

The runner evaluates three modes:

- `B0_no_governance`: all events are admitted;
- `B1_contract_only`: structural and source/type contract admission only;
- `B2_full_e2ag`: contract admission plus target capability policy.

Inputs are in `attack_cases.jsonl`. Outputs are overwritten deterministically in
`results/summary.json` and `results/case_results.csv`.

## Scope and interpretation

The current corpus contains 22 hand-authored cases: 14 attacks and 8 benign
events across Git, IMAP, generic webhook, manual and cron sources. Results are a
prototype sanity check, not evidence of population-level detection accuracy.
Latency is the in-process decision-core cost on the machine running the script;
it is not end-to-end Event Gateway latency.

`run_audit_experiment.py` reports both detected mutations and the expected blind
spot of an unanchored hash chain. `run_dispatch_benchmark.py` includes E2AG
evaluation plus EventLog/audit persistence in an in-memory SQLite database, but
still excludes HTTP and Agent execution.

The test does not claim to solve arbitrary prompt injection. A strict Agent
policy can require a declared action and constrain sources, event types, actions
and tools before an LLM runs. Semantic payload attacks that remain within those
declared bounds are not classified. The task-scoped remote MCP gateway instead
constrains their effects: `run_tool_gateway_experiment.py` compares dispatch-only
authorization with the runtime allow-list over ten deterministic tool/method
cases. The 26 backend tests additionally verify that denied calls never reach
the mocked upstream, authorized calls do, `tools/list` is filtered, grants
expire/revoke, and unmediated stdio transport is withheld in enforce mode.

`run_mutation_experiment.py` deterministically derives 700 single-factor cases
(seven operators x 100) from the eight benign fixtures with seed `20260812`.
It is a mechanism-coverage stress test, not a real-world attack distribution.

`run_http_benchmark.py` includes FastAPI routing, middleware, response
serialization and file-backed SQLite. It deliberately excludes a TCP socket,
subscription targets, Agent/model execution, and remote tools. Its balanced
mode order reduces but does not eliminate host noise.
