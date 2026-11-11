# Provider selection experiment #32

This suite compares the same authorization-state and intent-commit operation across PostgreSQL workspace serialization, PostgreSQL row locking, MySQL/InnoDB row locking and etcd native gRPC conditional transactions. It runs only against explicitly configured, isolated test services. It does not call a model, forward a business tool or migrate a production database.

The provider-neutral `CommitStore` contract lives in `model.py`. One successful operation checks run epoch, authorization epoch, revocation, action/resource scope and optional single-use approval; atomically records an immutable intent/evidence entry and the next state/head; and returns only after the backend commit acknowledgment. Replay returns the old identity, changed content conflicts, and unobserved outcomes remain unknown. This is a trusted-storage contract; authentication and full E2AG parameter/identity verification remain at the service boundary.

## Controlled setup

Each database has 2 CPU and 1 GiB RAM, its own Docker named volume on the same Docker storage, one member and synchronous local persistence. PostgreSQL fsync/synchronous_commit are on. MySQL innodb_flush_log_at_trx_commit=1 and sync_binlog=1. etcd uses its default fsync path. This tests local acknowledged durability and process recovery, not replicated HA or power-loss tolerance.

The same Python client is pinned to two WSL logical CPUs. SQL pools contain 32 pre-opened connections; etcd allows 32 in-flight operations over a native multiplexed gRPC channel. PostgreSQL and MySQL use READ COMMITTED plus row locks. The workspace variant adds a common lock before the run lock; all variants preserve the same state/evidence contract. Logical records and request bytes are identical; indexes, logging, protocols and drivers are native to each implementation.

Use only containers named `dios-p32-pg`, `dios-p32-mysql`, `dios-p32-etcd` carrying `dios.purpose=provider-evaluation-32`. Correctness tests deliberately SIGKILL/restart and pause/unpause these containers. Do not repoint the harness at a shared service.

## Reproduce

Install `requirements.txt` in an isolated Python environment. From repository root, set P32_PG_URL and P32_MYSQL_PASSWORD for the isolated PostgreSQL and MySQL services. MySQL listens on local port 15441 and etcd on 15442. The exact image digests, server versions and resource limits accompany the report. Docker socket read access supplies resource counters; fault tests require Docker control.

```sh
python -m experiments.providers32.run --mode check --output docs/design/evidence/provider-correctness-20261111.json
python -m experiments.providers32.run --mode benchmark --rounds 3 --seconds 5 --output docs/design/evidence/provider-benchmark-20261111.jsonl
```

`check` supports `--kinds` to rerun only a repaired adapter, replacing that adapter's evidence while preserving other completed checks. Benchmark data contains only synthetic resource names and parameters; credentials are environment configuration and excluded from output.

## Measurement

The formal screening consists of 132 cases: three randomized rounds, four implementations, closed-loop concurrency 1/8/32/128 for 64 independent runs and a single hot run; a 1,024-entry preloaded history at concurrency 8; and open-loop arrivals of 100/500 intents per second with 32 consumers. Each case has a two-second concurrency-matched warmup and a five-second measurement window. Closed-loop clients issue a new request after the previous response. Open-loop latency begins at scheduled arrival, includes queue wait, and records rejected arrivals when the bounded queue is full.

Throughput counts commits completed inside the window. Requests started in the window and completed during drain contribute to latency and are reported separately. Per-case p50/p95/p99 and repeated-round medians/ranges are separate statistics; the range is not a confidence interval. Stage measurements are implementation-specific and overlapping percentile values are not summed.

Record CPU time, end-of-case memory, block-write counter deltas, retry counts, errors and independently checked evidence heads. No monetary cost is inferred from local CPU counters. Each case verifies every stored entry's order, predecessor hash, request digest, scope, epoch, approval uniqueness and revocation ordering against the final state. The provider's transition function is not used by the verifier.

The pilot file predates fixed-pool concurrent warmup and is excluded from selection. It documents the corrected startup confound. The main suite is a bounded implementation-screening experiment; selected finalists require longer confirmation windows and production candidates require multi-node faults, actual Agent/PEP integration and capacity acceptance.

## Limits and fault meanings

Commit failure is injected before provider acknowledgment; SQL adapters fail after their writes and roll back, while etcd fails before its atomic request. Response loss is simulated by discarding an acknowledged result and replaying its identity. Database process death and pause are actual container faults. These are not disk-full, host power-loss, node-quorum or cross-region partition tests. Process recovery must establish a real data-plane connection; creating a gRPC client object alone is not a readiness signal.

