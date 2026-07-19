# Insights snapshot values — 2026-07-18

Captured from the local insights server (`bash scripts/run-local-insights.sh`) after the
2026-07-18 fleet + warehouse refresh. Fact file generated at
`2026-07-18T21:16:28+00:00`. Attribution window: **2026-03-26 → 2026-07-18**.

Screenshots live next to this file.

## `/insights` — hub

Index of the four dashboards below. No numeric KPIs on this page.

## `/cost` — Fleet Cost Dashboard (default **30d**)

| KPI | Value |
|---|---|
| Modeled cost (list $/Mtok) | **$13,849.87** |
| Actual billed | **$14,161.89** |
| Δ vs billed | **-2.2% (−$312.02)** |
| Tokens | **19,167,420,769** |
| Days | **30** (2026-06-19 → 2026-07-18) |
| Fact rows | 1,174 |

Totals by harness × model (modeled, 30d):

| Category | Cost | Share | Tokens |
|---|---:|---:|---:|
| omp+codex | $6,541.22 | 47.2% | 8,391,649,152 |
| native+codex | $3,735.93 | 27.0% | 4,348,397,017 |
| native+claude | $1,929.07 | 13.9% | 3,206,851,485 |
| omp+claude | $1,643.66 | 11.9% | 3,220,523,115 |

Command analytics (warehouse, ~30d):

- Cache hit rate: **99.1%**
- Cost by intent (top): implementation planning inspection, failure diagnosis inspection, environment initialization, test execution, repo orientation (panel total ≈ **$13,713**)

All-time fleet fact total (for comparison with `/cost-summary`): **$20,286.89** across 79 days.

## `/fixing-cost` — Fixing cost explainer (all-time attribution)

| KPI | Value |
|---|---|
| Total attributed cost | **$20,901.66** |
| Context / prompt-window cost | **$3,733.55** |
| Cache-read cost | **$14,118.19** |
| Output cost | **$2,957.90** |
| Prompt windows | **10,427** |
| Commands | **304,920** |

Top fixing / CI causes:

| Cause | Cost | Windows | Commands |
|---|---:|---:|---:|
| Orientation in service of fixing | $7,292.97 | 8,001 | 102,442 |
| Expected failure investigation overhead | $3,850.11 | 4,264 | 41,355 |
| Failure diagnosis thrash | $1,865.75 | 2,123 | 24,448 |
| Repeated repair/test loops | $1,430.89 | 1,727 | 21,105 |
| CI/merge monitoring thrash | $914.87 | 1,424 | 13,173 |

## `/cost-explorer` — Cost Explorer

Search/detail over the same attribution corpus:

- Date range: **2026-03-26 → 2026-07-18**
- Commands indexed: **304,920**
- Default time chip: **all**
- Top request-pattern filter chips include `pr_review` (~**$6,759**), `auto_stamp_ci_loop`, `implementation_refactor`, etc.

## `/cost-summary` — Fleet Cost Summary (static HTML)

| KPI | Value |
|---|---|
| Total cost | **$20,286.89** |
| Total tokens | **28,310,029,583** |
| Prompts | **55,903** |
| Hosts | **8** |

Same all-time fleet fact as `/api/cost-timeseries` (`reports/cost-daily-fact.json`).

## How to refresh this snapshot

```bash
bash scripts/run-local-insights.sh
# then re-capture docs/screenshots/insights-YYYY-MM-DD/*.png and update this file
```
