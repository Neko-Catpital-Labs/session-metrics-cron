#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEFAULT_ENV_FILE="$REPO_ROOT/config/warehouse-analytics.env"
ENV_FILE=""
INPUT="$REPO_ROOT/reports/usage-command-attribution-v4_5.csv"
OUTPUT="$REPO_ROOT/reports/warehouse-command-costs-v4_5.csv"
SUMMARY_OUTPUT="$REPO_ROOT/reports/warehouse-command-costs-v4_5-summary.json"
PRICING_TABLE="${USAGE_PRICING_TABLE:-}"
LIMIT=0
EXPECT_FULL_ROW_COUNT=0
SKIP_METABASE=0
SKIP_CARD_VALIDATION=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: bash scripts/run-warehouse-analytics.sh [options]

Runs the full warehouse analytics demo:
  1. export and validate the normalized command-cost table
  2. load BigQuery
  3. load ClickHouse
  4. create Metabase dashboards, unless --skip-metabase is set

Required env for a real run:
  GOOGLE_APPLICATION_CREDENTIALS
  BIGQUERY_PROJECT_ID
  BIGQUERY_DATASET                 optional, default: session_metrics_demo
  CLICKHOUSE_HOST
  CLICKHOUSE_PORT                  optional, default: 8443
  CLICKHOUSE_USER
  CLICKHOUSE_PASSWORD
  CLICKHOUSE_DATABASE              optional, default: session_metrics_demo
  METABASE_URL                     unless --skip-metabase
  METABASE_API_KEY                 unless --skip-metabase

Optional env when Metabase database connections already exist:
  METABASE_BIGQUERY_DATABASE_ID
  METABASE_CLICKHOUSE_DATABASE_ID

Options:
  --env-file PATH                  Source env vars before running.
                                   Default: config/warehouse-analytics.env when present.
  --input PATH                     Source attribution CSV.
  --output PATH                    Normalized warehouse CSV.
  --summary-output PATH            Local summary JSON.
  --pricing-table PATH_OR_URL      Override model pricing table.
  --limit N                        Limit source rows for smoke runs.
  --expect-full-row-count          Require the bundled v4.5 report row count.
  --skip-metabase                  Load warehouses but do not create dashboards.
  --skip-card-validation           Create dashboards without querying each card.
  --dry-run                        Print the commands that would run.
  -h, --help                       Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      [[ -n "$ENV_FILE" ]] || { echo "Missing value for --env-file" >&2; exit 2; }
      shift 2
      ;;
    --input)
      INPUT="${2:-}"
      [[ -n "$INPUT" ]] || { echo "Missing value for --input" >&2; exit 2; }
      shift 2
      ;;
    --output)
      OUTPUT="${2:-}"
      [[ -n "$OUTPUT" ]] || { echo "Missing value for --output" >&2; exit 2; }
      shift 2
      ;;
    --summary-output)
      SUMMARY_OUTPUT="${2:-}"
      [[ -n "$SUMMARY_OUTPUT" ]] || { echo "Missing value for --summary-output" >&2; exit 2; }
      shift 2
      ;;
    --pricing-table)
      PRICING_TABLE="${2:-}"
      [[ -n "$PRICING_TABLE" ]] || { echo "Missing value for --pricing-table" >&2; exit 2; }
      shift 2
      ;;
    --limit)
      LIMIT="${2:-}"
      [[ "$LIMIT" =~ ^[0-9]+$ ]] || { echo "Invalid --limit: $LIMIT" >&2; exit 2; }
      shift 2
      ;;
    --expect-full-row-count)
      EXPECT_FULL_ROW_COUNT=1
      shift
      ;;
    --skip-metabase)
      SKIP_METABASE=1
      shift
      ;;
    --skip-card-validation)
      SKIP_CARD_VALIDATION=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$ENV_FILE" && -f "$DEFAULT_ENV_FILE" ]]; then
  ENV_FILE="$DEFAULT_ENV_FILE"
fi

if [[ -n "$ENV_FILE" ]]; then
  [[ -f "$ENV_FILE" ]] || { echo "Missing env file: $ENV_FILE" >&2; exit 1; }
  # shellcheck disable=SC1090
  set -a
  source "$ENV_FILE"
  set +a
fi

cd "$REPO_ROOT"

require_env() {
  local name="$1"
  [[ -n "${!name:-}" ]] || {
    echo "Missing required env: $name" >&2
    exit 1
  }
}

preflight() {
  [[ -f "$INPUT" ]] || {
    echo "Missing input CSV: $INPUT" >&2
    exit 1
  }
  require_env GOOGLE_APPLICATION_CREDENTIALS
  [[ -f "$GOOGLE_APPLICATION_CREDENTIALS" ]] || {
    echo "GOOGLE_APPLICATION_CREDENTIALS does not point to a file: $GOOGLE_APPLICATION_CREDENTIALS" >&2
    exit 1
  }
  require_env BIGQUERY_PROJECT_ID
  require_env CLICKHOUSE_HOST
  require_env CLICKHOUSE_USER
  require_env CLICKHOUSE_PASSWORD
  if [[ "$SKIP_METABASE" -eq 0 ]]; then
    require_env METABASE_URL
    require_env METABASE_API_KEY
  fi
  command -v bq >/dev/null || {
    echo "Missing bq CLI on PATH" >&2
    exit 1
  }
}

run_cmd() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  if [[ "$DRY_RUN" -eq 0 ]]; then
    "$@"
  fi
}

if [[ "$DRY_RUN" -eq 0 ]]; then
  preflight
fi

EXPORT_ARGS=(--input "$INPUT" --output "$OUTPUT" --summary-output "$SUMMARY_OUTPUT")
if [[ -n "$PRICING_TABLE" ]]; then
  EXPORT_ARGS+=(--pricing-table "$PRICING_TABLE")
fi
if [[ "$LIMIT" -gt 0 ]]; then
  EXPORT_ARGS+=(--limit "$LIMIT")
fi

VALIDATE_ARGS=("${EXPORT_ARGS[@]}")
if [[ "$EXPECT_FULL_ROW_COUNT" -eq 1 ]]; then
  VALIDATE_ARGS+=(--expect-full-row-count)
fi

run_cmd python3 scripts/warehouse_cost_demo.py validate-local "${VALIDATE_ARGS[@]}"
run_cmd python3 scripts/warehouse_cost_demo.py load-bigquery "${EXPORT_ARGS[@]}" --skip-export
run_cmd python3 scripts/warehouse_cost_demo.py load-clickhouse "${EXPORT_ARGS[@]}" --skip-export

if [[ "$SKIP_METABASE" -eq 0 ]]; then
  METABASE_ARGS=()
  if [[ "$SKIP_CARD_VALIDATION" -eq 1 ]]; then
    METABASE_ARGS+=(--skip-card-validation)
  fi
  run_cmd python3 scripts/warehouse_cost_demo.py create-metabase "${METABASE_ARGS[@]}"
fi
