#!/usr/bin/env bash
# One-shot refresh for the WHOLE cost dashboard. Collects fleet sessions ONCE
# (the expensive rsync), then publishes both halves:
#   - top:    fleet cost fact   (refresh-fleet-cost-do1.sh)
#   - bottom: warehouse command analytics, reusing the same stage (no second rsync)
#
# This is why the two refresh scripts still exist (they publish to different
# places with different cost models) but you normally run this single entry point
# so the fleet is collected only once.
#
# Optional: set SESSION_ARCHIVE_DEST (local path or gs://bucket/prefix) to archive
# the raw collected sessions each run for durable backfill.
#
#   bash scripts/refresh-cost-dashboard.sh
#   SESSION_ARCHIVE_DEST=gs://my-bucket/sessions bash scripts/refresh-cost-dashboard.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${COST_DASHBOARD_LOG_DIR:-$HOME/.session-metrics-cron}"
mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/cost-dashboard-refresh.log" 2>&1
echo "=================================================================="
echo "[$(date '+%F %T')] cost-dashboard refresh: start"
cd "$REPO_ROOT"

# 1. Top half: collects fleet sessions into the shared stage, builds the cost
#    fact, and publishes it to the dashboard host. (Detailed log: fleet-cost/refresh.log)
bash scripts/refresh-fleet-cost-do1.sh
echo "[$(date '+%F %T')] cost-dashboard refresh: fleet cost done"

# 2. Optional durable archive of the freshly-collected raw sessions.
if [[ -n "${SESSION_ARCHIVE_DEST:-}" ]]; then
  bash scripts/archive-fleet-sessions.sh || echo "WARN: session archive failed (continuing)"
fi

# 3. Bottom half: REUSE the stage from step 1 (no second collection), build the
#    fleet attribution, and load BigQuery. (Detailed log: warehouse/refresh.log)
WAREHOUSE_NO_COLLECT=1 bash scripts/refresh-warehouse-analytics.sh
echo "[$(date '+%F %T')] cost-dashboard refresh: warehouse done"
echo "[$(date '+%F %T')] cost-dashboard refresh: complete"
