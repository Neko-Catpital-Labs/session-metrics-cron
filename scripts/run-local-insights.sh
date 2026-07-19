#!/usr/bin/env bash
# Serve fleet cost + fixing/CI insights locally (no Metabase, no SSH tunnel).
#
#   bash scripts/run-local-insights.sh
#   -> http://127.0.0.1:8899/insights
#
# Requires config/warehouse-analytics.env with BigQuery credentials for the
# warehouse panels (/api/*). Fleet cost facts come from reports/cost-daily-fact.json.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${WAREHOUSE_ANALYTICS_ENV:-$REPO_ROOT/config/warehouse-analytics.env}"
PORT="${SPLITTER_TREE_PORT:-8899}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

if [[ ! -f "$REPO_ROOT/reports/cost-daily-fact.json" ]]; then
  echo "WARN: reports/cost-daily-fact.json missing. Run: bash scripts/refresh-fleet-cost-do1.sh" >&2
fi

export SPLITTER_TREE_HOST="${SPLITTER_TREE_HOST:-127.0.0.1}"
export SPLITTER_TREE_PORT="$PORT"
export SESSION_METRICS_REPO_DIR="$REPO_ROOT"

echo "Starting local insights server on http://${SPLITTER_TREE_HOST}:${PORT}"
echo "  Hub:          /insights"
echo "  Fleet cost:   /cost"
echo "  Fixing / CI:  /fixing-cost"
echo "  Summary:      /cost-summary"
exec bash "$REPO_ROOT/scripts/run-splitter-metric-tree-app.sh"
