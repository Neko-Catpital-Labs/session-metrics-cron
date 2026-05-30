# session-metrics-cron

Standalone cron pipeline for session analytics and Mixpanel export.

This repository collects Codex/Claude session usage across local and SSH hosts, computes cache-hit + planning-vs-execution reports, and exports replay-safe events to Mixpanel.

## What this runs

1. `scripts/cache_hit_audit.py`
2. `scripts/planning_vs_execution_report.py`
3. `scripts/mixpanel_export_usage.py`

Orchestrated by:

- `scripts/nightly_usage_pipeline.sh`

This repo also includes a separate installable Invoker benchmark harness under
`invoker-benchmarks/` for the nightly 48-session model/mode benchmark launcher.

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

## Provenance

This was extracted from Invoker work tracked in [Neko-Catpital-Labs/Invoker#965](https://github.com/Neko-Catpital-Labs/Invoker/pull/965).
