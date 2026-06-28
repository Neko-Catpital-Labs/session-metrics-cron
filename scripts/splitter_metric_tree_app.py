#!/usr/bin/env python3
"""Serve the Splitter metric tree UI backed by BigQuery."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATIC_PATH = REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html"
DEFAULT_RULES_STATIC_PATH = REPO_ROOT / "docs" / "rules-d3-poc.html"
DEFAULT_STEPS_STATIC_PATH = REPO_ROOT / "docs" / "rules-steps.html"
DEFAULT_RULES_D3_POC_STATIC_PATH = REPO_ROOT / "docs" / "rules-d3-poc.html"
DEFAULT_COST_REPORT_PATH = REPO_ROOT / "reports" / "invoker-cost-breakdown.html"
DEFAULT_COST_DASHBOARD_PATH = REPO_ROOT / "docs" / "cost-dashboard.html"
DEFAULT_COST_FACT_PATH = REPO_ROOT / "reports" / "cost-daily-fact.json"
DEFAULT_CHART_ASSET_PATH = REPO_ROOT / "docs" / "vendor" / "chart.umd.min.js"
DEFAULT_WORKFLOW_ANALYSIS_ROOT = Path(
    os.environ.get(
        "WORKFLOW_ANALYSIS_SERVICE_ROOT",
        str(REPO_ROOT.parent / "workflow-analysis-service"),
    )
)
DEFAULT_TABLE = "summer-nexus-137922.splitter_metrics.splitter_replay_metric_scores_over_time"
DEFAULT_PROJECT = "summer-nexus-137922"
DEFAULT_METRIC_PATH = "planToResponseGraphScore"
DEFAULT_VARIANT = "hinted"
DEFAULT_HISTORY_RUNS = 20
MAX_HISTORY_RUNS = 100
MAX_METRIC_PATH_LENGTH = 512
DEFAULT_CACHE_TTL_SECONDS = 60
DEFAULT_METABASE_DATABASE_ID = 2
DEFAULT_BIGQUERY_LOCATION = "US"
DIAGNOSTIC_SCORE_PARENTS = {
    "correctStackLinkCount": "correctStackLinksScore",
    "extraStackLinkCount": "noExtraStackLinksScore",
    "missingStackLinkCount": "noMissingStackLinksScore",
    "plannedCorrectStackLinkCount": "plannedCorrectStackLinksScore",
    "plannedExtraStackLinkCount": "plannedNoExtraStackLinksScore",
    "plannedMissingStackLinkCount": "plannedNoMissingStackLinksScore",
    "responseCorrectStackLinkCount": "responseCorrectStackLinksScore",
    "responseExtraStackLinkCount": "responseNoExtraStackLinksScore",
    "responseMissingStackLinkCount": "responseNoMissingStackLinksScore",
}
FORMULAS = {
    "correctStackLinksScore": "correct links / max(expected links, actual links, 1)",
    "plannedCorrectStackLinksScore": "correct links / max(expected links, actual links, 1)",
    "responseCorrectStackLinksScore": "correct links / max(expected links, actual links, 1)",
    "noExtraStackLinksScore": "1 - extra links / max(actual links, 1)",
    "plannedNoExtraStackLinksScore": "1 - extra links / max(actual links, 1)",
    "responseNoExtraStackLinksScore": "1 - extra links / max(actual links, 1)",
    "noMissingStackLinksScore": "1 - missing links / max(expected links, 1)",
    "plannedNoMissingStackLinksScore": "1 - missing links / max(expected links, 1)",
    "responseNoMissingStackLinksScore": "1 - missing links / max(expected links, 1)",
    "stackLinkCorrectnessScore": "0.34 * Correct + 0.33 * No Extra + 0.33 * No Missing",
    "plannedStackLinkCorrectnessScore": "0.34 * Correct + 0.33 * No Extra + 0.33 * No Missing",
    "responseStackLinkCorrectnessScore": "0.34 * Correct + 0.33 * No Extra + 0.33 * No Missing",
}
STACK_LINK_SPECS = {
    "correctStackLinksScore": ("correctStackLinkCount", "extraStackLinkCount", "missingStackLinkCount"),
    "plannedCorrectStackLinksScore": (
        "plannedCorrectStackLinkCount",
        "plannedExtraStackLinkCount",
        "plannedMissingStackLinkCount",
    ),
    "responseCorrectStackLinksScore": (
        "responseCorrectStackLinkCount",
        "responseExtraStackLinkCount",
        "responseMissingStackLinkCount",
    ),
}
NO_EXTRA_SPECS = {
    "noExtraStackLinksScore": ("extraStackLinkCount", "correctStackLinkCount"),
    "plannedNoExtraStackLinksScore": ("plannedExtraStackLinkCount", "plannedCorrectStackLinkCount"),
    "responseNoExtraStackLinksScore": ("responseExtraStackLinkCount", "responseCorrectStackLinkCount"),
}
NO_MISSING_SPECS = {
    "noMissingStackLinksScore": ("missingStackLinkCount", "correctStackLinkCount"),
    "plannedNoMissingStackLinksScore": ("plannedMissingStackLinkCount", "plannedCorrectStackLinkCount"),
    "responseNoMissingStackLinksScore": ("responseMissingStackLinkCount", "responseCorrectStackLinkCount"),
}
STACK_LINK_AGGREGATES = {
    "stackLinkCorrectnessScore": [
        ("Correct links score", "correctStackLinksScore", 0.34),
        ("No extra links score", "noExtraStackLinksScore", 0.33),
        ("No missing links score", "noMissingStackLinksScore", 0.33),
    ],
    "plannedStackLinkCorrectnessScore": [
        ("Correct links score", "plannedCorrectStackLinksScore", 0.34),
        ("No extra links score", "plannedNoExtraStackLinksScore", 0.33),
        ("No missing links score", "plannedNoMissingStackLinksScore", 0.33),
    ],
    "responseStackLinkCorrectnessScore": [
        ("Correct links score", "responseCorrectStackLinksScore", 0.34),
        ("No extra links score", "responseNoExtraStackLinksScore", 0.33),
        ("No missing links score", "responseNoMissingStackLinksScore", 0.33),
    ],
}


@dataclass
class MetricTreeQuery:
    metric_path: str = DEFAULT_METRIC_PATH
    variant: str = DEFAULT_VARIANT
    history_runs: int = DEFAULT_HISTORY_RUNS


class TTLCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.items: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self.items.get(key)
        if not item:
            return None
        expires_at, value = item
        if time.time() >= expires_at:
            self.items.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self.items[key] = (time.time() + self.ttl_seconds, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("SPLITTER_TREE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SPLITTER_TREE_PORT", "8788")))
    parser.add_argument("--project-id", default=os.environ.get("BIGQUERY_PROJECT_ID", DEFAULT_PROJECT))
    parser.add_argument("--table", default=os.environ.get("SPLITTER_METRICS_TABLE", DEFAULT_TABLE))
    parser.add_argument("--backend", choices=("bigquery", "metabase"), default=os.environ.get("SPLITTER_TREE_BACKEND", "bigquery"))
    parser.add_argument("--metabase-url", default=os.environ.get("METABASE_URL", ""))
    parser.add_argument("--metabase-api-key", default=os.environ.get("METABASE_API_KEY", ""))
    parser.add_argument("--metabase-database-id", type=int, default=int(os.environ.get("METABASE_BIGQUERY_DATABASE_ID", str(DEFAULT_METABASE_DATABASE_ID))))
    parser.add_argument("--bigquery-location", default=os.environ.get("SPLITTER_BIGQUERY_LOCATION", DEFAULT_BIGQUERY_LOCATION))
    parser.add_argument("--static-path", type=Path, default=DEFAULT_STATIC_PATH)
    parser.add_argument("--rules-static-path", type=Path, default=DEFAULT_RULES_STATIC_PATH)
    parser.add_argument("--steps-static-path", type=Path, default=DEFAULT_STEPS_STATIC_PATH)
    parser.add_argument("--rules-d3-poc-static-path", type=Path, default=DEFAULT_RULES_D3_POC_STATIC_PATH)
    parser.add_argument("--cost-report-path", type=Path, default=DEFAULT_COST_REPORT_PATH)
    parser.add_argument("--cost-dashboard-path", type=Path, default=DEFAULT_COST_DASHBOARD_PATH)
    parser.add_argument("--cost-fact-path", type=Path, default=DEFAULT_COST_FACT_PATH)
    parser.add_argument("--chart-asset-path", type=Path, default=DEFAULT_CHART_ASSET_PATH)
    parser.add_argument("--workflow-analysis-root", type=Path, default=DEFAULT_WORKFLOW_ANALYSIS_ROOT)
    parser.add_argument("--cache-ttl-seconds", type=int, default=int(os.environ.get("SPLITTER_TREE_CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_SECONDS))))
    return parser.parse_args()


def sanitize_metric_path(value: str | None) -> str:
    metric_path = (value or DEFAULT_METRIC_PATH).strip()
    if not metric_path:
        return DEFAULT_METRIC_PATH
    if len(metric_path) > MAX_METRIC_PATH_LENGTH:
        raise ValueError("metric_path is too long")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if any(char not in allowed for char in metric_path):
        raise ValueError("metric_path contains unsupported characters")
    return metric_path


def sanitize_variant(value: str | None) -> str:
    variant = (value or DEFAULT_VARIANT).strip()
    if variant not in {"baseline", "hinted"}:
        raise ValueError("variant must be baseline or hinted")
    return variant


def clamp_history_runs(value: str | None) -> int:
    try:
        runs = int(value or DEFAULT_HISTORY_RUNS)
    except Exception:
        runs = DEFAULT_HISTORY_RUNS
    return max(1, min(MAX_HISTORY_RUNS, runs))


def query_from_params(params: dict[str, list[str]]) -> MetricTreeQuery:
    return MetricTreeQuery(
        metric_path=sanitize_metric_path(first(params, "metric_path")),
        variant=sanitize_variant(first(params, "variant")),
        history_runs=clamp_history_runs(first(params, "history_runs")),
    )


def first(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key) or []
    return values[0] if values else None


def latest_tree_sql(table: str) -> str:
    return f"""
