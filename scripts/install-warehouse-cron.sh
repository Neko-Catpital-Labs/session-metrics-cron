#!/usr/bin/env bash
# Install a daily crontab entry that repopulates the warehouse analytics
# (cache-hit + by-intent panels). Run from a STABLE checkout (not /tmp). Idempotent.
#
#   bash scripts/install-warehouse-cron.sh
#   WAREHOUSE_CRON_SCHEDULE="30 7 * * *" bash scripts/install-warehouse-cron.sh
#
# NOTE: runs on the workstation, so it only fires while the machine is awake.
# Refresh on demand any time with scripts/refresh-warehouse-analytics.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEDULE="${WAREHOUSE_CRON_SCHEDULE:-30 7 * * *}"
MARKER="# session-metrics-cron warehouse-analytics-refresh"

case "$REPO_ROOT" in
  /tmp/*|/private/tmp/*)
    echo "Refusing to install cron from an ephemeral path: $REPO_ROOT" >&2
    echo "Clone the repo to a stable location first (e.g. ~/Documents/GitHub/session-metrics-cron)." >&2
    exit 1 ;;
esac

LINE="$SCHEDULE cd $REPO_ROOT && /bin/bash scripts/refresh-warehouse-analytics.sh $MARKER"
current="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$current" | grep -vF "$MARKER" || true)"
{ printf '%s\n' "$filtered" | sed '/^[[:space:]]*$/d'; printf '%s\n' "$LINE"; } | crontab -

echo "Installed daily warehouse-analytics refresh:"
echo "  $LINE"
echo "Logs:   ~/.session-metrics-cron/warehouse/refresh.log"
echo "Remove: crontab -l | grep -vF '$MARKER' | crontab -"
