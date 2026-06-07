#!/usr/bin/env python3
"""Serve the Splitter metric tree UI backed by BigQuery."""

from __future__ import annotations

import argparse
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
DEFAULT_TABLE = "summer-nexus-137922.splitter_metrics.splitter_replay_metric_scores_over_time"
DEFAULT_PROJECT = "summer-nexus-137922"
DEFAULT_METRIC_PATH = "planToResponseGraphScore"
DEFAULT_VARIANT = "hinted"
DEFAULT_HISTORY_RUNS = 20
MAX_HISTORY_RUNS = 100
MAX_METRIC_PATH_LENGTH = 512
DEFAULT_CACHE_TTL_SECONDS = 60
DEFAULT_METABASE_DATABASE_ID = 2
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
    parser.add_argument("--static-path", type=Path, default=DEFAULT_STATIC_PATH)
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


def make_handler(
    static_path: Path,
    table: str,
    project_id: str,
    backend: str,
    metabase_url: str,
    metabase_api_key: str,
    metabase_database_id: int,
    cache: TTLCache,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html", "/healthz"}:
                self.send_response(HTTPStatus.OK)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self.send_static()
                return
            if parsed.path == "/api/splitter-metric-tree":
                self.send_metric_tree(parse_qs(parsed.query))
                return
            if parsed.path == "/healthz":
                self.send_json({"ok": True})
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def send_static(self) -> None:
            body = static_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
        args.table,
        args.project_id,
        args.backend,
        args.metabase_url,
        args.metabase_api_key,
        args.metabase_database_id,
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
