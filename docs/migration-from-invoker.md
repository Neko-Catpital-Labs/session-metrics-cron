# Migration from Invoker

This repository was extracted from Invoker work originally proposed in:

- https://github.com/Neko-Catpital-Labs/Invoker/pull/965

## Scope moved here

- Nightly pipeline orchestration (`scripts/nightly_usage_pipeline.sh`)
- Cache-hit and session analysis scripts
- Mixpanel export with deterministic replay-safe dedupe keys
- launchd scheduling helpers
- Dry-run regression checks and operator docs

## Cutover checklist

1. Verify dry-run behavior in this repo.
2. Verify replay/backfill mode (`--ignore-local-state`) against your Mixpanel project.
3. Switch production scheduler to this repo.
4. Close or narrow Invoker PR #965 to avoid ownership overlap.
