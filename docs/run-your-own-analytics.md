# Run Your Own Publishing Analytics

The shortest path is one shell script:

```bash
bash scripts/run-warehouse-analytics.sh
```

That command exports the normalized command-cost table, loads it into BigQuery, loads it into ClickHouse, and creates the matching Metabase dashboards.

## 1. Create An Env File

Create `config/warehouse-analytics.env` locally. Do not commit it. The script automatically sources this file when it exists.

```bash
cp config/warehouse-analytics.env.example config/warehouse-analytics.env
```

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json
export BIGQUERY_PROJECT_ID=your-gcp-project
export BIGQUERY_DATASET=session_metrics_demo

export CLICKHOUSE_HOST=your-clickhouse-host
export CLICKHOUSE_PORT=8443
export CLICKHOUSE_USER=default
export CLICKHOUSE_PASSWORD=...
export CLICKHOUSE_DATABASE=session_metrics_demo

export METABASE_URL=https://your-metabase.example.com
export METABASE_API_KEY=...
```

If the Metabase database connections already exist, add their IDs so the script reuses them instead of creating new connections:

```bash
export METABASE_BIGQUERY_DATABASE_ID=123
export METABASE_CLICKHOUSE_DATABASE_ID=456
```

## 2. Run It

```bash
bash scripts/run-warehouse-analytics.sh
```

By default, the script uses:

```bash
reports/usage-command-attribution-v4_5.csv
```

and writes:

```bash
reports/warehouse-command-costs-v4_5.csv
reports/warehouse-command-costs-v4_5-summary.json
```

To use your own attribution CSV:

```bash
bash scripts/run-warehouse-analytics.sh \
  --input reports/my-command-attribution.csv \
  --output reports/my-warehouse-command-costs.csv \
  --summary-output reports/my-warehouse-command-costs-summary.json
```

## What The Script Creates

The script runs these steps in order:

```bash
python3 scripts/warehouse_cost_demo.py validate-local ...
python3 scripts/warehouse_cost_demo.py load-bigquery ... --skip-export
python3 scripts/warehouse_cost_demo.py load-clickhouse ... --skip-export
python3 scripts/warehouse_cost_demo.py create-metabase
```

The Metabase step creates:

- `Session Cost Demo - BigQuery`
- `Session Cost Demo - ClickHouse`

Each dashboard includes cost, token, phase, efficiency, intention, origin, model, and session drilldown cards.

## Verify Before Running

Use dry-run mode to confirm the exact commands without credentials or external writes:

```bash
bash scripts/run-warehouse-analytics.sh --dry-run
```

You should see `create-metabase` in the printed command list. That is the dashboard creation step.
