# Architecture

`session-metrics` has three stable surfaces. This pass keeps files where they are and documents the ownership boundary between them.

## Nightly Pipeline

`scripts/nightly_usage_pipeline.sh` is the operational entrypoint for nightly usage metrics. It orchestrates local and SSH source collection, cache-hit auditing, planning-vs-execution reporting, request and command attribution, and replay-safe Mixpanel export.

Use this surface when the goal is to refresh session usage metrics or backfill Mixpanel events:

```bash
bash scripts/nightly_usage_pipeline.sh --dry-run --env-file config/nightly-usage.env
```

## Publishing Analytics

`scripts/run-warehouse-analytics.sh` is the publishing entrypoint for warehouse-backed analytics. It wraps the warehouse demo script so a normalized command-cost CSV can be validated, loaded into BigQuery and ClickHouse, and published as Metabase dashboards.

Use this surface when the goal is to update warehouse tables or recreate dashboard assets:

```bash
bash scripts/run-warehouse-analytics.sh --dry-run
```

The dry run should include a `create-metabase` command unless `--skip-metabase` is set.

## Benchmark Harness

`invoker-benchmarks/` contains the standalone Invoker benchmark coordinator and worker scripts. It is installed separately on benchmark hosts and emits benchmark-focused metrics; it is not part of the nightly usage pipeline or warehouse publishing wrapper.

Use this surface when the goal is to run model/mode benchmark batches:

```bash
invoker-benchmarks/bin/run-nightly-benchmark.sh --dry-run
```

## Compatibility Boundary

These paths are public operational interfaces and should remain stable unless a migration is planned:

- `bash scripts/nightly_usage_pipeline.sh ...`
- `bash scripts/run-warehouse-analytics.sh ...`
- `make nightly-dry-run`
- `make warehouse-demo-validate`

Runtime state paths, launchd identifiers, and existing env var names are also compatibility surfaces. They may still contain legacy naming even though the repo and product documentation now use `session-metrics`.
