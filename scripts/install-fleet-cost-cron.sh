#!/usr/bin/env bash
# Install a daily crontab entry that refreshes the DO1 fleet cost dashboard.
# Run this from a STABLE checkout on the workstation (not /tmp). Idempotent.
#
#   bash scripts/install-fleet-cost-cron.sh
#   FLEET_CRON_SCHEDULE="30 6 * * *" bash scripts/install-fleet-cost-cron.sh
#
# NOTE: this runs on the workstation, so it only fires while the machine is
# awake. You can always refresh on demand with scripts/refresh-fleet-cost-do1.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEDULE="${FLEET_CRON_SCHEDULE:-0 7 * * *}"
MARKER="# session-metrics-cron fleet-cost-refresh"

case "$REPO_ROOT" in
  /tmp/*|/private/tmp/*)
    echo "Refusing to install cron from an ephemeral path: $REPO_ROOT" >&2
    echo "Clone the repo to a stable location first (e.g. ~/Documents/GitHub/session-metrics-cron)." >&2
    exit 1 ;;
esac

LINE="$SCHEDULE cd $REPO_ROOT && /bin/bash scripts/refresh-fleet-cost-do1.sh $MARKER"
current="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$current" | grep -vF "$MARKER" || true)"
{ printf '%s\n' "$filtered" | sed '/^[[:space:]]*$/d'; printf '%s\n' "$LINE"; } | crontab -

echo "Installed daily fleet-cost refresh:"
echo "  $LINE"
echo "Logs:   ~/.session-metrics-cron/fleet-cost/refresh.log"
echo "Remove: crontab -l | grep -vF '$MARKER' | crontab -"
