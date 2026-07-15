# Publishing Analytics Warehouse Demo

The `session-metrics` warehouse demo now publishes two tables from the same
v4.5 command-attribution run:

- `command_costs` — the compact legacy one-row-per-command table that keeps only
  the stable phase/dimension/cost fields used by the existing `/cost` charts.
- `cost_explorer_commands_v1` — the high-cardinality sidecar that keeps readable
  previews, request/task taxonomy, fixing-cause labels, and the hybrid
  context/cache/output buckets used by `/cost-explorer`.

Source data:

```bash
reports/usage-command-attribution-v4_5.csv
reports/cost-explorer-v1/commands.csv
```

One-command full run:

```bash
bash scripts/run-warehouse-analytics.sh
```

That wrapper validates the local exports, loads BigQuery, loads ClickHouse, and
creates the matching Metabase dashboards. See `docs/run-your-own-analytics.md`
for the full setup.

Local validation:

```bash
python3 scripts/cost_explorer_report.py --input reports/usage-command-attribution-v4_5.csv --output-dir reports/cost-explorer-v1 --request-pattern-config config/request-patterns.yaml --task-categorization-config config/task-categorization.yaml
python3 scripts/warehouse_cost_demo.py validate-local --expect-full-row-count
```

The compact normalized output is:

```bash
reports/warehouse-command-costs-v4_5.csv
```

The local explorer artifact family is:

```bash
reports/cost-explorer-v1/
├── summary.json
├── summary.md
├── windows.csv
├── commands.csv
└── windows/<window_file>.json
```

`command_costs` intentionally drops previews, hashes, file paths, `workdir`,
delegated task text, terminal context fields, confidence/source/debug metadata,
and it does not create a `usage_command_cost_component` fanout table.
`cost_explorer_commands_v1` keeps the readable fields the explorer needs, but it
still excludes the hash-only debug columns such as `command_hash`,
`stdin_hash`, and `delegated_task_hash`.

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
The explorer page reads the local artifact family plus the new APIs on
`scripts/splitter_metric_tree_app.py`:

- `/cost-explorer`
- `/api/cost-explorer-summary`
- `/api/cost-explorer-search`
- `/api/cost-explorer-window`

