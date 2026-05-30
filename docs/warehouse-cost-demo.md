# Warehouse Cost Analytics Demo

This demo exports the cleaned v4.5 command attribution report into a compact one-row-per-command table, then can load the same table into BigQuery and ClickHouse for matching Metabase dashboards.

Source data:

```bash
reports/usage-command-attribution-v4_5.csv
```

One-command full run:

```bash
bash scripts/run-warehouse-analytics.sh
```

That wrapper validates the local export, loads BigQuery, loads ClickHouse, and creates the matching Metabase dashboards. See `docs/run-your-own-analytics.md` for the full setup.

Local export only:

```bash
python3 scripts/warehouse_cost_demo.py validate-local --expect-full-row-count
```

The normalized output is:

```bash
reports/warehouse-command-costs-v4_5.csv
```

It keeps `session_id`, phase fields, core dimensions, token metrics, total allocated cost, and numeric component-cost columns. It intentionally drops previews, hashes, file paths, `workdir`, delegated task text, terminal context fields, confidence/source/debug metadata, and it does not create a `usage_command_cost_component` fanout table.

## Credentials

Use environment variables, not committed config:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
export BIGQUERY_PROJECT_ID=...
export BIGQUERY_DATASET=session_metrics_demo

export CLICKHOUSE_HOST=...
export CLICKHOUSE_PORT=8443
export CLICKHOUSE_USER=...
export CLICKHOUSE_PASSWORD=...
export CLICKHOUSE_DATABASE=session_metrics_demo

export METABASE_URL=https://...
export METABASE_API_KEY=...
```

The BigQuery loader uses the `bq` CLI. ClickHouse loading uses HTTPS with the credentials above. Metabase setup uses API-key auth.

## Load

BigQuery:

```bash
python3 scripts/warehouse_cost_demo.py load-bigquery
```

ClickHouse:

```bash
python3 scripts/warehouse_cost_demo.py load-clickhouse
```

Metabase dashboards:

```bash
python3 scripts/warehouse_cost_demo.py create-metabase
```

If the Metabase database connections already exist, set:

```bash
export METABASE_BIGQUERY_DATABASE_ID=...
export METABASE_CLICKHOUSE_DATABASE_ID=...
```

The dashboards created are `Session Cost Demo - BigQuery` and `Session Cost Demo - ClickHouse`, each with matching cost, token, phase, efficiency, intention, origin, model, and session drilldown cards.

By default the Metabase command queries each card after creation and fails if a card returns no rows. Use `--skip-card-validation` only when the warehouse connection has not finished syncing yet.
