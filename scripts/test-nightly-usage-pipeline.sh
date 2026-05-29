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
  reports/planning-vs-execution-tool-attribution.csv \
  reports/usage-command-attribution-v4.csv \
  reports/usage-command-attribution-v4-summary.json \
  reports/usage-command-attribution-v4-report.md \
  reports/usage-command-attribution-v4_1.csv \
  reports/usage-command-attribution-v4_1-summary.json \
  reports/usage-command-attribution-v4_1-report.md \
  reports/usage-command-attribution-v4_2.csv \
  reports/usage-command-attribution-v4_2-summary.json \
  reports/usage-command-attribution-v4_2-report.md \
  reports/usage-command-attribution-v4_2-review.csv \
  reports/usage-command-attribution-v4_3.csv \
  reports/usage-command-attribution-v4_3-summary.json \
  reports/usage-command-attribution-v4_3-report.md \
  reports/usage-command-attribution-v4_3-review.csv; do
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
    "usage_command_attribution",
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
required_request_pattern_fields = {
    "request_pattern",
    "request_pattern_path",
    "request_pattern_depth",
    "request_pattern_rule_id",
    "request_pattern_confidence",
    "request_pattern_config_version",
    "diagnosis_version",
}
sample_props = summary.get("sample_event_properties", {})
for family in ("usage_request_cache_diagnosis", "usage_request_tool_attribution"):
    missing_fields = sorted(required_task_fields - set(sample_props.get(family, [])))
    if missing_fields:
        raise SystemExit(f"Expected {family} to include task_type fields, missing: {missing_fields}")
    missing_fields = sorted(required_request_pattern_fields - set(sample_props.get(family, [])))
    if missing_fields:
        raise SystemExit(f"Expected {family} to include request pattern fields, missing: {missing_fields}")
    if "request_subpattern" in set(sample_props.get(family, [])):
        raise SystemExit(f"Expected {family} to omit legacy request_subpattern")
required_command_fields = {
    "schema_version",
    "service_classifier_revision",
    "command_preview",
    "command_hash",
    "primary_why",
    "why_tags",
    "tool_action",
    "service_of_why",
    "service_of_confidence",
    "service_of_source",
    "session_root_cause_summary",
    "uncategorized_reason",
    "target_type",
    "target",
    "cost_is_estimated",
    "cost_allocation_method",
    "allocated_total_cost_usd",
}
missing_fields = sorted(required_command_fields - set(sample_props.get("usage_command_attribution", [])))
if missing_fields:
    raise SystemExit(f"Expected usage_command_attribution to include v4 fields, missing: {missing_fields}")
print("OK: nightly usage dry-run summary includes all event families")
print("event_counts", json.dumps(families, sort_keys=True))
PY
