# Session Metrics Nightly Pipeline

`session-metrics` runs a nightly pipeline that:

1. audits cache-hit usage across local + SSH session sources,
2. builds planning-vs-execution reports,
3. exports high-detail events into Mixpanel.

## Required files

- `config/nightly-usage.env`
- `config/sources.json` (or override via env/CLI)
- `config/task-categorization.yaml` (optional; enables YAML regex rules plus Codex CLI classification for regex misses)
- `config/request-patterns.yaml` (optional; enables the shipped recursive request-pattern taxonomy, also available as the built-in fallback)

## Run commands

Dry run:

```bash
bash scripts/nightly_usage_pipeline.sh --dry-run --env-file config/nightly-usage.env
```

Fast dry run (reuse existing report artifacts):

```bash
bash scripts/nightly_usage_pipeline.sh --dry-run --env-file config/nightly-usage.env --skip-cache-audit --skip-report
```

Backfill/replay (Mixpanel-safe):

```bash
bash scripts/nightly_usage_pipeline.sh --date 2026-05-25 --env-file config/nightly-usage.env --ignore-local-state
```

## Dedupe model

- Every event includes deterministic `$insert_id`.
- Keys are canonicalized (session file stem, prompt index, source/value hashes).
- Dry runs do not mutate local state.
- Optional local suppression can be bypassed with `--ignore-local-state`.
- Per-session events (`usage_session`, `usage_prompt`, and `usage_tool_attribution`) use each session's `session_date` as Mixpanel `time`/`report_date`; `batch_report_date` records the export batch date.
- Daily timestamps are set at noon UTC to avoid normal project timezones displaying the event on the previous calendar day.
- Session rows after the requested `--date` are skipped so a backfill through yesterday does not import current-day/future timestamps.
- Corrected session-date imports are marked with `export_version = session_date_v3`; dashboards should filter to that value if older batch-date imports exist.
- `$insert_id` values are kept under Mixpanel's practical length limit with the hash near the front, so imports do not collapse high-cardinality rows that share the same date and event family prefix.

## Cost and attribution events

- `usage_session` and `usage_prompt` include raw token components, billing metadata, legacy `estimated_cost_usd`, and pricing-derived `derived_*_cost_usd` fields.
- `model` remains the client family (`codex` or `claude`); `provider` and `billable_model` identify the billing provider/model.
- `pricing_missing = true` means the exporter could not find a LiteLLM-style pricing row, so derived costs are null.
- `usage_request_cache_diagnosis` emits one row per prompt/request with exact per-request cache-read, cache-creation, output, reasoning, total token, cache-hit, and derived cost fields. It also labels the final recursive `request_pattern`, `request_pattern_path`, pattern depth/rule/confidence metadata, deterministic task label, and a primary likely cache driver.
- `usage_request_cache_diagnosis` and `usage_request_tool_attribution` include `task_type`, `task_type_label`, `task_type_confidence`, `task_type_classifier`, `task_type_reason`, `task_type_source`, and `task_type_config_version`. If no config path is supplied, the exporter uses built-in regex defaults only; set `USAGE_TASK_CATEGORIZATION_CONFIG=$PWD/config/task-categorization.yaml` or pass `--task-categorization-config` to enable the full YAML taxonomy and Codex fallback.
- Recursive request-pattern classification is independent of `task_type`. Set `USAGE_REQUEST_PATTERN_CONFIG=$PWD/config/request-patterns.yaml` or pass `--request-pattern-config`; if the path is omitted or missing, the exporter uses the built-in `request_pattern_layers_v1` config.
- `usage_request_cache_source` emits a bounded top-N source breakdown per request (default 3 via `MAX_CACHE_SOURCES_PER_REQUEST`) for visualizing which repeated context sources are likely contributing to cache hits.
- Request diagnosis rows are marked with `diagnosis_version = request_pattern_layers_v1`; this version is included in diagnosis `$insert_id` keys so future diagnosis-schema changes do not collide with earlier imports. `request_subpattern` is legacy historical data only and is omitted from new `request_pattern_layers_v1` diagnosis and request-tool attribution events.
- Request cache-source fields use `source_attribution_method = provider_metric_exact_source_estimated`: provider token and cache-hit metrics are exact, but cache-source attribution is derived from repeated-context audit rows because provider logs do not expose exact cache spans.
- `usage_tool_attribution` allocates prompt-window tokens and costs evenly across tool calls by `function_name` and `shell_verb` using `allocation_method = prompt_window_even_split`.
- `usage_request_tool_attribution` joins tool attribution to request context by `model + bucket + session_id + prompt_index`, adding the same request-pattern metadata and `task_label` for command-cost and session-cost breakdowns.
- Local request-pattern quality diagnostics can be run with `python3 scripts/request_pattern_quality_report.py --prompts-csv reports/planning-vs-execution-prompts.csv`. This command is not a portable CI gate; the committed fixture test is the deterministic quality gate.
- The Mixpanel board fallback spec is in `docs/mixpanel-usage-cost-ops-board.md`.

## launchd

Install:

```bash
bash scripts/install-nightly-usage-launchd.sh --env-file config/nightly-usage.env --time 02:10
```

Uninstall:

```bash
bash scripts/uninstall-nightly-usage-launchd.sh
```

## Logs/state

- `~/.session-metrics-cron/usage-metrics/nightly-run.log`
- `~/.session-metrics-cron/usage-metrics/send_state.json`
