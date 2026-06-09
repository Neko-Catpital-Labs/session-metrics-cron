#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${SESSION_METRICS_REPO_DIR:-$HOME/session-metrics-cron}"
STATE_DIR="${SESSION_METRICS_STATE_DIR:-$HOME/.local/state/workflow-analysis-service}"
PID_FILE="$STATE_DIR/splitter-metric-tree.pid"
LOG_FILE="$STATE_DIR/logs/splitter-metric-tree.log"

mkdir -p "$STATE_DIR/logs"

if [[ -s "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE")"
  if kill -0 "$old_pid" >/dev/null 2>&1; then
    kill "$old_pid"
    for _ in {1..20}; do
      if ! kill -0 "$old_pid" >/dev/null 2>&1; then
        break
      fi
      sleep 0.25
    done
  fi
fi

while IFS= read -r old_pid; do
  if [[ -n "$old_pid" ]]; then
    kill "$old_pid" >/dev/null 2>&1 || true
  fi
done < <(pgrep -f "[s]plitter_metric_tree_app.py" || true)

for _ in {1..40}; do
  if ! pgrep -f "[s]plitter_metric_tree_app.py" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if pgrep -f "[s]plitter_metric_tree_app.py" >/dev/null 2>&1; then
  pkill -9 -f "[s]plitter_metric_tree_app.py" >/dev/null 2>&1 || true
  sleep 0.25
fi

cd "$REPO_DIR"
nohup bash scripts/run-splitter-metric-tree-app.sh >>"$LOG_FILE" 2>&1 &
new_pid="$!"
echo "$new_pid" > "$PID_FILE"

for _ in {1..40}; do
  if curl -fsS "http://127.0.0.1:${SPLITTER_TREE_PORT:-8788}/healthz" >/dev/null 2>&1; then
    echo "splitter metric tree started: pid=$new_pid"
    exit 0
  fi
  if ! kill -0 "$new_pid" >/dev/null 2>&1; then
    echo "splitter metric tree exited during startup; see $LOG_FILE" >&2
    exit 1
  fi
  sleep 0.25
done

echo "splitter metric tree did not become healthy; see $LOG_FILE" >&2
exit 1
