#!/usr/bin/env bash
# Refresh the fleet cost dashboard data and publish it to the DO1 splitter app.
#
# Runs on the WORKSTATION: it is the only host with local omp sessions AND SSH
# reach to every fleet machine. It (1) collects codex/claude/omp sessions across
# local + all SSH hosts, (2) builds the daily cost fact + summary, (3) copies
# them to DO1 where splitter_metric_tree_app.py serves them at /cost.
#
# Manual:  bash scripts/refresh-fleet-cost-do1.sh
# Cron:    see the crontab line printed by scripts/install-fleet-cost-cron.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE_DIR="${FLEET_STAGE_DIR:-/tmp/fleet-sessions}"
OUT_DIR="${FLEET_OUT_DIR:-/tmp/fleet-reports}"
DO1_HOST="${DO1_HOST:-invoker@157.230.133.215}"
DO1_REPO="${DO1_REPO:-/home/invoker/session-metrics-cron}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
LOG_DIR="${FLEET_LOG_DIR:-$HOME/.session-metrics-cron/fleet-cost}"

mkdir -p "$LOG_DIR" "$OUT_DIR"
exec >>"$LOG_DIR/refresh.log" 2>&1
echo "=================================================================="
echo "[$(date '+%F %T')] refresh start (repo=$REPO_ROOT)"

cd "$REPO_ROOT"

# 1. Collect across local + all SSH hosts -> daily fact + breakdown CSVs.
python3 scripts/fleet_cost_report.py --stage-dir "$STAGE_DIR" --out-dir "$OUT_DIR"

# 2. Render the static summary (served at /cost-summary).
python3 scripts/invoker_cost_breakdown_report.py \
  --reports-dir "$OUT_DIR" \
  --html-out "$OUT_DIR/invoker-cost-breakdown.html" \
  --markdown-out "$OUT_DIR/invoker-cost-breakdown.md" \
  --json-out "$OUT_DIR/invoker-cost-breakdown.json"

# 3. Publish to DO1 (served live; no restart needed - routes read files per request).
SSHO=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o BatchMode=yes)
ssh "${SSHO[@]}" "$DO1_HOST" "mkdir -p $DO1_REPO/reports"
scp "${SSHO[@]}" "$OUT_DIR/cost-daily-fact.json" "$DO1_HOST:$DO1_REPO/reports/cost-daily-fact.json"
scp "${SSHO[@]}" "$OUT_DIR/invoker-cost-breakdown.html" "$DO1_HOST:$DO1_REPO/reports/invoker-cost-breakdown.html"

echo "[$(date '+%F %T')] refresh done -> published cost-daily-fact.json + summary to $DO1_HOST"