WITH latest AS (
  SELECT MAX(collected_at) AS collected_at
  FROM `{table}`
  WHERE dirty = FALSE
    AND variant = {{variant_param}}
), nodes AS (
  SELECT *
  FROM `{table}`
  WHERE dirty = FALSE
    AND collected_at = (SELECT collected_at FROM latest)
    AND variant = {{variant_param}}
), selected AS (
  SELECT metric_path AS selected_path, depth AS selected_depth
  FROM nodes
  WHERE metric_path = {{metric_path_param}}
  LIMIT 1
)
SELECT
  nodes.collected_at,
  nodes.run_id,
  nodes.branch,
  nodes.head_sha,
  nodes.variant,
  nodes.root_metric_id,
  nodes.metric_path,
  IF(STRPOS(nodes.metric_path, '.') > 0, REGEXP_REPLACE(nodes.metric_path, r'\\.[^\\.]+$', ''), '') AS parent_metric_path,
  nodes.parent_metric_id,
  REGEXP_EXTRACT(nodes.metric_path, r'([^\\.]+)$') AS metric_id,
  nodes.kind,
  nodes.depth,
  nodes.depth - selected.selected_depth AS relative_depth,
  COALESCE(nodes.is_score, nodes.kind != 'diagnostic') AS is_score,
  ROUND(IF(COALESCE(nodes.is_score, nodes.kind != 'diagnostic'), nodes.value, NULL), 4) AS score,
  ROUND(
    IF(
      COALESCE(nodes.is_score, nodes.kind != 'diagnostic'),
      COALESCE(nodes.display_value, nodes.value),
      COALESCE(nodes.display_value, nodes.diagnostic_value, nodes.value)
    ),
    4
  ) AS display_value,
  ROUND(
    IF(
      COALESCE(nodes.is_score, nodes.kind != 'diagnostic'),
      NULL,
      COALESCE(nodes.diagnostic_value, nodes.display_value, nodes.value)
    ),
    4
  ) AS diagnostic_value,
  COALESCE(
    nodes.display_unit,
    IF(COALESCE(nodes.is_score, nodes.kind != 'diagnostic'), 'score', 'avg_count')
  ) AS display_unit,
  ROUND(nodes.local_weight * 100, 2) AS local_weight_pct,
  ROUND(nodes.effective_weight * 100, 2) AS effective_weight_pct,
  nodes.description,
  nodes.why
FROM nodes, selected
WHERE nodes.metric_path = selected.selected_path
   OR STARTS_WITH(nodes.metric_path, CONCAT(selected.selected_path, '.'))
ORDER BY nodes.metric_path
"""


def history_sql(table: str) -> str:
    return f"""
WITH run_times AS (
  SELECT DISTINCT collected_at
  FROM `{table}`
  WHERE dirty = FALSE
    AND variant = {{variant_param}}
  ORDER BY collected_at DESC
  LIMIT {{history_runs_param}}
), selected AS (
  SELECT {{metric_path_param}} AS selected_path
)
SELECT
  collected_at,
  run_id,
  branch,
  head_sha,
  variant,
  metric_path,
  COALESCE(is_score, kind != 'diagnostic') AS is_score,
  ROUND(IF(COALESCE(is_score, kind != 'diagnostic'), value, NULL), 4) AS score,
  ROUND(
    IF(
      COALESCE(is_score, kind != 'diagnostic'),
      COALESCE(display_value, value),
      COALESCE(display_value, diagnostic_value, value)
    ),
    4
  ) AS display_value,
  COALESCE(display_unit, IF(COALESCE(is_score, kind != 'diagnostic'), 'score', 'avg_count')) AS display_unit,
  ROUND(effective_weight * 100, 2) AS effective_weight_pct
