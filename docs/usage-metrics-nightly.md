# Nightly Usage Metrics (Mixpanel)

This repository runs a nightly pipeline that:

1. audits cache-hit usage across local + SSH session sources,
2. builds planning-vs-execution reports,
3. exports high-detail events into Mixpanel.

## Required files

- `config/nightly-usage.env`
- `config/sources.json` (or override via env/CLI)

## Run commands

Dry run:

```bash
bash scripts/nightly_usage_pipeline.sh --dry-run --env-file config/nightly-usage.env
```

Fast dry run (reuse existing report artifacts):

```bash
bash scripts/nightly_usage_pipeline.sh --dry-run --env-file config/nightly-usage.env --skip-cache-audit --skip-report
```

Backfill/replay (Mixpanel-safe):

```bash
bash scripts/nightly_usage_pipeline.sh --date 2026-05-25 --env-file config/nightly-usage.env --ignore-local-state
```

## Dedupe model

- Every event includes deterministic `$insert_id`.
- Keys are canonicalized (session file stem, prompt index, source/value hashes).
- Dry runs do not mutate local state.
- Optional local suppression can be bypassed with `--ignore-local-state`.

## launchd

Install:

```bash
bash scripts/install-nightly-usage-launchd.sh --env-file config/nightly-usage.env --time 02:10
```

Uninstall:

```bash
bash scripts/uninstall-nightly-usage-launchd.sh
```

## Logs/state

- `~/.session-metrics-cron/usage-metrics/nightly-run.log`
- `~/.session-metrics-cron/usage-metrics/send_state.json`
