# Invoker Benchmark Nightly Launcher

The benchmark harness lives in `invoker-benchmarks/` and is intended to be
installed on the coordinator at:

```text
/home/invoker/invoker-benchmarks/
```

Cron should invoke only:

```bash
/home/invoker/invoker-benchmarks/bin/run-nightly-benchmark.sh
```

The coordinator resolves `INVOKER_SHA` from `INVOKER_REPO` and
`INVOKER_BRANCH`. Worker jobs clone that exact SHA, build `@invoker/cli`, and
run generated plans through the standalone CLI with a per-job config and DB
directory. Update `INVOKER_BRANCH` to the Invoker branch under test, including
stack branches when benchmarking a stack.

Default schedule:

```cron
0 0 * * * TZ=Asia/Hong_Kong /home/invoker/invoker-benchmarks/bin/run-nightly-benchmark.sh
```

## Install

1. Copy `invoker-benchmarks/` to `/home/invoker/invoker-benchmarks/` on
   `remote_digital_ocean_1`.
2. Copy `config/benchmark.env.example` to `config/benchmark.env` and fill in
   Mixpanel auth if live emission is desired.
3. Generate worker inventory from the coordinator Invoker config:

```bash
/home/invoker/invoker-benchmarks/bin/sync-worker-credentials.sh \
  --write-workers /home/invoker/invoker-benchmarks/config/workers.json
```

The generated worker list includes `remote_digital_ocean_2`,
`remote_digital_ocean_3`, `remote_digital_ocean_4`, and `remote_linode_1`.
The coordinator only orchestrates and does not run benchmark jobs.

## Validation

```bash
/home/invoker/invoker-benchmarks/bin/run-nightly-benchmark.sh --dry-run
/home/invoker/invoker-benchmarks/bin/run-nightly-benchmark.sh --smoke
/home/invoker/invoker-benchmarks/bin/run-nightly-benchmark.sh --limit 6
/home/invoker/invoker-benchmarks/bin/run-nightly-benchmark.sh --job-set /home/invoker/invoker-benchmarks/config/job-set.json
```

Use `--job-set` or `BENCHMARK_JOB_SET_FILE` for an explicit ordered run, for
example three workflows back to back. JSON job sets use:

```json
{
  "jobs": [
    {"file": "session-01.jsonl", "model": "codex", "mode": "invoker_workflow"},
    {"file": "session-02.jsonl", "model": "codex", "mode": "invoker_workflow"},
    {"file": "session-03.jsonl", "model": "codex", "mode": "invoker_workflow"}
  ]
}
```

No-argument nightly runs publish metrics by default. Add `--no-emit-mixpanel`
to write `mixpanel-export.jsonl` locally without publishing. Smoke runs publish
only when `--emit-mixpanel` is passed.

## Runtime Output

Each batch writes to:

```text
/home/invoker/invoker-benchmarks/runs/YYYY-MM-DD_HH-mm-ss_HKT_<batch_id>/
```

The coordinator writes `summary.md`, `summary.json`, `job-matrix.tsv`,
`worker-assignments.tsv`, and `mixpanel-export.jsonl`. Each job writes
`job.json`, logs, generated plan, token usage, raw sessions, and the disposable
checkout under `jobs/<run_id>/`.

## Mixpanel Dashboard

Use a benchmark-only board named `Invoker Benchmark Runs`. The primary event is
`benchmark_run`; reserve `benchmark_batch`, `benchmark_task`, and
`benchmark_token_usage` for drilldowns. Every benchmark event must include the
canonical `invoker_sha` property.

Recommended panels:

- Run count: event `benchmark_run`, count events, breakdown `invoker_sha`.
- Cost by SHA: event `benchmark_run`, sum `derived_total_cost_usd`, breakdown
  `invoker_sha`.
- Normalized tokens by SHA: event `benchmark_run`, sum
  `normalized_total_tokens`, breakdown `invoker_sha`.
- Result/status: event `benchmark_run`, count events, breakdowns
  `invoker_sha`, `result`, and `status`.
- Error reasons: event `benchmark_run`, count events, filter `result = fail`,
  breakdowns `invoker_sha`, `failure_reason`, `failure_stage`, `mode`, and
  `model`.
- Model comparison: event `benchmark_run`, count events, breakdowns
  `invoker_sha` and `model`.
- Scenario comparison: event `benchmark_run`, count events, breakdowns
  `invoker_sha` and `scenario`.
- Execution surface: event `benchmark_run`, count events, breakdowns
  `invoker_sha` and `execution_surface`.
