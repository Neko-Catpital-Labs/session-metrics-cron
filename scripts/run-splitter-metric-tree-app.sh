#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${SESSION_METRICS_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DIR="${SESSION_METRICS_STATE_DIR:-$HOME/.local/state/workflow-analysis-service}"
BIGQUERY_ENV="${WORKFLOW_ANALYSIS_BIGQUERY_ENV:-$HOME/.config/workflow-analysis-service/bigquery.env}"
BIGQUERY_VENV="${WORKFLOW_ANALYSIS_BIGQUERY_VENV:-$HOME/.local/share/workflow-analysis-service/bigquery-venv}"
WORKFLOW_ANALYSIS_ROOT="${WORKFLOW_ANALYSIS_SERVICE_ROOT:-$HOME/workflow-analysis-service}"

if [[ -f "$BIGQUERY_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$BIGQUERY_ENV"
  set +a
fi

if [[ "${WORKFLOW_ANALYSIS_BIGQUERY_ENABLED:-1}" == "1" ]]; then
  if [[ ! -x "$BIGQUERY_VENV/bin/python" ]]; then
    python3 -m venv "$BIGQUERY_VENV"
    "$BIGQUERY_VENV/bin/python" -m pip install --upgrade pip
  fi
  if ! "$BIGQUERY_VENV/bin/python" -c 'import google.cloud.bigquery' >/dev/null 2>&1; then
    "$BIGQUERY_VENV/bin/python" -m pip install google-cloud-bigquery
  fi
  PYTHON="$BIGQUERY_VENV/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

export BIGQUERY_PROJECT_ID="${BIGQUERY_PROJECT_ID:-${SPLITTER_BIGQUERY_PROJECT_ID:-summer-nexus-137922}}"
export SPLITTER_BIGQUERY_LOCATION="${SPLITTER_BIGQUERY_LOCATION:-US}"
export WORKFLOW_ANALYSIS_SERVICE_ROOT="$WORKFLOW_ANALYSIS_ROOT"
export SPLITTER_TREE_HOST="${SPLITTER_TREE_HOST:-0.0.0.0}"
export SPLITTER_TREE_PORT="${SPLITTER_TREE_PORT:-8788}"

mkdir -p "$STATE_DIR/logs"
cd "$REPO_DIR"

exec "$PYTHON" scripts/splitter_metric_tree_app.py \
  --host "$SPLITTER_TREE_HOST" \
  --port "$SPLITTER_TREE_PORT" \
  --static-path docs/splitter-metric-tree-mvp.html \
  --rules-static-path docs/rules-d3-poc.html \
  --workflow-analysis-root "$WORKFLOW_ANALYSIS_ROOT"