Only one run's authorization root is modified in a transaction. Cross-run delegation/revocation roots, approval retention, full historical policy recomputation and external tool outcomes require additional service-level tests. The workload compares an important persistence path rather than full Agent business throughput or the paper's complete end-to-end workflow.


## Lightweight extension

SQLite WAL and LMDB are embedded transaction engines exposed through the same isolated Python TCP/JSON gateway. Each gateway runs in its own 2-CPU/1-GiB container and named volume on the same Docker storage as the other candidates; 32 client connections are pre-opened. The same state machine executes inside this gateway, while SQL/etcd adapters perform their read/transition/write sequence from the client. Thus comparison is of complete implementations, including the benefit/cost of moving an atomic operation to the state service. It is not an engine-only ranking.

SQLite uses APSW 3.51.3.0 / SQLite 3.51.3, WAL, synchronous=FULL, a 30-second busy timeout and per-connection 8-MiB cache ceiling. This version includes the upstream WAL-reset race fix. LMDB uses py-lmdb 1.7.5, sync=True, metasync=True, writemap=False and a 2-GiB virtual map ceiling; resident memory remains limited by the 1-GiB container. Both engines have one concurrent writer per database/environment. Their identical gateway binds only isolated localhost ports 15443 and 15444. It deliberately provides no production authentication endpoint.

The lightweight screening adds 36 cases: both engines, three randomized rounds, concurrency 1/32 with uniform/hotspot workloads, 1,024-entry history at concurrency 8 and open arrivals of 500/s. These are the same input/duration/acknowledgment conditions as the corresponding service-engine cases. Concurrency 8/128 uniform/hotspot and open 100/s are not additionally sampled for these engines.

Twelve additional cases probe open arrivals of 25/s and 100/s for both embedded engines, with the same five-second window and three rounds. These are recorded separately as `provider-lightweight-arrival-20261111.jsonl`.

The 18-case, 15-second confirmation compares optimized PostgreSQL, SQLite and LMDB at concurrency 32 for uniform/hotspot workloads. Logical test tables are emptied before each confirmation case, then the common warmup runs; physical engine allocation/cache remains. Screening creates independent namespaces while engine data accumulates; memory readings include accumulated data and cache, so they are not minimum RAM requirements. The finalist set was chosen after the service-engine screening and the request for lightweight alternatives; this is exploratory selection with three repetitions, not an independent preregistered benchmark.

```sh
python -m experiments.providers32.run --mode check --kinds pg-workspace pg-row mysql-row etcd-cas sqlite-wal lmdb-txn --output docs/design/evidence/provider-correctness-20261111.json
python -m experiments.providers32.lightweight --output docs/design/evidence/provider-lightweight-20261111.jsonl
python -m experiments.providers32.confirm --screen docs/design/evidence/provider-benchmark-20261111.jsonl --output docs/design/evidence/provider-confirmation-20261111.jsonl
python -m experiments.providers32.summarize docs/design/evidence/provider-benchmark-20261111.jsonl docs/design/evidence/provider-lightweight-20261111.jsonl docs/design/evidence/provider-lightweight-arrival-20261111.jsonl docs/design/evidence/provider-confirmation-20261111.jsonl --output docs/design/evidence/provider-summary-20261111.json
```

`confirm` clears only the isolated experiment tables/embedded databases after checking the purpose label. Preserve desired experimental raw records before running it. The environment record pins the local DiOS base image ID; `Dockerfile.embedded` accepts it through BASE_IMAGE. Reproducing on another host requires building the repository's backend image or supplying the recorded image. The experiment imports only its own model/server modules and starts no DiOS production service.


## Isolated service setup

`compose.yml` reproduces the five isolated service configurations. First build the repository backend image (`docker build -t dios-backend:local backend`), or set `P32_BASE_IMAGE` to the recorded local image ID. Set `P32_TEST_PASSWORD` to a synthetic test value; expose the matching client values through `P32_PG_URL` and `P32_MYSQL_PASSWORD`. `PIP_INDEX_URL` may select a package mirror; package versions remain fixed. The report records actual image IDs because upstream image tags can change.

```sh
docker compose -f experiments/providers32/compose.yml build sqlite
docker compose -f experiments/providers32/compose.yml up -d
```

Do not use the production Compose file. The experiment requires the fixed, purpose-labeled container names for fault control. A normal start command and every fault operation target these names only. Keep replica count at one for this experiment profile. Cleanup only these explicitly identified containers and experiment volumes after preserving the JSON/JSONL evidence.

The optional report generator uses `requirements-report.txt`: `python -m experiments.providers32.report` regenerates the Markdown report and standalone SVG/PNG figures from the recorded summary and validation evidence.