FROM `{table}`, selected
WHERE dirty = FALSE
  AND variant = {{variant_param}}
  AND collected_at IN (SELECT collected_at FROM run_times)
  AND (
    metric_path = selected_path
    OR STARTS_WITH(metric_path, CONCAT(selected_path, '.'))
  )
ORDER BY metric_path, collected_at
"""


def parameterized_sql(sql: str) -> str:
    return (
        sql.replace("{metric_path_param}", "@metric_path")
        .replace("{variant_param}", "@variant")
        .replace("{history_runs_param}", "@history_runs")
    )


def literal_sql(sql: str, query: MetricTreeQuery) -> str:
    return (
        sql.replace("{metric_path_param}", sql_string(query.metric_path))
        .replace("{variant_param}", sql_string(query.variant))
        .replace("{history_runs_param}", str(query.history_runs))
    )


def sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def assert_no_weighted_fields(sql: str) -> None:
    lowered = sql.lower()
    if "weighted_value" in lowered or "weighted_contribution" in lowered:
        raise AssertionError("weighted contribution fields must not be selected")


def bigquery_parameters(query: MetricTreeQuery) -> list[Any]:
    from google.cloud import bigquery  # type: ignore

    return [
        bigquery.ScalarQueryParameter("metric_path", "STRING", query.metric_path),
        bigquery.ScalarQueryParameter("variant", "STRING", query.variant),
        bigquery.ScalarQueryParameter("history_runs", "INT64", query.history_runs),
    ]


def run_bigquery(client: Any, sql: str, query: MetricTreeQuery) -> list[dict[str, Any]]:
    from google.cloud import bigquery  # type: ignore

    assert_no_weighted_fields(sql)
    job_config = bigquery.QueryJobConfig(query_parameters=bigquery_parameters(query))
    return [dict(row.items()) for row in client.query(sql, job_config=job_config).result()]


def tree_response(client: Any, table: str, query: MetricTreeQuery) -> dict[str, Any]:
    latest_rows = run_bigquery(client, parameterized_sql(latest_tree_sql(table)), query)
    history_rows = run_bigquery(client, parameterized_sql(history_sql(table)), query)
    return {
        "metric_path": query.metric_path,
        "variant": query.variant,
        "history_runs": query.history_runs,
        "rows": normalize_rows(latest_rows),
        "history": normalize_history(history_rows),
    }


def metabase_request(metabase_url: str, api_key: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not metabase_url or not api_key:
        raise RuntimeError("Missing METABASE_URL or METABASE_API_KEY")
    request = urllib.request.Request(
        metabase_url.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    request.add_header("X-API-Key", api_key)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Metabase API failed: HTTP {exc.code} {body}") from exc


def run_metabase_dataset(
    metabase_url: str,
    api_key: str,
    database_id: int,
    sql: str,
) -> list[dict[str, Any]]:
    assert_no_weighted_fields(sql)
    result = metabase_request(
        metabase_url,
        api_key,
        "/api/dataset",
        {"database": database_id, "type": "native", "native": {"query": sql}},
    )
    data = result.get("data") or {}
    columns = [column.get("name") for column in data.get("cols") or []]
    return [dict(zip(columns, row)) for row in data.get("rows") or []]


def metabase_tree_response(
    metabase_url: str,
    api_key: str,
    database_id: int,
    table: str,
    query: MetricTreeQuery,
) -> dict[str, Any]:
    latest_rows = run_metabase_dataset(
        metabase_url,
        api_key,
        database_id,
        literal_sql(latest_tree_sql(table), query),
    )
    history_rows = run_metabase_dataset(
        metabase_url,
        api_key,
        database_id,
        literal_sql(history_sql(table), query),
    )
    return {
        "metric_path": query.metric_path,
        "variant": query.variant,
        "history_runs": query.history_runs,
        "rows": normalize_rows(latest_rows),
        "history": normalize_history(history_rows),
    }


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [
        {
            "collected_at": serialize_value(row.get("collected_at")),
            "run_id": row.get("run_id"),
            "branch": row.get("branch"),
            "head_sha": row.get("head_sha"),
            "short_sha": short_sha(row.get("head_sha")),
            "variant": row.get("variant"),
            "root_metric_id": row.get("root_metric_id"),
            "metric_path": row.get("metric_path"),
            "parent_metric_path": row.get("parent_metric_path") or "",
            "parent_metric_id": row.get("parent_metric_id"),
            "metric_id": row.get("metric_id"),
            "kind": row.get("kind"),
            "depth": to_int(row.get("depth")),
            "relative_depth": to_int(row.get("relative_depth")),
            "is_score": to_bool(row.get("is_score"), default=row.get("kind") != "diagnostic"),
            "score": to_float(row.get("score")),
            "diagnostic_value": to_float(row.get("diagnostic_value")),
            "display_value": to_float(row.get("display_value")),
            "display_unit": row.get("display_unit") or "score",
            "local_weight_pct": to_float(row.get("local_weight_pct")),
            "effective_weight_pct": to_float(row.get("effective_weight_pct")),
            "description": row.get("description") or "",
            "why": row.get("why") or "",
            "formula": FORMULAS.get(str(row.get("metric_id") or ""), ""),
        }
        for row in rows
    ]
    apply_explanation_tree(normalized)
    attach_explanations(normalized)
    return sorted(normalized, key=lambda row: row.get("tree_path") or row.get("metric_path") or "")


def apply_explanation_tree(rows: list[dict[str, Any]]) -> None:
    paths = {str(row.get("metric_path") or "") for row in rows}
    for row in rows:
        metric_path = str(row.get("metric_path") or "")
        parent_path = str(row.get("parent_metric_path") or "")
        metric_id = str(row.get("metric_id") or "")
        score_metric_id = DIAGNOSTIC_SCORE_PARENTS.get(metric_id)
        row["tree_path"] = metric_path
        row["tree_parent_path"] = parent_path
        row["tree_relative_depth"] = row.get("relative_depth")
        if not score_metric_id or not parent_path:
            continue
        score_path = f"{parent_path}.{score_metric_id}"
        if score_path not in paths:
            continue
        row["tree_parent_path"] = score_path
        row["tree_path"] = f"{score_path}.{metric_id}"
        if isinstance(row.get("relative_depth"), int):
            row["tree_relative_depth"] = int(row["relative_depth"]) + 1


def attach_explanations(rows: list[dict[str, Any]]) -> None:
    by_metric_id = {str(row.get("metric_id") or ""): row for row in rows}
    for row in rows:
        row["explanation"] = explanation_for_row(row, by_metric_id)


def explanation_for_row(row: dict[str, Any], by_metric_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    metric_id = str(row.get("metric_id") or "")
    if metric_id in STACK_LINK_SPECS:
        correct_id, extra_id, missing_id = STACK_LINK_SPECS[metric_id]
        correct = display_value(by_metric_id.get(correct_id))
        extra = display_value(by_metric_id.get(extra_id))
        missing = display_value(by_metric_id.get(missing_id))
        expected = sum_values(correct, missing)
        actual = sum_values(correct, extra)
        return {
            "kind": "fraction",
            "title": "Correct stack links score",
            "formula": {
                "numerator": "correct links",
                "denominator": "max(expected links, actual links, 1)",
            },
            "inputs": [
                input_item("correct links", correct, correct_id),
                input_item("expected links", expected, "correct + missing"),
                input_item("actual links", actual, "correct + extra"),
            ],
            "result": input_item("score", row.get("score"), metric_id),
            "note": "Calculated per replay case, then averaged. Average inputs explain the score but may not recompute it exactly.",
        }
    if metric_id in NO_EXTRA_SPECS:
        extra_id, correct_id = NO_EXTRA_SPECS[metric_id]
        extra = display_value(by_metric_id.get(extra_id))
        correct = display_value(by_metric_id.get(correct_id))
        actual = sum_values(correct, extra)
        return {
            "kind": "expression",
            "title": "No extra stack links score",
            "formula": {"expression": "1 - extra links / max(actual links, 1)"},
            "inputs": [
                input_item("extra links", extra, extra_id),
                input_item("actual links", actual, "correct + extra"),
            ],
            "result": input_item("score", row.get("score"), metric_id),
            "note": "Calculated per replay case, then averaged. Average inputs explain the score but may not recompute it exactly.",
        }
    if metric_id in NO_MISSING_SPECS:
        missing_id, correct_id = NO_MISSING_SPECS[metric_id]
        missing = display_value(by_metric_id.get(missing_id))
        correct = display_value(by_metric_id.get(correct_id))
        expected = sum_values(correct, missing)
        return {
            "kind": "expression",
            "title": "No missing stack links score",
            "formula": {"expression": "1 - missing links / max(expected links, 1)"},
            "inputs": [
                input_item("missing links", missing, missing_id),
                input_item("expected links", expected, "correct + missing"),
            ],
            "result": input_item("score", row.get("score"), metric_id),
            "note": "Calculated per replay case, then averaged. Average inputs explain the score but may not recompute it exactly.",
        }
    if metric_id in STACK_LINK_AGGREGATES:
        inputs = [
            input_item(label, display_value(by_metric_id.get(child_id)), f"{weight:g} * {child_id}")
            for label, child_id, weight in STACK_LINK_AGGREGATES[metric_id]
        ]
        return {
            "kind": "weighted_sum",
            "title": "Stack link correctness score",
            "formula": {"expression": FORMULAS[metric_id]},
            "inputs": inputs,
            "result": input_item("score", row.get("score"), metric_id),
            "note": "Diagnostic count rows explain the child scores; only normalized score rows contribute to this aggregate.",
        }
    if row.get("kind") == "diagnostic":
        return {
            "kind": "diagnostic",
            "title": "Diagnostic count",
            "formula": {"expression": "average raw count per replay case"},
            "inputs": [input_item("avg count", display_value(row), metric_id)],
            "result": input_item("avg count", display_value(row), metric_id),
            "note": "This value is not normalized and does not contribute directly to aggregate scoring.",
        }
    if metric_id in FORMULAS:
        return {
            "kind": "expression",
            "title": "Score formula",
            "formula": {"expression": FORMULAS[metric_id]},
            "inputs": [],
            "result": input_item("score", row.get("score"), metric_id),
            "note": "Score values are normalized from 0 to 1.",
        }
    return {
        "kind": "score",
        "title": "Metric value",
        "formula": {"expression": str(row.get("description") or "Catalog-defined score")},
        "inputs": [],
        "result": input_item("value", display_value(row), metric_id),
        "note": "",
    }


def display_value(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    value = row.get("display_value")
    if isinstance(value, (int, float)):
        return float(value)
    value = row.get("score")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def sum_values(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return round(left + right, 4)


def input_item(label: str, value: Any, source: str) -> dict[str, Any]:
    return {"label": label, "value": value, "source": source}


def normalize_history(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    history: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        metric_path = str(row.get("metric_path") or "")
        history.setdefault(metric_path, []).append(
            {
                "collected_at": serialize_value(row.get("collected_at")),
                "run_id": row.get("run_id"),
                "branch": row.get("branch"),
                "head_sha": row.get("head_sha"),
                "short_sha": short_sha(row.get("head_sha")),
                "variant": row.get("variant"),
                "score": to_float(row.get("score")),
                "display_value": to_float(row.get("display_value")),
                "display_unit": row.get("display_unit") or "score",
                "is_score": to_bool(row.get("is_score")),
                "effective_weight_pct": to_float(row.get("effective_weight_pct")),
            }
        )
    return history


def serialize_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def short_sha(value: Any) -> str:
    return str(value or "")[:12]


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def to_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "t", "yes"}


def to_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def read_json_file(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {}


def read_jsonl_file(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
            if limit is not None and len(rows) >= limit:
                break
    return rows


def latest_learning_run_path(workflow_analysis_root: Path) -> Path:
    runs_dir = workflow_analysis_root / "target" / "stack-learning" / "runs"
    paths = sorted(runs_dir.glob("*/pipeline-run.json"))
    if not paths:
        raise FileNotFoundError(f"no learning runs found under {runs_dir}")
    return paths[-1]


def artifact_path(workflow_analysis_root: Path, pipeline_run: dict[str, Any], key: str) -> Path | None:
    path_value = str(((pipeline_run.get("artifacts") or {}).get(key) or "")).strip()
    if not path_value:
        return None
    path = Path(path_value)
    return path if path.is_absolute() else workflow_analysis_root / path


def rule_item_label(item: dict[str, Any]) -> str:
    item_type = item.get("type") or "item"
    item_id = item.get("id") or ""
    return f"{item_type}:{item_id}" if item_id else str(item_type)


LEGACY_PHASE_TO_CHANGE_TYPE = {
    "foundation": "foundation",
    "change": "behavior",
    "surface": "surface",
    "verification": "verification",
    "docs": "docs",
    "cleanup": "cleanup",
}
CHANGE_TYPES = {
    "foundation",
    "behavior",
    "surface",
    "dependency",
    "refactor",
    "compatibility",
    "docs",
    "cleanup",
    "verification",
}


def derive_change_type(parsed: dict[str, Any]) -> str:
    """Single changeType from parsed tags: explicit change-type wins over legacy fields."""
    explicit = str(parsed.get("changeType") or "")
    if explicit in CHANGE_TYPES:
        return explicit
    task_kind = str(parsed.get("taskKind") or "")
    if task_kind in CHANGE_TYPES:
        return task_kind
    return LEGACY_PHASE_TO_CHANGE_TYPE.get(str(parsed.get("phase") or ""), "")


def canonical_tags(tag_list: list[Any] | None) -> dict[str, Any]:
    """Parse action tags (change-type:behavior, plus legacy task-kind:/phase:) into a label dict."""
    parsed: dict[str, Any] = {"qualifiers": []}
    for tag in tag_list or []:
        text = str(tag)
        if ":" not in text:
            continue
        prefix, value = text.split(":", 1)
        if prefix == "change-type":
            parsed["changeType"] = value
        elif prefix == "task-kind":
            parsed["taskKind"] = value
        elif prefix == "behavior-type":
            parsed["behaviorType"] = value
        elif prefix == "layer":
            parsed["architectureLayer"] = value
        elif prefix == "phase":
            parsed["phase"] = value
        elif prefix == "qualifier":
            parsed["qualifiers"].append(value)
    parsed["changeType"] = derive_change_type(parsed)
    return parsed


def dominant_task_key(task_keys: dict[str, Any] | None) -> str:
    if not task_keys:
        return ""
    return sorted(task_keys.items(), key=lambda item: (-int(item[1] or 0), item[0]))[0][0]


def normalize_gate(action: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a step's verification gate to {state, raw}; None means no gate recorded."""
    gate = action.get("gate") or action.get("verification")
    if not gate:
        return None
    if not isinstance(gate, dict):
        return {"state": "passed", "raw": gate}
    state = str(gate.get("state") or gate.get("status") or "")
    if not state:
        if gate.get("hasTests") or gate.get("verifiedBy"):
            state = "passed"
        elif gate.get("passed") is True:
            state = "passed"
        elif gate.get("passed") is False:
            state = "failed"
        else:
            state = "open"
    return {"state": state, "raw": gate}


