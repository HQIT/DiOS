# E2AG reproducible experiment

This directory contains the deterministic evaluation for E2AG and one
supplementary live-model experiment. The primary ablation, end-to-end,
fault-injection, and concurrency protocols exclude model calls so that
governance effects can be measured independently from model stochasticity and
network latency. The live-model protocol replays each immutable model decision
across three governance configurations for a paired comparison.

## Run

For dependency installation in mainland China, use the explicit mirrors in
`docs/paper/E2AG-reproducibility.md`.

From the repository root:

```powershell
backend\.venv\Scripts\python.exe experiments/e2ag/run_frozen_ablation.py
backend\.venv\Scripts\python.exe experiments/e2ag/run_e2e_chain_experiment.py --repeats 30
backend\.venv\Scripts\python.exe experiments/e2ag/run_causal_audit_experiment.py --repeats 20
backend\.venv\Scripts\python.exe experiments/e2ag/run_concurrency_experiment.py --levels 8,32 --rounds 100
backend\.venv\Scripts\python.exe experiments/e2ag/prepare_independent_review.py
backend\.venv\Scripts\python.exe experiments/e2ag/run_live_llm_chain_experiment.py --repeats 10
python experiments/e2ag/run_experiment.py --repeats 5000
python experiments/e2ag/run_audit_experiment.py
backend\.venv\Scripts\python.exe experiments/e2ag/run_dispatch_benchmark.py --repeats 500
backend\.venv\Scripts\python.exe experiments/e2ag/run_tool_gateway_experiment.py --repeats 10000
backend\.venv\Scripts\python.exe experiments/e2ag/run_mutation_experiment.py --per-operator 100
backend\.venv\Scripts\python.exe experiments/e2ag/run_http_benchmark.py --repeats 300
```

The independent policy-engine baseline uses the official OPA v1.17.0 binary.
Its primary comparison applies a structural call-ready rule to the frozen
60-case matrix (non-empty target, explicit requested tool, and target tool
allowlist), producing 49 cases. The existing eight `tools/call` cases remain a
supplementary interface check:

```bash
python3 experiments/e2ag/run_opa_tool_baseline.py \
  --opa-bin /path/to/opa_linux_amd64_static \
  --expected-opa-sha256 e83da46804832578e9d9e1733dffbe4d3b5f8cc9c26eb124da9ceea4abfe189f
```

The output is `results/opa_tool_baseline_summary.json`. OPA-Tool receives each
case's task-level tool patterns through a generic default-deny Rego policy.
The primary slice records all 11 structurally excluded case IDs; the
supplementary interface check excludes two non-`tools/call` methods. NoGuard
and E2AG comparator rows are projected from the existing frozen C0P0/C1P1
results for exactly the same 49 IDs. This comparison isolates visible policy
context; it does not compare local process latency or rank OPA against the E2AG
architecture.

The first four commands are the primary deterministic paper evaluation as of
2026-08-15. They produce the frozen Contract x Policy 2x2 result, 480 real
dispatcher-to-MCP executions, 100 persisted causal-localization traces, and the
SQLite concurrency robustness result. The live-model command adds 30 model
decisions and 90 paired execution paths. The older 22-case, mutation, and pure
authorization scripts remain development and supplementary checks.

The self-operated qame--demogo governance regression is recorded in
`results/external_qame_demogo_summary.json` and
`results/external_governance_regression_summary.json`. The positive control
connects a real `HQIT/qame` GitHub push to an isolated DiOS backend, a real
task-mode DiAgent container, the task-scoped MCP proxy, and the external
`git-perf` service. The fixed negative control checks that a tool outside the
issued grant is denied before upstream and leaves the canary unchanged. The
negative vector is deterministic: do not ask a model to generate, select, or
advance it. This pair is a governance-consistency check, not a statistical
workload or a model-behavior estimate.

The live-model command is an external-validity example. It requires
`OPENAI_BASE_URL`, `OPENAI_API_KEY`, and optionally `OPENAI_MODEL` and
`OFOX_HTTP_PROXY`. Credentials are read only from the process environment and
are not written to results. The default protocol makes 30 model requests and
replays those immutable decisions across three governance configurations.

`frozen_cases.jsonl` is immutable by protocol once reviewed. Its current
SHA-256 is stored in `results/frozen_ablation_summary.json`. Four reviewers
who did not construct the matrix completed the blinded protocol; anonymized
returns and the panel statistics are stored under `review/`.

`prepare_independent_review.py` creates a deterministically shuffled blind
sheet and a separate author-label mapping under `review/`. Give only the blind
sheet to reviewers who did not construct the corpus. After returns are
collected, `summarize_independent_review.py` verifies frozen input integrity,
normalizes UTF-8/GB18030 inputs, reports author, pairwise and Fleiss agreement,
and creates anonymized returns plus an adjudication table for disagreements.

The runner evaluates three modes:

- `B0_no_governance`: all events are admitted;
- `B1_contract_only`: structural and source/type contract admission only;
- `B2_full_e2ag`: contract admission plus target capability policy.

Inputs are in `attack_cases.jsonl`. Outputs are overwritten deterministically in
`results/summary.json` and `results/case_results.csv`.

## Scope and interpretation

The frozen corpus contains 60 threat-driven cases: 30 directly admissible
events, 26 cases expected to be denied, and 4 production-sensitive cases
expected to enter approval. The frozen author map encodes the latter 30 as
`attack` for backward-compatible ablation, but the paper reports governance
outcomes separately because approval-required does not imply malicious intent.
Results establish mechanism behavior on this matrix, not
population-level detection accuracy. The original corpus contains 22
development cases and remains a prototype sanity check.
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
cases. The 34 backend tests additionally verify that denied calls never reach
the mocked upstream, authorized calls do, `tools/list` is filtered, grants
expire/revoke, and unmediated stdio transport is withheld in enforce mode.

`run_mutation_experiment.py` deterministically derives 700 single-factor cases
(seven operators x 100) from the eight benign fixtures with seed `20260812`.
It is a mechanism-coverage stress test, not a real-world attack distribution.

`run_http_benchmark.py` includes FastAPI routing, middleware, response
serialization and file-backed SQLite. It deliberately excludes a TCP socket,
subscription targets, Agent/model execution, and remote tools. Its balanced
mode order reduces but does not eliminate host noise.
