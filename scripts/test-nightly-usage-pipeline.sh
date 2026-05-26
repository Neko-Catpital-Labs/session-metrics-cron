#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUMMARY_PATH="/tmp/nightly-usage-summary.json"
STATE_PATH="/tmp/nightly-usage-state.json"

cd "$REPO_ROOT"

for required in \
  cache-hit-audit-report.json \
  reports/planning-vs-execution-report.json \
  reports/planning-vs-execution-sessions.csv \
  reports/planning-vs-execution-prompts.csv \
  reports/planning-vs-execution-tool-breakdown.csv \
  reports/planning-vs-execution-tool-attribution.csv; do
  [[ -f "$required" ]] || {
    echo "Missing required artifact: $required" >&2
    echo "Run: bash scripts/nightly_usage_pipeline.sh --dry-run --env-file config/nightly-usage.env" >&2
    exit 1
  }
done

rm -f "$SUMMARY_PATH" "$STATE_PATH"
MIXPANEL_TOKEN="test-token" python3 scripts/mixpanel_export_usage.py \
  --dry-run \
  --input-root "$REPO_ROOT" \
  --summary-path "$SUMMARY_PATH" \
  --state-file "$STATE_PATH"

python3 - <<'PY'
import json
from pathlib import Path

summary = json.loads(Path("/tmp/nightly-usage-summary.json").read_text())
required_families = {
    "usage_daily_rollup",
    "usage_session",
    "usage_prompt",
    "usage_request_cache_diagnosis",
    "usage_request_cache_source",
    "usage_tool_breakdown",
    "usage_tool_attribution",
    "usage_request_tool_attribution",
    "usage_cache_driver",
}
families = summary.get("families", {})
missing = sorted(required_families - set(families))
if missing:
    raise SystemExit(f"Missing families in summary: {missing}")
if summary.get("total_events_after_dedupe", 0) <= 0:
    raise SystemExit("Expected > 0 events in dry-run summary")
if families.get("usage_request_cache_diagnosis") != families.get("usage_prompt"):
    raise SystemExit("Expected request diagnosis count to match prompt count")
if families.get("usage_request_tool_attribution") != families.get("usage_tool_attribution"):
    raise SystemExit("Expected request tool attribution count to match tool attribution count")
required_task_fields = {
    "task_type",
    "task_type_label",
    "task_type_confidence",
    "task_type_classifier",
    "task_type_reason",
    "task_type_source",
    "task_type_config_version",
}
sample_props = summary.get("sample_event_properties", {})
for family in ("usage_request_cache_diagnosis", "usage_request_tool_attribution"):
    missing_fields = sorted(required_task_fields - set(sample_props.get(family, [])))
    if missing_fields:
        raise SystemExit(f"Expected {family} to include task_type fields, missing: {missing_fields}")
print("OK: nightly usage dry-run summary includes all event families")
print("event_counts", json.dumps(families, sort_keys=True))
PY
