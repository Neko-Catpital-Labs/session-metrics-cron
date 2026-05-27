#!/usr/bin/env python3
"""Shared token pricing helpers for usage exports and benchmarks."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_PRICING_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"

COST_FORMULA = (
    "derived_total_cost_usd = "
    "billable_non_cache_input_tokens * pricing_input_cost_per_token + "
    "cache_read_tokens * pricing_cache_read_input_token_cost + "
    "cache_creation_tokens * pricing_cache_creation_input_token_cost + "
    "output_tokens * pricing_output_cost_per_token"
)


def none_if_missing(value: float | None) -> float | None:
    return value if value is not None else None


def normalize_model_name(value: str) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def default_billable_model(provider: str) -> tuple[str, str]:
    if provider in {"codex", "openai"}:
        return "gpt-5.5", "default_codex"
    if provider in {"claude", "anthropic"}:
        return "claude-sonnet-4-5-20250929", "default_claude"
    return "", "missing"


def provider_for_session_family(model: str) -> str:
    if model == "codex":
        return "openai"
    if model == "claude":
        return "anthropic"
    return ""


def execution_surface_for_mode(mode: str) -> str:
    return "baseline" if mode == "baseline_direct" else "invoker"


def autofix_enabled_for_mode(mode: str) -> bool:
    return mode == "invoker_auto_fix"


def resolve_billable_model(provider: str, observed: str) -> tuple[str, str]:
    normalized = normalize_model_name(observed)
    if normalized:
        return normalized, "session_log"
    return default_billable_model(provider)


def load_pricing_table(path_or_url: str | None) -> dict[str, Any]:
    source = path_or_url or DEFAULT_PRICING_URL
    try:
        if source.startswith(("http://", "https://")):
            with urllib.request.urlopen(source, timeout=20) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        path = Path(source).expanduser()
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        return {}
    return {}


def pricing_for_model(pricing_table: dict[str, Any], model: str) -> dict[str, Any] | None:
    if not model:
        return None
    candidates = [model, model.lower(), model.replace("openai/", ""), model.replace("anthropic/", "")]
    for candidate in candidates:
        row = pricing_table.get(candidate)
        if isinstance(row, dict):
            return row
    return None


def price_component(pricing: dict[str, Any] | None, *keys: str) -> float | None:
    if not pricing:
        return None
    for key in keys:
        value = pricing.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def derive_cost(
    pricing_table: dict[str, Any],
    billable_model: str,
    *,
    input_tokens: float,
    cache_read_tokens: float,
    cache_creation_tokens: float,
    output_tokens: float,
    input_includes_cache: bool = False,
) -> dict[str, Any]:
    pricing = pricing_for_model(pricing_table, billable_model)
    input_price = price_component(pricing, "input_cost_per_token", "prompt_cost_per_token")
    output_price = price_component(pricing, "output_cost_per_token", "completion_cost_per_token")
    cache_read_price = price_component(pricing, "cache_read_input_token_cost", "cache_read_cost_per_token")
    cache_creation_price = price_component(pricing, "cache_creation_input_token_cost", "cache_creation_cost_per_token")
    pricing_missing = input_price is None or output_price is None
    billable_input_tokens = max(0.0, input_tokens - cache_read_tokens - cache_creation_tokens) if input_includes_cache else input_tokens
    effective_cache_read_price = cache_read_price if cache_read_price is not None else input_price
    effective_cache_creation_price = cache_creation_price if cache_creation_price is not None else input_price
    input_cost = billable_input_tokens * input_price if input_price is not None else None
    cache_read_cost = cache_read_tokens * effective_cache_read_price if effective_cache_read_price is not None else None
    cache_creation_cost = cache_creation_tokens * effective_cache_creation_price if effective_cache_creation_price is not None else None
    output_cost = output_tokens * output_price if output_price is not None else None
    parts = [input_cost, cache_read_cost, cache_creation_cost, output_cost]
    return {
        "derived_input_cost_usd": none_if_missing(input_cost),
        "derived_non_cache_input_cost_usd": none_if_missing(input_cost),
        "derived_cache_read_cost_usd": none_if_missing(cache_read_cost),
        "derived_cache_creation_cost_usd": none_if_missing(cache_creation_cost),
        "derived_output_cost_usd": none_if_missing(output_cost),
        "derived_total_cost_usd": sum(p for p in parts if p is not None) if not pricing_missing else None,
        "billable_non_cache_input_tokens": billable_input_tokens,
        "pricing_input_cost_per_token": input_price,
        "pricing_cache_read_input_token_cost": effective_cache_read_price,
        "pricing_cache_creation_input_token_cost": effective_cache_creation_price,
        "pricing_output_cost_per_token": output_price,
        "cost_formula": COST_FORMULA,
        "pricing_missing": pricing_missing,
        "pricing_source": "litellm_model_prices" if pricing else "missing",
    }


def build_cost_calculation(
    *,
    batch_id: str,
    run_id: str,
    test_id: str,
    model: str,
    scenario: str,
    billable_model: str,
    billable_model_source: str,
    token_totals: dict[str, Any],
    cost: dict[str, Any],
) -> dict[str, Any]:
    """Build a benchmark cost audit payload from canonical derived-cost output."""

    return {
        "schema_version": "usage_costing_v1",
        "batch_id": batch_id,
        "run_id": run_id,
        "test_id": test_id,
        "model": model,
        "scenario": scenario,
        "billable_model": billable_model,
        "billable_model_source": billable_model_source,
        "pricing_source": cost.get("pricing_source"),
        "pricing_missing": cost.get("pricing_missing"),
        "token_inputs": {
            "input_tokens": token_totals.get("input_tokens", 0),
            "cache_read_tokens": token_totals.get("cache_read_tokens", 0),
            "cache_creation_tokens": token_totals.get("cache_creation_tokens", 0),
            "fresh_input_tokens": token_totals.get("fresh_input_tokens", 0),
            "output_tokens": token_totals.get("output_tokens", 0),
            "reasoning_tokens": token_totals.get("reasoning_tokens", 0),
            "total_tokens": token_totals.get("total_tokens", 0),
            "normalized_total_tokens": token_totals.get("normalized_total_tokens", 0),
        },
        "billable_token_math": {
            "input_includes_cache": bool(token_totals.get("input_includes_cache")),
            "billable_non_cache_input_tokens": cost.get("billable_non_cache_input_tokens"),
        },
        "unit_prices_usd_per_token": {
            "input": cost.get("pricing_input_cost_per_token"),
            "cache_read": cost.get("pricing_cache_read_input_token_cost"),
            "cache_creation": cost.get("pricing_cache_creation_input_token_cost"),
            "output": cost.get("pricing_output_cost_per_token"),
        },
        "component_costs_usd": {
            "non_cache_input": cost.get("derived_non_cache_input_cost_usd"),
            "cache_read": cost.get("derived_cache_read_cost_usd"),
            "cache_creation": cost.get("derived_cache_creation_cost_usd"),
            "output": cost.get("derived_output_cost_usd"),
        },
        "final_total_cost_usd": cost.get("derived_total_cost_usd"),
        "formula": cost.get("cost_formula") or COST_FORMULA,
    }
