# Autoresearch sample — cost breakdown (2026-07-18)

## ML experiment loop (autoresearch-mlx)

Repo: `/Users/edbertchan/Documents/GitHub/autoresearch-mlx`  
Branch: `autoresearch/jul18`

| Commit | val_bpb | Memory GB | Status | Description |
|---|---:|---:|---|---|
| `766a25f` | 1.908083 | 8.4 | keep | baseline |
| `9eb8805` | 1.652357 | 8.4 | keep | halve `TOTAL_BATCH_SIZE` to `2^15` |

Δ val_bpb: **−0.255726** (−13.4% vs baseline). Steps 230 → 478 in the same 5‑minute budget.

Local MLX `train.py` wall-clock is **not** fleet LLM spend. Cost Explorer attributes the **agent session** that drove the loop.

## Taxonomy buckets (session-metrics-cron)

Added closed-list labels so this work is not buried in `uncategorized`:

| Axis | Id |
|---|---|
| `task_type` | `autoresearch` |
| `request_pattern` domain | `autoresearch` |
| `request_pattern` leaf | `autoresearch_experiment_loop` |

Configs: `config/task-categorization.yaml`, `config/request-patterns.yaml` (+ builtin defaults in `scripts/mixpanel_export_usage.py`).

Verified classification on the sample prompt:

- `task_type` = `autoresearch`
- `request_pattern` = `autoresearch_experiment_loop`
- path = `other/autoresearch/autoresearch_experiment_loop`

## Cost Explorer slice

Artifacts:

- Attribution CSV: `reports/autoresearch-jul18-sample-attribution.csv`
- Explorer output: `reports/cost-explorer-autoresearch-jul18/`

| KPI | Value |
|---|---|
| Total attributed cost | **$4.80** |
| Prompt windows / commands | 1 / 5 |
| Context / prompt-window | $0.71 |
| Cache-read | $1.46 |
| Output | $2.64 |

Filter chips (100% of this slice):

- Task type **Autoresearch** — $4.80
- Request pattern **autoresearch_experiment_loop** — $4.80

Top `agent_tool_intention` within the slice:

| Intention | Cost |
|---|---:|
| test_execution | $2.65 |
| feature_implementation_edit | $1.10 |
| environment_initialization | $0.75 |
| analytics_inspection | $0.30 |

Fixing causes observed (efficiency narrative, not a new domain bucket):

- Orientation in service of fixing — $0.75
- Expected failure investigation overhead — $0.30

## How to view in the live hub

```bash
# serve the sample explorer static summary, or merge into the main refresh later
bash scripts/run-local-insights.sh
# Cost Explorer filters: task type = Autoresearch, request pattern = autoresearch_experiment_loop
```

**Note:** This Cursor agent chat is not ingested by the Codex/OMP collectors (`~/.codex/sessions`, `~/.omp/agent/sessions`). The `$4.80` slice is a representative attribution window for the jul18 sample so the new buckets are exercisable in Cost Explorer. Overnight runs that edit `autoresearch-mlx` via Codex/OMP will classify automatically once nightly attribution + `cost_explorer_report.py` refresh.
