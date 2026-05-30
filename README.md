# session-metrics

Session analytics for nightly usage metrics, warehouse publishing, and Invoker benchmark runs.

This repository collects Codex/Claude session usage across local and SSH hosts, computes cache-hit and planning-vs-execution reports, exports replay-safe events to Mixpanel, and publishes normalized command-cost analytics into warehouse-backed Metabase dashboards.

## Repo Map

- Nightly pipeline: collects session usage, computes cache and attribution reports, and exports Mixpanel events through `scripts/nightly_usage_pipeline.sh`.
- Publishing analytics: exports the normalized warehouse table, loads BigQuery and ClickHouse, and creates Metabase dashboards through `scripts/run-warehouse-analytics.sh`.
- Benchmark harness: runs the Invoker nightly model/mode benchmark tooling from `invoker-benchmarks/`.

## Nightly Pipeline

1. `scripts/cache_hit_audit.py`
2. `scripts/planning_vs_execution_report.py`
3. `scripts/mixpanel_export_usage.py`

Orchestrated by:

- `scripts/nightly_usage_pipeline.sh`

## Publishing Analytics

The warehouse publishing surface is orchestrated by:

- `scripts/run-warehouse-analytics.sh`

It validates the local command-cost export, loads BigQuery, loads ClickHouse, and creates matching Metabase dashboards.

## Quickstart

1. Copy env file and edit credentials:

```bash
cp config/nightly-usage.env.example config/nightly-usage.env
```

2. Configure source hosts:

```bash
cp config/sources.json config/sources.local.json
```

Then set `USAGE_PIPELINE_SOURCES_CONFIG` in `config/nightly-usage.env` to your local file.

3. Run a dry-run pipeline:

```bash
bash scripts/nightly_usage_pipeline.sh --dry-run --env-file config/nightly-usage.env
```

## Make targets

- `make audit`
- `make report`
- `make export-dry-run`
- `make nightly-dry-run`
- `make warehouse-demo-validate`
- `make test`
- `make benchmark-dry-run`

## Scheduling

- Local launchd scripts:
  - `scripts/install-nightly-usage-launchd.sh`
  - `scripts/uninstall-nightly-usage-launchd.sh`
- Optional GitHub Actions cron:
  - `.github/workflows/nightly-session-metrics.yml`

## Documentation

- `docs/setup.md`
- `docs/architecture.md`
- `docs/source-onboarding.md`
- `docs/usage-metrics-nightly.md`
- `docs/operations-backfill.md`
- `docs/run-your-own-analytics.md`
- `docs/warehouse-cost-demo.md`
- `docs/reports/planning-vs-execution-tooling.md`
- `docs/migration-from-invoker.md`
- `docs/invoker-benchmark-nightly.md`

## Backfills and replay-safe dedupe

- Re-submit with deterministic dedupe keys:

```bash
bash scripts/nightly_usage_pipeline.sh --date 2026-05-25 --ignore-local-state --env-file config/nightly-usage.env
```

Mixpanel dedupe is driven by stable `$insert_id` values per logical row.

## Repository Rename Note

If the GitHub repository is renamed to `session-metrics`, GitHub should preserve redirects. After that rename, update local remotes with the new repository URL; script paths, runtime state paths, launchd identifiers, and env var names intentionally remain stable in this pass.

## Provenance

This was extracted from Invoker work tracked in [Neko-Catpital-Labs/Invoker#965](https://github.com/Neko-Catpital-Labs/Invoker/pull/965).
