#!/usr/bin/env bash
# Repopulate the warehouse command-cost analytics that back the cost dashboard's
# "Cache hit rate" and "Cost by intent" panels, then reload BigQuery.
#
# Scope: THIS workstation's codex/claude/omp sessions only. The attribution cost
# model anchors per-command cost to this machine's ccusage bill, so it is
# local-scoped by design. Fleet-wide attribution is a separate pipeline.
#
# Dedup-safe by construction:
#   - cache_hit_audit dedups source logs by content hash, and
#   - warehouse_cost_demo loads with `bq load --replace` (full table overwrite).
# Re-running never appends or double-counts.
#
# Manual:  bash scripts/refresh-warehouse-analytics.sh
# Cron:    bash scripts/install-warehouse-cron.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${WAREHOUSE_ANALYTICS_ENV:-$REPO_ROOT/config/warehouse-analytics.env}"
SOURCES_CONFIG="${WAREHOUSE_SOURCES_CONFIG:-$REPO_ROOT/config/sources-local.json}"
WORKSPACE="${WAREHOUSE_BACKFILL_WORKSPACE:-$HOME/.session-metrics-cron/warehouse/backfill-workspace}"
LOG_DIR="${WAREHOUSE_LOG_DIR:-$HOME/.session-metrics-cron/warehouse}"

mkdir -p "$LOG_DIR" "$(dirname "$WORKSPACE")"
exec >>"$LOG_DIR/refresh.log" 2>&1
echo "=================================================================="
echo "[$(date '+%F %T')] warehouse refresh start (repo=$REPO_ROOT)"

cd "$REPO_ROOT"

# Load BigQuery project + service-account creds (BIGQUERY_PROJECT_ID,
# BIGQUERY_DATASET, GOOGLE_APPLICATION_CREDENTIALS).
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
: "${BIGQUERY_PROJECT_ID:?Set BIGQUERY_PROJECT_ID in $ENV_FILE}"
if [[ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" || ! -f "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
  echo "Missing GOOGLE_APPLICATION_CREDENTIALS (service-account json). Set it in $ENV_FILE." >&2
  exit 1
fi

# 1. Audit local sessions: fresh ccusage baseline + content-hash-deduped merged dirs.
python3 scripts/cache_hit_audit.py \
  --output cache-hit-audit-report.json --top 50 \
  --sources-config "$SOURCES_CONFIG" --workspace "$WORKSPACE"

# 2. Build the v4.5 command-attribution CSV (deduped dirs + intent classifier).
python3 scripts/planning_vs_execution_report.py --out-dir reports

# 3. Load BigQuery: bq load --replace + refresh views + parity check (row/cost == CSV).
python3 scripts/warehouse_cost_demo.py load-bigquery

echo "[$(date '+%F %T')] warehouse refresh done -> $BIGQUERY_PROJECT_ID command_costs reloaded"
