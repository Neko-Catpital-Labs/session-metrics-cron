# Operations and Backfill

## Standard nightly run

```bash
bash scripts/nightly_usage_pipeline.sh --env-file config/nightly-usage.env
```

## Backfill a specific date

```bash
bash scripts/nightly_usage_pipeline.sh --date 2026-05-25 --env-file config/nightly-usage.env
```

## Replay/resubmit without local suppression

Use this when local state may be stale or partial:

```bash
bash scripts/nightly_usage_pipeline.sh --date 2026-05-25 --env-file config/nightly-usage.env --ignore-local-state
```

This relies on deterministic Mixpanel `$insert_id` values to deduplicate repeated sends.

## Troubleshooting

- Missing input artifacts:
  - Run full pipeline without skip flags.
- SSH pull failures:
  - Verify host/user/key/port in source config.
  - Verify `ssh` and `rsync` can connect manually.
- Unexpected empty exports:
  - Check `cache-hit-audit-report.json` and `reports/` contents.
  - Run `python3 scripts/mixpanel_export_usage.py --dry-run --input-root .`.
