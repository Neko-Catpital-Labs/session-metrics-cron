# Cost dashboard — run it yourself

A single-page dashboard served at `/cost` that shows:

- **Fleet spend** over time with a live **pricing what-if simulator** — drag any
  provider's `$/Mtok` and modeled cost recomputes against actual billed (top half).
- **Cache-hit %** and **cost-by-intent**, fleet-wide, straight from the warehouse
  (bottom half: "Command analytics").

It has two independent data lineages on purpose:

| Half | Source | How it's costed |
|---|---|---|
| Top (fleet cost) | `reports/cost-daily-fact.json` — token totals per host/origin/model/day | omp-derived effective rates (ccusage-free) |
| Bottom (command analytics) | BigQuery `command_costs` table — per-command intent + tokens | pricing-table list prices |

Because they use different cost models, their totals differ slightly; the UI labels
this. Both are correct for what they measure.

## Architecture

- `scripts/splitter_metric_tree_app.py` is the web server. Routes:
  - `/cost` → serves `docs/cost-dashboard.html` (per request; no build step).
  - `/api/cost-timeseries` → the fleet cost fact JSON (top half).
  - `/api/cache-hit`, `/api/usage-by-intent` → BigQuery proxy for the bottom half
    (server-side; credentials never reach the browser; 60s cache; `?from=&to=`
    honor the dashboard's time-range control).
- `docs/cost-dashboard.html` is the whole UI (vanilla JS + Chart.js). Edit and
  redeploy by copying the file — no bundler.

## Prerequisites

- `python3` (the server itself is stdlib-only; the warehouse proxy needs
  `google-cloud-bigquery`, installed into the runner's venv — see
  `docs/run-your-own-analytics.md`).
- A Google Cloud project with BigQuery, a **service-account JSON** with BigQuery
  read/write, and `bq`/`gcloud` authenticated as that account.
- Codex / Claude / omp session logs on this machine and/or reachable SSH hosts.
- A machine to serve from (a small VM, or just run it locally).

## 1. Configure

```bash
cp config/warehouse-analytics.env.example config/warehouse-analytics.env
# then edit and set at minimum:
#   GOOGLE_APPLICATION_CREDENTIALS=/abs/path/to/service-account.json
#   BIGQUERY_PROJECT_ID=your-gcp-project
#   BIGQUERY_DATASET=session_metrics_demo
```

Setting `BIGQUERY_PROJECT_ID` + `BIGQUERY_DATASET` repoints every table default
(metric tree, `command_costs`) at *your* project automatically.

Fleet hosts come from `~/.invoker/config.json` (`remoteTargets`) or
`config/sources.json`. Local-only works too — it just collects this machine.

## 2. Build the data

```bash
# Top half — fleet cost fact (collects sessions across local + SSH hosts,
# writes reports/cost-daily-fact.json, and copies it to the dashboard host):
DO1_HOST=invoker@YOUR_HOST bash scripts/refresh-fleet-cost-do1.sh

# Bottom half — warehouse command analytics (collects fleet sessions, builds the
# v4.5 command-attribution CSV, then `bq load --replace` into
# <project>.<dataset>.command_costs plus its views):
set -a; . config/warehouse-analytics.env; set +a
bash scripts/refresh-warehouse-analytics.sh
```

Both are **dedup-safe**: fleet sessions are de-duplicated by file content hash, and
the warehouse load fully replaces the table (`bq load --replace`) with a built-in
row/cost parity check. Re-running never double-counts.

## 3. Serve the dashboard

```bash
# Local:
set -a; . config/warehouse-analytics.env; set +a
bash scripts/run-splitter-metric-tree-app.sh        # -> http://127.0.0.1:8788/cost

# On a server (deploy/restart, health-checked, idempotent):
bash scripts/install-splitter-metric-tree-do1.sh
```

HTML-only changes need no restart (the file is served per request) — just copy
`docs/cost-dashboard.html` to the host. Code changes to the server need a restart
(the install script handles it).

## 4. Schedule daily refreshes (optional)

```bash
bash scripts/install-fleet-cost-cron.sh        # 7:00 daily — top half
bash scripts/install-warehouse-cron.sh         # 7:30 daily — bottom half
```

These are workstation crontab entries, so they only fire while the machine is awake.
Logs land in `~/.session-metrics-cron/`.

## 5. Reach it from your laptop (optional)

The dashboard binds to the server's loopback, so tunnel to it:

```bash
# One-off:
ssh -fN -L 8899:127.0.0.1:8788 invoker@YOUR_HOST   # -> http://127.0.0.1:8899/cost

# Persistent on macOS (auto-starts at login, auto-restarts if it drops):
TUNNEL_SSH_TARGET=invoker@YOUR_HOST bash scripts/install-cost-tunnel-launchagent.sh
```

## Configuration reference

| Env var | Default | Used by |
|---|---|---|
| `BIGQUERY_PROJECT_ID` | `summer-nexus-137922` | server, warehouse load |
| `BIGQUERY_DATASET` | `session_metrics_demo` | warehouse table default |
| `GOOGLE_APPLICATION_CREDENTIALS` | — (required for loads) | warehouse load |
| `WAREHOUSE_COMMAND_COSTS_TABLE` | `<project>.<dataset>.command_costs` | `/api/*` proxy |
| `SPLITTER_TREE_PORT` | `8788` | server bind port |
| `DO1_HOST` | `invoker@157.230.133.215` | fleet-cost refresh target |
| `TUNNEL_SSH_TARGET` / `TUNNEL_LOCAL_PORT` / `TUNNEL_REMOTE_PORT` | `invoker@157.230.133.215` / `8899` / `8788` | tunnel installer |

## Notes & gotchas

- **Secrets are gitignored**: `config/warehouse-analytics.env` and the
  service-account JSON are never committed. Set them per machine.
- **Pricing levers are client-side only** — they recompute the displayed cost in the
  browser; a refresh resets to the published list-price baseline. No backend change.
- **Scope**: the warehouse attribution is fleet-wide (local + SSH hosts). The
  attribution cost model is the pricing table, so its total differs from the
  omp-derived fleet cost on the top half — this is expected and labeled.
- The warehouse data is a normal BigQuery table; you can also point Metabase at it
  (see `docs/warehouse-cost-demo.md`).
