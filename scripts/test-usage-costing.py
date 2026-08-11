#!/usr/bin/env python3
"""Unit tests for canonical usage costing helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from usage_costing import (
    DEFAULT_PRICING_URL,
    build_cost_calculation,
    derive_cost,
    load_pricing_table,
    resolve_billable_model,
)


def assert_close(actual: float | None, expected: float) -> None:
    assert actual is not None
    assert abs(actual - expected) < 1e-12, (actual, expected)


def test_cache_aware_cost() -> None:
    pricing = {
        "gpt-test": {
            "input_cost_per_token": 0.000001,
            "cache_read_input_token_cost": 0.0000001,
            "cache_creation_input_token_cost": 0.00000125,
            "output_cost_per_token": 0.000004,
        }
    }
    cost = derive_cost(
        pricing,
        "gpt-test",
        input_tokens=1000,
        cache_read_tokens=300,
        cache_creation_tokens=100,
        output_tokens=200,
        input_includes_cache=True,
    )
    assert cost["pricing_missing"] is False
    assert cost["billable_non_cache_input_tokens"] == 600
    assert_close(cost["derived_non_cache_input_cost_usd"], 0.0006)
    assert_close(cost["derived_cache_read_cost_usd"], 0.00003)
    assert_close(cost["derived_cache_creation_cost_usd"], 0.000125)
    assert_close(cost["derived_output_cost_usd"], 0.0008)
    assert_close(cost["derived_total_cost_usd"], 0.001555)

    audit = build_cost_calculation(
        batch_id="batch-1",
        run_id="test-1__codex__baseline_direct",
        test_id="test-1",
        model="codex",
        scenario="baseline_direct",
        billable_model="gpt-test",
        billable_model_source="session_log",
        token_totals={
            "input_tokens": 1000,
            "cache_read_tokens": 300,
            "cache_creation_tokens": 100,
            "fresh_input_tokens": 800,
            "output_tokens": 200,
            "reasoning_tokens": 50,
            "total_tokens": 1250,
            "normalized_total_tokens": 1050,
            "input_includes_cache": True,
        },
        cost=cost,
    )
    assert audit["schema_version"] == "usage_costing_v1"
    assert audit["final_total_cost_usd"] == cost["derived_total_cost_usd"]
    assert audit["unit_prices_usd_per_token"]["cache_read"] == 0.0000001
    assert "formula" in audit


def test_missing_pricing_and_model_resolution() -> None:
    assert resolve_billable_model("openai", " GPT-5.5 ")[0] == "gpt-5.5"
    cost = derive_cost({}, "missing-model", input_tokens=1, cache_read_tokens=0, cache_creation_tokens=0, output_tokens=1)
    assert cost["pricing_missing"] is True
    assert cost["derived_total_cost_usd"] is None


def test_load_pricing_table_defaults_to_litellm_url_without_env() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("LANGFUSE_PRICING_URL", None)
        os.environ.pop("LANGFUSE_HOST", None)

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b"{}"

        captured_urls: list[str] = []

        def fake_urlopen(url: str, timeout: int = 20) -> FakeResponse:
            captured_urls.append(url)
            return FakeResponse()

        with patch("usage_costing.urllib.request.urlopen", fake_urlopen):
            result = load_pricing_table(None)

        assert captured_urls == [DEFAULT_PRICING_URL]
        assert result == {}


def test_load_pricing_table_prefers_langfuse_pricing_url_env() -> None:
    fixture = {"langfuse-model": {"input_cost_per_token": 0.000002, "output_cost_per_token": 0.000005}}
    with tempfile.TemporaryDirectory() as tmp_dir:
        fixture_path = Path(tmp_dir) / "langfuse_pricing.json"
        fixture_path.write_text(json.dumps(fixture))

        with patch.dict(os.environ, {"LANGFUSE_PRICING_URL": str(fixture_path)}, clear=False):
            os.environ.pop("LANGFUSE_HOST", None)
            result = load_pricing_table(None)

    assert result == fixture


if __name__ == "__main__":
    test_cache_aware_cost()
    test_missing_pricing_and_model_resolution()
    test_load_pricing_table_defaults_to_litellm_url_without_env()
    test_load_pricing_table_prefers_langfuse_pricing_url_env()
    print("OK: usage costing")
