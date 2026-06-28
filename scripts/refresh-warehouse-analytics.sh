#!/usr/bin/env bash
# Repopulate the FLEET-WIDE warehouse command-cost analytics (the cost dashboard's
# "Cache hit rate" and "Cost by intent" panels), then reload BigQuery.
#
# Scope: codex/claude/omp sessions across local + all SSH fleet hosts (from
# ~/.invoker/config.json). Per-command cost is anchored to the pricing table
# (ccusage-free) so the whole fleet is costed consistently; omp commands keep
# their exact per-turn cost.
#
# Dedup-safe by construction:
#   - fleet sessions are de-duplicated by file content hash across hosts, and
#   - warehouse_cost_demo loads with `bq load --replace` (full table overwrite).
# Re-running never appends or double-counts.
#
# Manual:  bash scripts/refresh-warehouse-analytics.sh
# Cron:    bash scripts/install-warehouse-cron.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${WAREHOUSE_ANALYTICS_ENV:-$REPO_ROOT/config/warehouse-analytics.env}"
STAGE_DIR="${FLEET_STAGE_DIR:-/tmp/fleet-sessions}"
LOG_DIR="${WAREHOUSE_LOG_DIR:-$HOME/.session-metrics-cron/warehouse}"

mkdir -p "$LOG_DIR"
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

# 1. Collect fleet sessions (local + SSH hosts), dedup by content hash, classify
#    intent, and emit the fleet-wide v4.5 command-attribution CSV (pricing-table costs).
python3 scripts/fleet_warehouse_attribution.py --stage-dir "$STAGE_DIR" --out-dir reports

# 2. Load BigQuery: bq load --replace + refresh views + parity check (row/cost == CSV).
python3 scripts/warehouse_cost_demo.py load-bigquery

echo "[$(date '+%F %T')] warehouse refresh done -> $BIGQUERY_PROJECT_ID command_costs reloaded (fleet)"
