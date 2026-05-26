# Mixpanel Usage Cost Ops Board

Build this board after a live import of the nightly usage events. Do not store Mixpanel credentials in this repository.

Board name: `Usage Cost Ops`

## Event Families

- `usage_daily_rollup`
- `usage_session`
- `usage_prompt`
- `usage_tool_attribution`
- `usage_request_tool_attribution`
- `usage_tool_breakdown`
- `usage_cache_driver`

## Reports

### Cost Overview

- Daily cost by bucket: event `usage_session`, sum `derived_total_cost_usd`, line chart, x-axis `report_date`, breakdown `bucket`.
- Daily cost by provider/model: event `usage_session`, sum `derived_total_cost_usd`, line chart, x-axis `report_date`, breakdowns `provider`, `billable_model`.
- Planning vs execution share: event `usage_session`, sum `derived_total_cost_usd`, stacked bar, breakdown `bucket`.

### Token Breakdown

- Raw token components: event `usage_session`, sums of `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`, `reasoning_output_tokens`, and `total_tokens`, line chart by `report_date`.
- Token components by model: event `usage_session`, sum `total_tokens`, breakdowns `provider`, `billable_model`, `bucket`.

### Cache Economics

- Cache read/create tokens by model: event `usage_session`, sums `cache_read_input_tokens` and `cache_creation_input_tokens`, breakdown `billable_model`.
- Cache vs non-cache input cost: event `usage_session`, sums `derived_cache_read_cost_usd`, `derived_cache_creation_cost_usd`, and `derived_non_cache_input_cost_usd`, stacked over `report_date`.
- Cache hit percentage by planning/execution: event `usage_session`, average `cache_hit_pct`, breakdown `bucket`.

### Planning Tool Attribution

- Planning tool allocated cost: event `usage_tool_attribution`, filter `bucket = planning`, sum `allocated_total_cost_usd`, breakdown `name`.
- Planning tool allocated tokens: event `usage_tool_attribution`, filter `bucket = planning`, sum `allocated_total_tokens`, breakdown `name`.
- Function calls: event `usage_tool_attribution`, filters `bucket = planning` and `dimension = function_name`, sum `calls`, breakdown `name`.
- Shell verbs: event `usage_tool_attribution`, filters `bucket = planning` and `dimension = shell_verb`, sum `calls`, breakdown `name`.
- Shell cost share: event `usage_tool_attribution`, filters `bucket = planning`, `dimension = shell_verb`, and `name in git,sed,rg,cat`, sum `allocated_total_cost_usd`, breakdown `name`.

### Drilldowns

- Session table: event `usage_session`, properties `session_id`, `bucket`, `provider`, `billable_model`; metrics sum `derived_total_cost_usd`, sum `total_tokens`, sum `tool_calls`.
- Prompt table: event `usage_prompt`, properties `session_id`, `prompt_index`, `bucket`, `provider`, `billable_model`; metrics sum `derived_total_cost_usd`, sum `total_tokens_delta`, sum `tool_calls`.
- Tool attribution table: event `usage_tool_attribution`, properties `session_id`, `prompt_index`, `dimension`, `name`; metrics sum `calls`, sum `allocated_total_cost_usd`, sum `allocated_total_tokens`.
- Request command cost table: event `usage_request_tool_attribution`, properties `task_label`, `request_subpattern`, `session_id`, `dimension`, `name`; metrics sum `calls`, sum `allocated_total_cost_usd`, sum `allocated_total_tokens`.

## Notes

- `model` remains the client family (`codex` or `claude`).
- `provider` is the billing provider (`openai` or `anthropic`).
- `billable_model` is read from session logs where possible; otherwise it uses the parser default.
- `estimated_cost_usd` is the legacy ccusage-proportional estimate.
- `derived_*_cost_usd` fields are calculated from LiteLLM-style pricing rows when available.
- Missing pricing emits null derived costs with `pricing_missing = true`.
- `usage_tool_attribution` uses `allocation_method = prompt_window_even_split`; these are allocation estimates, not exact provider billing records.
- `usage_request_tool_attribution` uses the same allocation method and enriches rows with deterministic request labels from nearby prompt/session context.
