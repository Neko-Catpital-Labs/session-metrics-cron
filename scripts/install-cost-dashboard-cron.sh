#!/usr/bin/env bash
# Install ONE daily crontab entry that refreshes the whole cost dashboard
# (top + bottom) via scripts/refresh-cost-dashboard.sh, collecting the fleet once.
# Removes the older split fleet-cost / warehouse cron entries so the fleet is not
# collected multiple times. Run from a STABLE checkout (not /tmp). Idempotent.
#
#   bash scripts/install-cost-dashboard-cron.sh
#   COST_DASHBOARD_CRON_SCHEDULE="0 7 * * *" bash scripts/install-cost-dashboard-cron.sh
#
# To keep the split schedules instead, use install-fleet-cost-cron.sh +
# install-warehouse-cron.sh and do NOT run this.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEDULE="${COST_DASHBOARD_CRON_SCHEDULE:-0 7 * * *}"
MARKER="# session-metrics-cron cost-dashboard-refresh"
OLD_MARKERS=(
  "# session-metrics-cron fleet-cost-refresh"
  "# session-metrics-cron warehouse-analytics-refresh"
)

case "$REPO_ROOT" in
  /tmp/*|/private/tmp/*)
    echo "Refusing to install cron from an ephemeral path: $REPO_ROOT" >&2
    echo "Clone the repo to a stable location first (e.g. ~/Documents/GitHub/session-metrics-cron)." >&2
    exit 1 ;;
esac

LINE="$SCHEDULE cd $REPO_ROOT && /bin/bash scripts/refresh-cost-dashboard.sh $MARKER"
current="$(crontab -l 2>/dev/null || true)"
filtered="$current"
for m in "$MARKER" "${OLD_MARKERS[@]}"; do
  filtered="$(printf '%s\n' "$filtered" | grep -vF "$m" || true)"
done
{ printf '%s\n' "$filtered" | sed '/^[[:space:]]*$/d'; printf '%s\n' "$LINE"; } | crontab -

echo "Installed unified daily cost-dashboard refresh (removed any split fleet-cost/warehouse crons):"
echo "  $LINE"
echo "Logs:   ~/.session-metrics-cron/cost-dashboard-refresh.log (+ fleet-cost/ and warehouse/ sub-logs)"
echo "Remove: crontab -l | grep -vF '$MARKER' | crontab -"