def normalize_task_action(action: dict[str, Any]) -> dict[str, Any]:
    return {
        **action,
        "changeType": str(action.get("changeType") or "")
        or LEGACY_PHASE_TO_CHANGE_TYPE.get(str(action.get("phase") or ""), ""),
        "gate": normalize_gate(action),
    }


def task_change_type_mix(task: dict[str, Any]) -> dict[str, Any]:
    mix = task.get("changeTypeMix")
    if isinstance(mix, dict) and mix:
        return mix
    remapped: dict[str, Any] = {}
    for phase, count in (task.get("phaseMix") or {}).items():
        change_type = LEGACY_PHASE_TO_CHANGE_TYPE.get(str(phase), str(phase))
        remapped[change_type] = remapped.get(change_type, 0) + count
    return remapped


def normalize_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        **task,
        "changeTypeMix": task_change_type_mix(task),
        "actions": [
            normalize_task_action(action)
            for action in task.get("actions") or []
            if isinstance(action, dict)
        ],
    }


def action_index(catalog: dict[str, Any], role_catalog: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Index resolvable actions by id from the catalog plus learned role-catalog nodes."""
    index: dict[str, dict[str, Any]] = {}
    for action in catalog.get("actions") or []:
        if isinstance(action, dict) and action.get("id"):
            index[str(action["id"])] = action
    for node in (role_catalog or {}).get("nodes") or []:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        node_action_id = f"learned-node-{node['id']}"
        if node_action_id not in index:
            index[node_action_id] = {
                "id": node_action_id,
                "title": node.get("title"),
                "tags": node.get("tags") or [],
                "phase": node.get("phase"),
                "metadata": {"nodeId": node["id"], "taskKeys": node.get("taskKeys") or {}},
            }
    return index


def resolve_rule_item(item: dict[str, Any], actions_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Resolve a rule selector to a display object; label is kept for older renderers."""
    resolved: dict[str, Any] = {
        "label": rule_item_label(item),
        "type": item.get("type"),
        "id": item.get("id"),
    }
    if item.get("type") == "action":
        action = actions_by_id.get(str(item.get("id") or ""))
        if action:
            metadata = action.get("metadata") or {}
            tags = canonical_tags(action.get("tags"))
            resolved.update(
                {
                    "title": action.get("title"),
                    "tags": tags,
                    "changeType": tags.get("changeType")
                    or str(metadata.get("changeType") or ""),
                    "taskKey": dominant_task_key(metadata.get("taskKeys")),
                    "phase": action.get("phase"),
                    "nodeId": metadata.get("nodeId"),
                }
            )
    return resolved


def subrule_identity(item: dict[str, Any]) -> str:
    explicit = str(item.get("candidateId") or item.get("ruleId") or "").strip()
    if explicit:
        return explicit
    return "|".join(
        [
            str(item.get("parentRuleId") or ""),
            str(item.get("before") or ""),
            str(item.get("after") or ""),
        ]
    )


def subrule_delta(summary: dict[str, Any]) -> dict[str, Any]:
    return ((summary.get("subruleProof") or {}).get("hintsRulesVsHints") or {})


def normalize_subrule(item: dict[str, Any]) -> dict[str, Any]:
    validation_delta = subrule_delta(item.get("validationSummary") or {})
    test_delta = subrule_delta(item.get("testSummary") or {})
    return {
        "id": subrule_identity(item),
        "candidateId": item.get("candidateId"),
        "ruleId": item.get("ruleId"),
        "parentRuleId": item.get("parentRuleId"),
        "before": item.get("before"),
        "after": item.get("after"),
        "support": item.get("support"),
        "repoSupport": item.get("repoSupport"),
        "repos": item.get("repos") or [],
        "confidence": item.get("confidence"),
        "promoted": item.get("promoted") or item.get("decision") == "promoted",
        "decision": item.get("decision") or ("promoted" if item.get("promoted") else "candidate"),
        "rejectedReason": item.get("rejectedReason"),
        "failureBucket": item.get("failureBucket"),
        "beforeTitle": item.get("beforeTitle"),
        "afterTitle": item.get("afterTitle"),
        "backoffLevel": (item.get("validationSupport") or {}).get("backoffLevel"),
        "metadata": item.get("metadata") or {},
        "validation": {
            "support": (item.get("validationSupport") or {}).get("support"),
            "repoSupport": (item.get("validationSupport") or {}).get("repoSupport"),
            "weightedSupport": (item.get("validationSupport") or {}).get("weightedSupport"),
            "backoffLevel": (item.get("validationSupport") or {}).get("backoffLevel"),
            "pairedCount": validation_delta.get("pairedCount"),
            "stackLinkDelta": validation_delta.get("averagePlannedStackLinkCorrectnessDelta"),
            "collapseDelta": validation_delta.get("averageWorkItemCollapseRateDelta"),
            "noExtraDelta": validation_delta.get("averageNoExtraStackLinksDelta"),
            "noMissingDelta": validation_delta.get("averageNoMissingStackLinksDelta"),
            "regressionCount": validation_delta.get("regressionCount"),
        },
        "test": {
            "support": (item.get("testSupport") or {}).get("support"),
            "repoSupport": (item.get("testSupport") or {}).get("repoSupport"),
            "pairedCount": test_delta.get("pairedCount"),
            "stackLinkDelta": test_delta.get("averagePlannedStackLinkCorrectnessDelta"),
            "collapseDelta": test_delta.get("averageWorkItemCollapseRateDelta"),
            "noExtraDelta": test_delta.get("averageNoExtraStackLinksDelta"),
            "noMissingDelta": test_delta.get("averageNoMissingStackLinksDelta"),
            "regressionCount": test_delta.get("regressionCount"),
        },
        "examples": item.get("examples") or [],
        "raw": item,
    }


def merge_subrules(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_rule_id: dict[str, str] = {}
    for item in primary:
        if isinstance(item, dict):
            key = subrule_identity(item)
            by_id[key] = dict(item)
            rule_id = str(item.get("ruleId") or "").strip()
            if rule_id:
                by_rule_id[rule_id] = key
    for item in secondary:
        if not isinstance(item, dict):
            continue
        key = subrule_identity(item)
        rule_id = str(item.get("ruleId") or "").strip()
        if rule_id and rule_id in by_rule_id:
            key = by_rule_id[rule_id]
        by_id[key] = {**by_id.get(key, {}), **item}
    return list(by_id.values())


def subrules_by_parent(subrules: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in subrules:
        normalized = normalize_subrule(item)
        parent = str(normalized.get("parentRuleId") or "unmapped")
        grouped.setdefault(parent, []).append(normalized)
    for items in grouped.values():
        items.sort(
            key=lambda item: (
                0 if item.get("promoted") else 1,
                str(item.get("decision") or ""),
                -float(item.get("confidence") or 0),
                -int(item.get("support") or 0),
                str(item.get("id") or ""),
            )
        )
    return grouped


def load_subrule_artifacts(workflow_analysis_root: Path, pipeline_run: dict[str, Any], pipeline_path: Path) -> dict[str, Any]:
    generated_path = artifact_path(workflow_analysis_root, pipeline_run, "subrulesModel")
    candidate_path = artifact_path(workflow_analysis_root, pipeline_run, "subruleCandidates")
    report_md_path = artifact_path(workflow_analysis_root, pipeline_run, "subruleInvestigationReport")
    report_json_path = report_md_path.with_suffix(".json") if report_md_path else pipeline_path.parent / "reports" / "subrule-investigation.json"

    generated = read_json_file(generated_path) if generated_path and generated_path.exists() else {}
    candidates = read_json_file(candidate_path) if candidate_path and candidate_path.exists() else {}
    report = read_json_file(report_json_path) if report_json_path and report_json_path.exists() else {}
    if isinstance(candidates.get("subrules"), list):
        merged = candidates["subrules"]
    elif isinstance(report.get("candidates"), list):
        merged = report["candidates"]
    else:
        merged = generated.get("subrules") if isinstance(generated.get("subrules"), list) else []
    grouped = subrules_by_parent(merged)
    split_summary = next(
        (
            payload.get("splitSummary")
            for payload in (candidates, generated, report)
            if isinstance(payload.get("splitSummary"), dict) and payload.get("splitSummary")
        ),
        {},
    )
    by_backoff_level: dict[str, int] = {}
    for items in grouped.values():
        for subrule in items:
            level = subrule.get("backoffLevel")
            key = str(level) if level is not None else "none"
            by_backoff_level[key] = by_backoff_level.get(key, 0) + 1
    return {
        "path": str(generated_path) if generated_path else "",
        "candidatePath": str(candidate_path) if candidate_path else "",
        "reportPath": str(report_json_path) if report_json_path else "",
        "status": report.get("status") or "",
        "splitSummary": split_summary,
        "candidatesByBackoffLevel": by_backoff_level,
        "count": len(merged),
        "promotedCount": sum(1 for item in grouped.values() for subrule in item if subrule.get("promoted")),
        "parents": [
            {
                "parentRuleId": parent,
                "candidateCount": len(items),
                "promotedCount": sum(1 for item in items if item.get("promoted")),
                "subrules": items,
            }
            for parent, items in sorted(grouped.items())
        ],
        "byParent": grouped,
    }


def load_learning_artifact(workflow_analysis_root: Path, pipeline_run: dict[str, Any], key: str) -> dict[str, Any]:
    path = artifact_path(workflow_analysis_root, pipeline_run, key)
    payload = read_json_file(path) if path and path.exists() else {}
    if payload:
        payload = {**payload, "path": str(path)}
    return payload


def normalize_rule_catalog(
    catalog: dict[str, Any],
    *,
    kind: str,
    title: str,
    path: Path | None,
    subrules: dict[str, list[dict[str, Any]]] | None = None,
    role_catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rules = catalog.get("rules") if isinstance(catalog.get("rules"), list) else []
    actions = catalog.get("actions") if isinstance(catalog.get("actions"), list) else []
    reasons = catalog.get("reasons") if isinstance(catalog.get("reasons"), list) else []
    reason_messages = {
        str(reason.get("id")): reason.get("message") or ""
        for reason in reasons
        if isinstance(reason, dict)
    }
    actions_by_id = action_index(catalog, role_catalog)
    normalized_rules: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        reason_id = str(rule.get("reasonId") or "")
        metadata = rule.get("metadata") if isinstance(rule.get("metadata"), dict) else {}
        backoff_level = metadata.get("backoffLevel")
        normalized_rules.append(
            {
                "id": rule.get("id"),
                "priority": rule.get("priority"),
                "relation": rule.get("relation"),
                "reasonId": reason_id,
                "reason": reason_messages.get(reason_id, ""),
                "triggerThreshold": rule.get("triggerThreshold"),
                "backoffLevel": backoff_level,
                "isBackoffPrior": bool(backoff_level is not None and int(backoff_level) >= 1),
                "source": metadata.get("source") or "",
                "before": [
                    resolve_rule_item(item, actions_by_id)
                    for item in rule.get("before") or []
                    if isinstance(item, dict)
                ],
                "after": [
                    resolve_rule_item(item, actions_by_id)
                    for item in rule.get("after") or []
                    if isinstance(item, dict)
                ],
                "triggerTerms": [
                    {
                        "text": term.get("text"),
                        "category": term.get("category"),
                        "weight": term.get("weight"),
                    }
                    for term in rule.get("triggerTerms") or []
                    if isinstance(term, dict)
                ],
                "subrules": (subrules or {}).get(str(rule.get("id") or ""), []),
                "raw": rule,
            }
        )
    normalized_rules.sort(
        key=lambda rule: (
            -(rule.get("priority") or 0),
            str(rule.get("id") or ""),
        )
    )
    return {
        "kind": kind,
        "title": title,
        "path": str(path) if path else "",
        "version": catalog.get("version"),
        "counts": {
            "rules": len(normalized_rules),
            "actions": len(actions),
            "reasons": len(reasons),
        },
        "rules": normalized_rules,
        "actions": actions,
        "reasons": reasons,
    }


def uncategorized_subrule_parents(catalogs: list[dict[str, Any]], grouped_subrules: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    visible_rule_ids = {
        str(rule.get("id") or "")
        for catalog in catalogs
        for rule in catalog.get("rules") or []
    }
    parents = []
    for parent_id, items in sorted(grouped_subrules.items()):
        if parent_id in visible_rule_ids:
            continue
        subrules = [
            {
                **item,
                "displayParentRuleId": parent_id,
                "sourceParentRuleId": parent_id,
            }
            for item in items
        ]
        parents.append(
            {
                "sourceParentRuleId": parent_id,
                "candidateCount": len(subrules),
                "promotedCount": sum(1 for item in subrules if item.get("promoted")),
                "subrules": subrules,
            }
        )
    return parents


def rules_response(workflow_analysis_root: Path) -> dict[str, Any]:
    pipeline_path = latest_learning_run_path(workflow_analysis_root)
    pipeline_run = read_json_file(pipeline_path)
    catalogs = []
    subrule_artifacts = load_subrule_artifacts(workflow_analysis_root, pipeline_run, pipeline_path)
    role_catalog = load_learning_artifact(workflow_analysis_root, pipeline_run, "roleCatalog")
    planning_dag = load_learning_artifact(workflow_analysis_root, pipeline_run, "planningDag")
    grouped_subrules = subrule_artifacts.get("byParent") or {}
    generated_path = artifact_path(workflow_analysis_root, pipeline_run, "generatedRuleCatalog")
    if generated_path and generated_path.exists():
        catalogs.append(
            normalize_rule_catalog(
                read_json_file(generated_path),
                kind="generated",
                title="Generated candidate rules",
                path=generated_path,
                subrules=grouped_subrules,
                role_catalog=role_catalog,
            )
        )
    effective_path = artifact_path(workflow_analysis_root, pipeline_run, "effectiveRuleCatalog")
    if effective_path and effective_path.exists():
        catalogs.append(
            normalize_rule_catalog(
                read_json_file(effective_path),
                kind="effective",
                title="Effective rules after promotion gate",
                path=effective_path,
                subrules=grouped_subrules,
                role_catalog=role_catalog,
            )
        )
    uncategorized_parents = uncategorized_subrule_parents(catalogs, grouped_subrules)
    visible_subrule_artifacts = {key: value for key, value in subrule_artifacts.items() if key != "byParent"}
    visible_subrule_artifacts["uncategorizedParents"] = uncategorized_parents
    leaderboard_path = artifact_path(workflow_analysis_root, pipeline_run, "experimentLeaderboard")
    if not leaderboard_path or not leaderboard_path.exists():
        fallback = pipeline_path.parent / "experiments" / "leaderboard.json"
        leaderboard_path = fallback if fallback.exists() else None
    leaderboard = read_json_file(leaderboard_path) if leaderboard_path else {}
    tasks_path = artifact_path(workflow_analysis_root, pipeline_run, "tasksCorpus")
    if not tasks_path or not tasks_path.exists():
        fallback = pipeline_path.parent / "corpus" / "tasks.jsonl"
        tasks_path = fallback if fallback.exists() else None
    tasks = (
        [normalize_task(row) for row in read_jsonl_file(tasks_path, limit=500)]
        if tasks_path
        else []
    )
    return {
        "workflowAnalysisRoot": str(workflow_analysis_root),
        "pipelineRunPath": str(pipeline_path),
        "splitSummary": subrule_artifacts.get("splitSummary") or {},
        "leaderboard": leaderboard,
        "tasks": tasks,
        "run": {
            "runId": pipeline_run.get("runId"),
            "collectedAt": pipeline_run.get("collectedAt"),
            "branch": pipeline_run.get("branch"),
            "headSha": pipeline_run.get("headSha"),
            "shortSha": short_sha(pipeline_run.get("headSha")),
            "pipelineStatus": pipeline_run.get("pipelineStatus"),
            "prUrl": (pipeline_run.get("prResult") or {}).get("url"),
            "repos": pipeline_run.get("repos") or [],
            "rulePromotion": (pipeline_run.get("replaySummary") or {}).get("rulePromotion") or {},
        },
        "catalogs": catalogs,
        "roleCatalog": role_catalog,
        "planningDag": planning_dag,
        "subrules": visible_subrule_artifacts,
    }


def check_result(name: str, ok: bool, message: str = "", **details: Any) -> dict[str, Any]:
    result = {"name": name, "ok": ok}
    if message:
        result["message"] = message
    result.update(details)
    return result


def bigquery_credentials_check() -> dict[str, Any]:
    path_value = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not path_value:
        return check_result("bigquery_credentials", False, "GOOGLE_APPLICATION_CREDENTIALS is not set")
    path = Path(path_value)
    if not path.exists():
        return check_result(
            "bigquery_credentials",
            False,
            "GOOGLE_APPLICATION_CREDENTIALS file does not exist",
            path=str(path),
        )
    return check_result("bigquery_credentials", True, path=str(path))


def bigquery_import_check() -> dict[str, Any]:
    try:
        spec = importlib.util.find_spec("google.cloud.bigquery")
    except ModuleNotFoundError:
        spec = None
    if spec is None:
        return check_result("bigquery_import", False, "google-cloud-bigquery is not installed")
    return check_result("bigquery_import", True)


def bigquery_table_check(project_id: str, table: str, location: str) -> dict[str, Any]:
    from google.cloud import bigquery  # type: ignore

    client = bigquery.Client(project=project_id)
    sql = f"SELECT COUNT(1) AS row_count FROM `{table}` WHERE dirty = FALSE"
    rows = list(client.query(sql, location=location).result())
    row_count = int(rows[0]["row_count"]) if rows else 0
    return check_result(
        "bigquery_metrics_table",
        row_count > 0,
        "" if row_count > 0 else "metrics table returned zero clean rows",
        table=table,
        rowCount=row_count,
    )


def metabase_ready_check(metabase_url: str, metabase_api_key: str) -> dict[str, Any]:
    return check_result(
        "metabase_config",
        bool(metabase_url and metabase_api_key),
        "" if metabase_url and metabase_api_key else "METABASE_URL or METABASE_API_KEY is missing",
    )


def rules_artifacts_check(workflow_analysis_root: Path) -> dict[str, Any]:
    pipeline_path = latest_learning_run_path(workflow_analysis_root)
    pipeline_run = read_json_file(pipeline_path)
    subrule_artifacts = load_subrule_artifacts(workflow_analysis_root, pipeline_run, pipeline_path)
    leaderboard_path = artifact_path(workflow_analysis_root, pipeline_run, "experimentLeaderboard")
    if not leaderboard_path:
        leaderboard_path = pipeline_path.parent / "experiments" / "leaderboard.json"
    return check_result(
        "rules_artifacts",
        True,
        pipelineRunPath=str(pipeline_path),
        runId=pipeline_run.get("runId"),
        headSha=pipeline_run.get("headSha"),
        subruleCount=subrule_artifacts.get("count", 0),
        splitSummaryPresent=bool(subrule_artifacts.get("splitSummary")),
        leaderboardPresent=bool(leaderboard_path and leaderboard_path.exists()),
    )


def ready_response(
    *,
    static_path: Path,
    rules_static_path: Path,
    steps_static_path: Path,
    workflow_analysis_root: Path,
    table: str,
    project_id: str,
    backend: str,
    metabase_url: str,
    metabase_api_key: str,
    bigquery_location: str,
) -> tuple[dict[str, Any], HTTPStatus]:
    checks = [
        check_result("static_page", static_path.exists(), path=str(static_path)),
        check_result("rules_page", rules_static_path.exists(), path=str(rules_static_path)),
        check_result("steps_page", steps_static_path.exists(), path=str(steps_static_path)),
    ]
    try:
        checks.append(rules_artifacts_check(workflow_analysis_root))
    except Exception as exc:
        checks.append(check_result("rules_artifacts", False, str(exc)))

    if backend == "metabase":
        checks.append(metabase_ready_check(metabase_url, metabase_api_key))
    else:
        credentials = bigquery_credentials_check()
        checks.append(credentials)
        import_check = bigquery_import_check()
        checks.append(import_check)
        if credentials["ok"] and import_check["ok"]:
            try:
                checks.append(bigquery_table_check(project_id, table, bigquery_location))
            except Exception as exc:
                checks.append(check_result("bigquery_metrics_table", False, str(exc), table=table))

    ok = all(check.get("ok") for check in checks)
    return {
        "ok": ok,
        "backend": backend,
        "table": table,
        "projectId": project_id,
        "workflowAnalysisRoot": str(workflow_analysis_root),
        "checks": checks,
    }, HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE


def make_handler(
    static_path: Path,
    rules_static_path: Path,
    steps_static_path: Path,
    rules_d3_poc_static_path: Path,
    workflow_analysis_root: Path,
    cost_report_path: Path,
    cost_dashboard_path: Path,
    cost_fact_path: Path,
    chart_asset_path: Path,
    table: str,
    project_id: str,
    backend: str,
    metabase_url: str,
    metabase_api_key: str,
    metabase_database_id: int,
    bigquery_location: str,
    cache: TTLCache,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {
                "/",
                "/index.html",
                "/rules.html",
                "/steps.html",
                "/rules-d3-poc.html",
                "/action-rule-graph-poc.html",
                "/cost",
                "/healthz",
                "/readyz",
            }:
                if parsed.path in {"/rules-d3-poc.html", "/action-rule-graph-poc.html"}:
                    self.send_redirect("/rules.html")
                else:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self.send_static(static_path)
                return
            if parsed.path == "/rules.html":
                self.send_static(rules_static_path)
                return
            if parsed.path == "/steps.html":
                self.send_static(steps_static_path)
                return
            if parsed.path == "/rules-d3-poc.html":
                suffix = f"?{parsed.query}" if parsed.query else ""
                self.send_redirect(f"/rules.html{suffix}")
                return
            if parsed.path == "/action-rule-graph-poc.html":
                self.send_redirect("/rules.html?demo=nested")
                return
            if parsed.path in {"/cost", "/cost.html"}:
                if cost_dashboard_path.exists():
                    self.send_static(cost_dashboard_path)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, "cost dashboard not deployed yet")
                return
            if parsed.path == "/cost-summary":
                if cost_report_path.exists():
                    self.send_static(cost_report_path)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, "cost summary not generated yet")
                return
            if parsed.path == "/api/cost-timeseries":
                if cost_fact_path.exists():
                    self.send_bytes(cost_fact_path.read_bytes(), "application/json; charset=utf-8")
                else:
                    self.send_json({"error": "cost fact not generated yet"}, status=HTTPStatus.NOT_FOUND)
                return
            if parsed.path == "/cost-assets/chart.umd.min.js":
                if chart_asset_path.exists():
                    self.send_bytes(chart_asset_path.read_bytes(), "application/javascript; charset=utf-8")
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, "chart asset missing")
                return
            if parsed.path == "/api/splitter-metric-tree":
                self.send_metric_tree(parse_qs(parsed.query))
                return
            if parsed.path == "/api/splitter-rules":
                self.send_rules()
                return
            if parsed.path == "/healthz":
                self.send_json({"ok": True})
                return
            if parsed.path == "/readyz":
                payload, status = ready_response(
                    static_path=static_path,
                    rules_static_path=rules_static_path,
                    steps_static_path=steps_static_path,
                    workflow_analysis_root=workflow_analysis_root,
                    table=table,
                    project_id=project_id,
                    backend=backend,
                    metabase_url=metabase_url,
                    metabase_api_key=metabase_api_key,
                    bigquery_location=bigquery_location,
                )
                self.send_json(payload, status=status)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def send_static(self, path: Path) -> None:
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_bytes(self, body: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def send_metric_tree(self, params: dict[str, list[str]]) -> None:
            try:
                query = query_from_params(params)
                cache_key = json.dumps(query.__dict__, sort_keys=True)
                payload = cache.get(cache_key)
                if payload is None:
                    if backend == "metabase":
                        payload = metabase_tree_response(
                            metabase_url,
                            metabase_api_key,
                            metabase_database_id,
                            table,
                            query,
                        )
                    else:
                        from google.cloud import bigquery  # type: ignore

                        client = bigquery.Client(project=project_id)
                        payload = tree_response(client, table, query)
                    cache.set(cache_key, payload)
                self.send_json(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def send_rules(self) -> None:
            try:
                payload = cache.get("splitter-rules")
                if payload is None:
                    payload = rules_response(workflow_analysis_root)
                    cache.set("splitter-rules", payload)
                self.send_json(payload)
            except FileNotFoundError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, separators=(",", ":"), default=serialize_value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}", file=sys.stderr)

    return Handler


def main() -> int:
    args = parse_args()
    cache = TTLCache(args.cache_ttl_seconds)
    handler = make_handler(
        args.static_path,
        args.rules_static_path,
        args.steps_static_path,
        args.rules_d3_poc_static_path,
        args.workflow_analysis_root,
        args.cost_report_path,
        args.cost_dashboard_path,
        args.cost_fact_path,
        args.chart_asset_path,
        args.table,
        args.project_id,
        args.backend,
        args.metabase_url,
        args.metabase_api_key,
        args.metabase_database_id,
        args.bigquery_location,
        cache,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"serving splitter metric tree on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
