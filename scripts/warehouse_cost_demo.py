#!/usr/bin/env python3
"""Build warehouse demo tables for v4.5 command cost analytics."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from session_phase_narrative_report import CLASSIFICATION_REVISION, SCHEMA_VERSION, classify_prompt_window
from usage_costing import load_pricing_table, price_component, pricing_for_model


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO_ROOT / "reports" / "usage-command-attribution-v4_5.csv"
DEFAULT_OUTPUT = REPO_ROOT / "reports" / "warehouse-command-costs-v4_5.csv"
DEFAULT_LOCAL_SUMMARY = REPO_ROOT / "reports" / "warehouse-command-costs-v4_5-summary.json"
DEFAULT_EXPLORER_INPUT = REPO_ROOT / "reports" / "cost-explorer-v1" / "commands.csv"
DEFAULT_DATASET = "session_metrics_demo"
DEFAULT_TABLE = "command_costs"
DEFAULT_EXPLORER_TABLE = "cost_explorer_commands_v1"
EXPECTED_FULL_ROW_COUNT = 261_576

DIMENSION_COLUMNS = [
    "workflow_phase",
    "efficiency_label",
    "agent_tool_intention",
    "request_origin",
    "work_motivation",
    "function_name",
    "shell_verb",
    "tool_execution_mode",
    "model",
    "provider",
    "billable_model",
]

OUTPUT_COLUMNS = [
    "session_date",
    "session_id",
    "prompt_index",
    "command_index",
    *DIMENSION_COLUMNS,
    "allocated_input_tokens",
    "allocated_cache_read_tokens",
    "allocated_cache_creation_tokens",
    "allocated_output_tokens",
    "allocated_reasoning_tokens",
    "allocated_total_tokens",
    "allocated_fresh_input_tokens",
    "allocated_total_cost_usd",
    "allocated_fresh_input_cost_usd",
    "allocated_cache_read_cost_usd",
    "allocated_cache_creation_cost_usd",
    "allocated_output_cost_usd",
    "prompt_input_tokens",
    "prompt_cache_read_tokens",
    "prompt_cache_creation_tokens",
    "prompt_output_tokens",
    "prompt_reasoning_tokens",
    "prompt_total_tokens",
    "prompt_derived_total_cost_usd",
]

INTEGER_COLUMNS = {"prompt_index", "command_index"}
DATE_COLUMNS = {"session_date"}
NUMERIC_COLUMNS = set(OUTPUT_COLUMNS) - set(DIMENSION_COLUMNS) - {"session_date", "session_id"}
EXPLORER_INTEGER_COLUMNS = {"prompt_index", "command_index", "request_pattern_depth"}
EXPLORER_DATE_COLUMNS = {"session_date"}
EXPLORER_NUMERIC_COLUMNS = set(NUMERIC_COLUMNS) | {
    "headline_context_tokens",
    "headline_context_cost_usd",
    "headline_cache_read_tokens",
    "headline_cache_read_cost_usd",
    "headline_output_tokens",
    "headline_output_cost_usd",
}
EXPLORER_FORBIDDEN_COLUMNS = {
    "command_hash",
    "stdin_hash",
    "delegated_task_hash",
    "terminal_context_parent_command_hash",
}


def explorer_commands_columns() -> list[str]:
    from cost_explorer_report import COMMANDS_COLUMNS

    return list(COMMANDS_COLUMNS)

VIEW_QUERIES = {
    "daily_cost_by_phase": """
        SELECT session_date, workflow_phase, SUM(allocated_total_cost_usd) AS total_cost_usd, COUNT(*) AS command_count
        FROM {table}
        GROUP BY session_date, workflow_phase
    """,
    "daily_cost_by_efficiency": """
        SELECT session_date, efficiency_label, SUM(allocated_total_cost_usd) AS total_cost_usd, COUNT(*) AS command_count
        FROM {table}
        GROUP BY session_date, efficiency_label
    """,
    "daily_cost_by_agent_intention": """
        SELECT session_date, agent_tool_intention, SUM(allocated_total_cost_usd) AS total_cost_usd, COUNT(*) AS command_count
        FROM {table}
        GROUP BY session_date, agent_tool_intention
    """,
    "phase_efficiency_breakdown": """
        SELECT
          workflow_phase,
          efficiency_label,
          SUM(allocated_total_cost_usd) AS total_cost_usd,
          SUM(allocated_total_tokens) AS total_tokens,
          COUNT(*) AS command_count
        FROM {table}
        GROUP BY workflow_phase, efficiency_label
    """,
    "phase_efficiency_motivation_breakdown": """
        SELECT
          workflow_phase,
          efficiency_label,
          work_motivation,
          SUM(allocated_total_cost_usd) AS total_cost_usd,
          SUM(allocated_total_tokens) AS total_tokens,
          COUNT(*) AS command_count
        FROM {table}
        GROUP BY workflow_phase, efficiency_label, work_motivation
    """,
    "token_consumption_over_time": """
        SELECT
          session_date,
          SUM(allocated_total_tokens) AS total_tokens,
          SUM(allocated_input_tokens) AS input_tokens,
          SUM(allocated_cache_read_tokens) AS cache_read_tokens,
          SUM(allocated_cache_creation_tokens) AS cache_creation_tokens,
          SUM(allocated_output_tokens) AS output_tokens,
          SUM(allocated_reasoning_tokens) AS reasoning_tokens
        FROM {table}
        GROUP BY session_date
    """,
    "model_billable_model_cost": """
        SELECT model, billable_model, SUM(allocated_total_cost_usd) AS total_cost_usd, SUM(allocated_total_tokens) AS total_tokens, COUNT(*) AS command_count
        FROM {table}
        GROUP BY model, billable_model
    """,
    "session_drilldown": """
        SELECT
          session_date,
          session_id,
          SUM(allocated_total_cost_usd) AS total_cost_usd,
          SUM(allocated_total_tokens) AS total_tokens,
          COUNT(*) AS command_count,
          COUNT(DISTINCT prompt_index) AS prompt_count,
          {any_value}(model) AS model,
          {any_value}(billable_model) AS billable_model
        FROM {table}
        GROUP BY session_date, session_id
    """,
}

METABASE_CARDS = [
    ("Total cost over time", "line", "SELECT session_date, SUM(allocated_total_cost_usd) AS total_cost_usd FROM {table} GROUP BY session_date ORDER BY session_date"),
    ("Total tokens over time", "line", "SELECT session_date, SUM(allocated_total_tokens) AS total_tokens FROM {table} GROUP BY session_date ORDER BY session_date"),
    ("Cost by workflow_phase", "bar", "SELECT workflow_phase, SUM(allocated_total_cost_usd) AS total_cost_usd FROM {table} GROUP BY workflow_phase ORDER BY total_cost_usd DESC"),
    ("Cost by efficiency_label", "bar", "SELECT efficiency_label, SUM(allocated_total_cost_usd) AS total_cost_usd FROM {table} GROUP BY efficiency_label ORDER BY total_cost_usd DESC"),
    ("Phase x efficiency breakdown", "table", "SELECT workflow_phase, efficiency_label, SUM(allocated_total_cost_usd) AS total_cost_usd, SUM(allocated_total_tokens) AS total_tokens, COUNT(*) AS command_count FROM {table} GROUP BY workflow_phase, efficiency_label ORDER BY workflow_phase, total_cost_usd DESC"),
    ("Phase x efficiency x motivation", "table", "SELECT workflow_phase, efficiency_label, work_motivation, SUM(allocated_total_cost_usd) AS total_cost_usd, SUM(allocated_total_tokens) AS total_tokens, COUNT(*) AS command_count FROM {table} GROUP BY workflow_phase, efficiency_label, work_motivation ORDER BY workflow_phase, efficiency_label, total_cost_usd DESC"),
    ("Cost by agent_tool_intention", "bar", "SELECT agent_tool_intention, SUM(allocated_total_cost_usd) AS total_cost_usd FROM {table} GROUP BY agent_tool_intention ORDER BY total_cost_usd DESC LIMIT 25"),
    ("Cost by request_origin", "bar", "SELECT request_origin, SUM(allocated_total_cost_usd) AS total_cost_usd FROM {table} GROUP BY request_origin ORDER BY total_cost_usd DESC"),
    ("Cost by model/billable model", "bar", "SELECT model, billable_model, SUM(allocated_total_cost_usd) AS total_cost_usd FROM {table} GROUP BY model, billable_model ORDER BY total_cost_usd DESC"),
    ("Session drilldown", "table", "SELECT session_date, session_id, SUM(allocated_total_cost_usd) AS total_cost_usd, SUM(allocated_total_tokens) AS total_tokens, COUNT(*) AS command_count FROM {table} GROUP BY session_date, session_id ORDER BY total_cost_usd DESC LIMIT 500"),
]


@dataclass
class ExportSummary:
    rows: int = 0
    total_cost: float = 0.0
    total_tokens: float = 0.0
    total_input_tokens: float = 0.0
    total_cache_read_tokens: float = 0.0
    total_cache_creation_tokens: float = 0.0
    total_output_tokens: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "total_cost_usd": round(self.total_cost, 10),
            "total_tokens": round(self.total_tokens, 3),
            "total_input_tokens": round(self.total_input_tokens, 3),
            "total_cache_read_tokens": round(self.total_cache_read_tokens, 3),
            "total_cache_creation_tokens": round(self.total_cache_creation_tokens, 3),
            "total_output_tokens": round(self.total_output_tokens, 3),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export and load v4.5 command costs into BigQuery/ClickHouse demos.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Write normalized local CSV.")
    add_export_args(export_parser)

    validate_parser = subparsers.add_parser("validate-local", help="Validate normalized CSV against source totals.")
    add_export_args(validate_parser)
    validate_parser.add_argument("--expect-full-row-count", action=argparse.BooleanOptionalAction, default=False)

    bq_parser = subparsers.add_parser("load-bigquery", help="Load normalized CSV into BigQuery with bq CLI.")
    add_export_args(bq_parser)
    bq_parser.add_argument("--project-id", default=os.environ.get("BIGQUERY_PROJECT_ID", ""))
    bq_parser.add_argument("--dataset", default=os.environ.get("BIGQUERY_DATASET", DEFAULT_DATASET))
    bq_parser.add_argument("--table", default=DEFAULT_TABLE)
    bq_parser.add_argument("--explorer-table", default=DEFAULT_EXPLORER_TABLE)
    bq_parser.add_argument("--skip-export", action="store_true")

    ch_parser = subparsers.add_parser("load-clickhouse", help="Load normalized CSV into ClickHouse Cloud over HTTPS.")
    add_export_args(ch_parser)
    ch_parser.add_argument("--host", default=os.environ.get("CLICKHOUSE_HOST", ""))
    ch_parser.add_argument("--port", default=os.environ.get("CLICKHOUSE_PORT", "8443"))
    ch_parser.add_argument("--user", default=os.environ.get("CLICKHOUSE_USER", ""))
    ch_parser.add_argument("--password", default=os.environ.get("CLICKHOUSE_PASSWORD", ""))
    ch_parser.add_argument("--database", default=os.environ.get("CLICKHOUSE_DATABASE", DEFAULT_DATASET))
    ch_parser.add_argument("--table", default=DEFAULT_TABLE)
    ch_parser.add_argument("--explorer-table", default=DEFAULT_EXPLORER_TABLE)
    ch_parser.add_argument("--skip-export", action="store_true")

    mb_parser = subparsers.add_parser("create-metabase", help="Create Metabase databases, dashboards, and cards.")
    mb_parser.add_argument("--metabase-url", default=os.environ.get("METABASE_URL", ""))
    mb_parser.add_argument("--api-key", default=os.environ.get("METABASE_API_KEY", ""))
    mb_parser.add_argument("--bigquery-project-id", default=os.environ.get("BIGQUERY_PROJECT_ID", ""))
    mb_parser.add_argument("--bigquery-dataset", default=os.environ.get("BIGQUERY_DATASET", DEFAULT_DATASET))
    mb_parser.add_argument("--clickhouse-host", default=os.environ.get("CLICKHOUSE_HOST", ""))
    mb_parser.add_argument("--clickhouse-port", default=os.environ.get("CLICKHOUSE_PORT", "8443"))
    mb_parser.add_argument("--clickhouse-user", default=os.environ.get("CLICKHOUSE_USER", ""))
    mb_parser.add_argument("--clickhouse-password", default=os.environ.get("CLICKHOUSE_PASSWORD", ""))
    mb_parser.add_argument("--clickhouse-database", default=os.environ.get("CLICKHOUSE_DATABASE", DEFAULT_DATASET))
    mb_parser.add_argument("--bigquery-database-id", type=int, default=to_int(os.environ.get("METABASE_BIGQUERY_DATABASE_ID")))
    mb_parser.add_argument("--clickhouse-database-id", type=int, default=to_int(os.environ.get("METABASE_CLICKHOUSE_DATABASE_ID")))
    mb_parser.add_argument("--skip-card-validation", action="store_true")

    all_parser = subparsers.add_parser("all", help="Export, load both warehouses, and create Metabase dashboards.")
    add_export_args(all_parser)
    all_parser.add_argument("--skip-metabase", action="store_true")

    return parser.parse_args()


def add_export_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--summary-output", default=str(DEFAULT_LOCAL_SUMMARY))
    parser.add_argument("--explorer-input", default=str(DEFAULT_EXPLORER_INPUT))
    parser.add_argument("--pricing-table", default=os.environ.get("USAGE_PRICING_TABLE", ""))
    parser.add_argument("--limit", type=int, default=0, help="Limit source rows for fixtures or smoke tests.")


def to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def to_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return 0


def session_id(row: dict[str, str]) -> str:
    return Path(row.get("file", "")).stem or "unknown-session"


def normalized_text(row: dict[str, str], key: str) -> str:
    return " ".join((row.get(key) or "").split())


def source_rows(path: Path, limit: int = 0) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("schema_version") != SCHEMA_VERSION:
                continue
            if row.get("classification_revision") != CLASSIFICATION_REVISION:
                continue
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    return rows


def enrich_phase_fields(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    enriched = [dict(row) for row in rows]
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in enriched:
        grouped[(session_id(row), row.get("prompt_index") or "0")].append(row)

    for group_rows in grouped.values():
        for item in classify_prompt_window(group_rows):
            item.row["workflow_phase"] = item.workflow_phase
            item.row["efficiency_label"] = item.efficiency_label
    return enriched


def component_costs(row: dict[str, str], pricing_table: dict[str, Any]) -> dict[str, float]:
    pricing = pricing_for_model(pricing_table, row.get("billable_model") or "")
    input_price = price_component(pricing, "input_cost_per_token", "prompt_cost_per_token")
    output_price = price_component(pricing, "output_cost_per_token", "completion_cost_per_token")
    cache_read_price = price_component(pricing, "cache_read_input_token_cost", "cache_read_cost_per_token") or input_price
    cache_creation_price = price_component(pricing, "cache_creation_input_token_cost", "cache_creation_cost_per_token") or input_price

    input_tokens = to_float(row.get("allocated_input_tokens"))
    cache_read_tokens = to_float(row.get("allocated_cache_read_tokens"))
    cache_creation_tokens = to_float(row.get("allocated_cache_creation_tokens"))
    output_tokens = to_float(row.get("allocated_output_tokens"))
    fresh_input_tokens = max(0.0, input_tokens - cache_read_tokens - cache_creation_tokens)

    raw = {
        "allocated_fresh_input_cost_usd": fresh_input_tokens * input_price if input_price is not None else 0.0,
        "allocated_cache_read_cost_usd": cache_read_tokens * cache_read_price if cache_read_price is not None else 0.0,
        "allocated_cache_creation_cost_usd": cache_creation_tokens * cache_creation_price if cache_creation_price is not None else 0.0,
        "allocated_output_cost_usd": output_tokens * output_price if output_price is not None else 0.0,
    }
    raw_total = sum(raw.values())
    target_total = to_float(row.get("allocated_total_cost_usd"))
    if raw_total > 0 and target_total > 0:
        scale = target_total / raw_total
        return {key: value * scale for key, value in raw.items()}
    return raw


def normalize_row(row: dict[str, str], pricing_table: dict[str, Any]) -> dict[str, Any]:
    costs = component_costs(row, pricing_table)
    allocated_input_tokens = to_float(row.get("allocated_input_tokens"))
    allocated_cache_read_tokens = to_float(row.get("allocated_cache_read_tokens"))
    allocated_cache_creation_tokens = to_float(row.get("allocated_cache_creation_tokens"))
    output: dict[str, Any] = {
        "session_date": row.get("session_date") or "",
        "session_id": session_id(row),
        "prompt_index": to_int(row.get("prompt_index")),
        "command_index": to_int(row.get("command_index")),
        "workflow_phase": row.get("workflow_phase") or "orientation",
        "efficiency_label": row.get("efficiency_label") or "expected_overhead",
        "agent_tool_intention": normalized_text(row, "agent_tool_intention"),
        "request_origin": normalized_text(row, "request_origin"),
        "work_motivation": normalized_text(row, "work_motivation"),
        "function_name": normalized_text(row, "function_name"),
        "shell_verb": normalized_text(row, "shell_verb"),
        "tool_execution_mode": normalized_text(row, "tool_execution_mode"),
        "model": normalized_text(row, "model"),
        "provider": normalized_text(row, "provider"),
        "billable_model": normalized_text(row, "billable_model"),
        "allocated_input_tokens": allocated_input_tokens,
        "allocated_cache_read_tokens": allocated_cache_read_tokens,
        "allocated_cache_creation_tokens": allocated_cache_creation_tokens,
        "allocated_output_tokens": to_float(row.get("allocated_output_tokens")),
        "allocated_reasoning_tokens": to_float(row.get("allocated_reasoning_tokens")),
        "allocated_total_tokens": to_float(row.get("allocated_total_tokens")),
        "allocated_fresh_input_tokens": max(0.0, allocated_input_tokens - allocated_cache_read_tokens - allocated_cache_creation_tokens),
        "allocated_total_cost_usd": to_float(row.get("allocated_total_cost_usd")),
        "prompt_input_tokens": to_float(row.get("prompt_input_tokens")),
        "prompt_cache_read_tokens": to_float(row.get("prompt_cache_read_tokens")),
        "prompt_cache_creation_tokens": to_float(row.get("prompt_cache_creation_tokens")),
        "prompt_output_tokens": to_float(row.get("prompt_output_tokens")),
        "prompt_reasoning_tokens": to_float(row.get("prompt_reasoning_tokens")),
        "prompt_total_tokens": to_float(row.get("prompt_total_tokens")),
        "prompt_derived_total_cost_usd": to_float(row.get("prompt_derived_total_cost_usd")),
    }
    output.update(costs)
    return output


def export_csv(input_path: Path, output_path: Path, summary_path: Path, pricing_table_path: str = "", limit: int = 0) -> ExportSummary:
    rows = source_rows(input_path, limit=limit)
    pricing_table = load_pricing_table(pricing_table_path or None)
    enriched = enrich_phase_fields(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary = ExportSummary()
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for row in enriched:
            normalized = normalize_row(row, pricing_table)
            writer.writerow(normalized)
            summary.rows += 1
            summary.total_cost += to_float(normalized["allocated_total_cost_usd"])
            summary.total_tokens += to_float(normalized["allocated_total_tokens"])
            summary.total_input_tokens += to_float(normalized["allocated_input_tokens"])
            summary.total_cache_read_tokens += to_float(normalized["allocated_cache_read_tokens"])
            summary.total_cache_creation_tokens += to_float(normalized["allocated_cache_creation_tokens"])
            summary.total_output_tokens += to_float(normalized["allocated_output_tokens"])
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary.as_dict(), indent=2) + "\n", encoding="utf-8")
    return summary


def read_summary_from_csv(path: Path) -> ExportSummary:
    summary = ExportSummary()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            summary.rows += 1
            summary.total_cost += to_float(row.get("allocated_total_cost_usd"))
            summary.total_tokens += to_float(row.get("allocated_total_tokens"))
            summary.total_input_tokens += to_float(row.get("allocated_input_tokens"))
            summary.total_cache_read_tokens += to_float(row.get("allocated_cache_read_tokens"))
            summary.total_cache_creation_tokens += to_float(row.get("allocated_cache_creation_tokens"))
            summary.total_output_tokens += to_float(row.get("allocated_output_tokens"))
    return summary

def validate_local(args: argparse.Namespace) -> None:
    summary = export_csv(Path(args.input), Path(args.output), Path(args.summary_output), args.pricing_table, args.limit)
    missing = [column for column in OUTPUT_COLUMNS if column not in csv_header(Path(args.output))]
    if missing:
        raise RuntimeError(f"normalized output is missing columns: {missing}")
    header = csv_header(Path(args.output))
    forbidden_fragments = ["preview", "hash", "workdir", "delegated_task", "terminal_context", "confidence", "source", "debug"]
    leaked = [column for column in header if any(fragment in column for fragment in forbidden_fragments)]
    if leaked:
        raise RuntimeError(f"dropped text/debug fields leaked into normalized output: {leaked}")
    if "usage_command_cost_component" in header:
        raise RuntimeError("component fanout table was generated unexpectedly")
    if args.expect_full_row_count and summary.rows != EXPECTED_FULL_ROW_COUNT:
        raise RuntimeError(f"expected {EXPECTED_FULL_ROW_COUNT} rows, got {summary.rows}")
    explorer_summary = validate_explorer_local(Path(args.explorer_input))
    print(json.dumps({"command_costs": summary.as_dict(), "cost_explorer_commands_v1": explorer_summary.as_dict()}, indent=2))


def csv_header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        return next(reader)


def validate_explorer_local(path: Path) -> ExportSummary:
    if not path.exists():
        raise RuntimeError(f"explorer commands CSV is missing: {path}")
    header = csv_header(path)
    expected = explorer_commands_columns()
    if header != expected:
        raise RuntimeError(f"explorer commands header mismatch: expected {len(expected)} columns, got {len(header)}")
    leaked = sorted(column for column in header if column in EXPLORER_FORBIDDEN_COLUMNS)
    if leaked:
        raise RuntimeError(f"hash/debug columns leaked into explorer commands CSV: {leaked}")
    return read_summary_from_csv(path)


def bigquery_field_type(column: str, *, date_columns: set[str], integer_columns: set[str], numeric_columns: set[str]) -> str:
    if column in date_columns:
        return "DATE"
    if column in integer_columns:
        return "INTEGER"
    if column in numeric_columns:
        return "FLOAT"
    return "STRING"


def write_bigquery_schema(
    path: Path,
    *,
    columns: list[str],
    date_columns: set[str],
    integer_columns: set[str],
    numeric_columns: set[str],
) -> None:
    schema = [
        {"name": column, "type": bigquery_field_type(column, date_columns=date_columns, integer_columns=integer_columns, numeric_columns=numeric_columns), "mode": "NULLABLE"}
        for column in columns
    ]
    path.write_text(json.dumps(schema, indent=2), encoding="utf-8")


def run_checked(command: list[str]) -> None:
    print("+ " + " ".join(command))
    subprocess.run(command, check=True)


def run_capture_json(command: list[str]) -> Any:
    print("+ " + " ".join(command))
    completed = subprocess.run(command, check=True, text=True, capture_output=True)
    return json.loads(completed.stdout)


def load_bigquery_csv(
    project_id: str,
    dataset: str,
    table: str,
    csv_path: Path,
    schema_path: Path,
    *,
    allow_quoted_newlines: bool = False,
) -> None:
    command = [
        "bq",
        "--project_id",
        project_id,
        "load",
        "--replace",
        "--source_format=CSV",
        "--skip_leading_rows=1",
    ]
    if allow_quoted_newlines:
        command.append("--allow_quoted_newlines")
    command.extend(
        [
            "--schema",
            str(schema_path),
            f"{project_id}:{dataset}.{table}",
            str(csv_path),
        ]
    )
    run_checked(command)


def load_bigquery(args: argparse.Namespace) -> None:
    if not args.skip_export:
        export_csv(Path(args.input), Path(args.output), Path(args.summary_output), args.pricing_table, args.limit)
    explorer_path = Path(args.explorer_input)
    validate_explorer_local(explorer_path)
    if not args.project_id:
        raise RuntimeError("Missing BIGQUERY_PROJECT_ID or --project-id")
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise RuntimeError("Missing GOOGLE_APPLICATION_CREDENTIALS")
    if not shutil.which("bq"):
        raise RuntimeError("The bq CLI is required for BigQuery loading in this repo environment")

    ensure_bigquery_dataset(args.project_id, args.dataset)
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        compact_schema_path = temp / "command_costs.schema.json"
        explorer_schema_path = temp / "cost_explorer_commands_v1.schema.json"
        write_bigquery_schema(
            compact_schema_path,
            columns=OUTPUT_COLUMNS,
            date_columns=DATE_COLUMNS,
            integer_columns=INTEGER_COLUMNS,
            numeric_columns=NUMERIC_COLUMNS,
        )
        write_bigquery_schema(
            explorer_schema_path,
            columns=explorer_commands_columns(),
            date_columns=EXPLORER_DATE_COLUMNS,
            integer_columns=EXPLORER_INTEGER_COLUMNS,
            numeric_columns=EXPLORER_NUMERIC_COLUMNS,
        )
        load_bigquery_csv(args.project_id, args.dataset, args.table, Path(args.output), compact_schema_path)
        load_bigquery_csv(
            args.project_id,
            args.dataset,
            args.explorer_table,
            explorer_path,
            explorer_schema_path,
            allow_quoted_newlines=True,
        )
        create_bigquery_views(args.project_id, args.dataset, args.table)
    validate_bigquery(args.project_id, args.dataset, args.table, Path(args.output))
    validate_bigquery(args.project_id, args.dataset, args.explorer_table, explorer_path)


def ensure_bigquery_dataset(project_id: str, dataset: str) -> None:
    show_command = ["bq", "--project_id", project_id, "show", f"{project_id}:{dataset}"]
    completed = subprocess.run(show_command, text=True, capture_output=True)
    if completed.returncode == 0:
        return
    run_checked(["bq", "--project_id", project_id, "mk", "--dataset", dataset])


def create_bigquery_views(project_id: str, dataset: str, table: str) -> None:
    qualified_table = f"`{project_id}.{dataset}.{table}`"
    for view_name, query in VIEW_QUERIES.items():
        sql = f"CREATE OR REPLACE VIEW `{project_id}.{dataset}.{view_name}` AS {query.format(table=qualified_table, any_value='ANY_VALUE')}"
        run_checked(["bq", "--project_id", project_id, "query", "--use_legacy_sql=false", sql])


def validate_bigquery(project_id: str, dataset: str, table: str, output_path: Path) -> None:
    local = read_summary_from_csv(output_path)
    sql = (
        f"SELECT COUNT(*) AS row_count, SUM(allocated_total_cost_usd) AS total_cost_usd, "
        f"SUM(allocated_total_tokens) AS total_tokens FROM `{project_id}.{dataset}.{table}`"
    )
    rows = run_capture_json(["bq", "--project_id", project_id, "query", "--format=json", "--use_legacy_sql=false", sql])
    remote = rows[0] if rows else {}
    assert_totals_match(
        local,
        remote_rows=to_int(remote.get("row_count")),
        remote_cost=to_float(remote.get("total_cost_usd")),
        remote_tokens=to_float(remote.get("total_tokens")),
        label=f"BigQuery {table}",
    )


def clickhouse_type(column: str, *, date_columns: set[str], integer_columns: set[str], numeric_columns: set[str]) -> str:
    if column in date_columns:
        return "Date"
    if column in integer_columns:
        return "UInt32"
    if column in numeric_columns:
        return "Float64"
    return "LowCardinality(String)" if column in DIMENSION_COLUMNS else "String"


def clickhouse_query(args: argparse.Namespace, query: str, data: bytes | None = None) -> bytes:
    if not args.host or not args.user or not args.password:
        raise RuntimeError("Missing ClickHouse credentials: CLICKHOUSE_HOST, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD")
    scheme = "https" if str(args.port) == "8443" else "http"
    params = {}
    if args.database and not query.lstrip().upper().startswith("CREATE DATABASE"):
        params["database"] = args.database
    query_string = urllib.parse.urlencode(params)
    url = f"{scheme}://{args.host}:{args.port}/"
    if query_string:
        url = f"{url}?{query_string}"
    request = urllib.request.Request(url, data=data or query.encode("utf-8"), method="POST")
    request.add_header("X-ClickHouse-User", args.user)
    request.add_header("X-ClickHouse-Key", args.password)
    if data is not None:
        request.data = query.encode("utf-8") + b"\n" + data
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
        return response.read()


def load_clickhouse_csv(
    args: argparse.Namespace,
    *,
    table: str,
    csv_path: Path,
    columns: list[str],
    date_columns: set[str],
    integer_columns: set[str],
    numeric_columns: set[str],
) -> None:
    column_sql = ",\n  ".join(
        f"{column} {clickhouse_type(column, date_columns=date_columns, integer_columns=integer_columns, numeric_columns=numeric_columns)}"
        for column in columns
    )
    clickhouse_query(args, f"DROP TABLE IF EXISTS {table}")
    clickhouse_query(args, f"CREATE TABLE {table} (\n  {column_sql}\n) ENGINE = MergeTree ORDER BY (session_date, session_id, prompt_index, command_index)")
    clickhouse_query(args, f"INSERT INTO {table} FORMAT CSVWithNames", data=csv_path.read_bytes())


def load_clickhouse(args: argparse.Namespace) -> None:
    if not args.skip_export:
        export_csv(Path(args.input), Path(args.output), Path(args.summary_output), args.pricing_table, args.limit)
    explorer_path = Path(args.explorer_input)
    validate_explorer_local(explorer_path)
    clickhouse_query(args, f"CREATE DATABASE IF NOT EXISTS {args.database}")
    load_clickhouse_csv(
        args,
        table=args.table,
        csv_path=Path(args.output),
        columns=OUTPUT_COLUMNS,
        date_columns=DATE_COLUMNS,
        integer_columns=INTEGER_COLUMNS,
        numeric_columns=NUMERIC_COLUMNS,
    )
    load_clickhouse_csv(
        args,
        table=args.explorer_table,
        csv_path=explorer_path,
        columns=explorer_commands_columns(),
        date_columns=EXPLORER_DATE_COLUMNS,
        integer_columns=EXPLORER_INTEGER_COLUMNS,
        numeric_columns=EXPLORER_NUMERIC_COLUMNS,
    )
    for view_name, query in VIEW_QUERIES.items():
        clickhouse_query(args, f"CREATE OR REPLACE VIEW {view_name} AS {query.format(table=args.table, any_value='any')}")
    validate_clickhouse(args, args.table, Path(args.output))
    validate_clickhouse(args, args.explorer_table, explorer_path)


def validate_clickhouse(args: argparse.Namespace, table: str, output_path: Path) -> None:
    local = read_summary_from_csv(output_path)
    result = clickhouse_query(
        args,
        f"SELECT count() AS rows, sum(allocated_total_cost_usd) AS total_cost_usd, sum(allocated_total_tokens) AS total_tokens FROM {table} FORMAT JSON",
    )
    payload = json.loads(result.decode("utf-8"))
    remote = payload.get("data", [{}])[0]
    assert_totals_match(
        local,
        remote_rows=to_int(remote.get("rows")),
        remote_cost=to_float(remote.get("total_cost_usd")),
        remote_tokens=to_float(remote.get("total_tokens")),
        label=f"ClickHouse {table}",
    )


def assert_totals_match(local: ExportSummary, *, remote_rows: int, remote_cost: float, remote_tokens: float, label: str) -> None:
    if remote_rows != local.rows:
        raise RuntimeError(f"{label} row count mismatch: remote={remote_rows} local={local.rows}")
    if abs(remote_cost - local.total_cost) > 0.01:
        raise RuntimeError(f"{label} cost mismatch: remote={remote_cost} local={local.total_cost}")
    if abs(remote_tokens - local.total_tokens) > 0.01:
        raise RuntimeError(f"{label} token mismatch: remote={remote_tokens} local={local.total_tokens}")
    print(
        json.dumps(
            {
                "warehouse": label,
                "rows": remote_rows,
                "total_cost_usd": round(remote_cost, 10),
                "total_tokens": round(remote_tokens, 3),
                "status": "validated",
            },
            indent=2,
        )
    )


class MetabaseClient:
    def __init__(self, url: str, api_key: str) -> None:
        if not url or not api_key:
            raise RuntimeError("Missing METABASE_URL or METABASE_API_KEY")
        self.url = url.rstrip("/")
        self.api_key = api_key

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(f"{self.url}{path}", data=data, method=method)
        request.add_header("X-API-Key", self.api_key)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Metabase API {method} {path} failed: HTTP {exc.code} {exc.read().decode('utf-8', errors='replace')}") from exc


def create_metabase(args: argparse.Namespace) -> None:
    client = MetabaseClient(args.metabase_url, args.api_key)
    bigquery_db_id = args.bigquery_database_id or create_metabase_bigquery_database(client, args)
    clickhouse_db_id = args.clickhouse_database_id or create_metabase_clickhouse_database(client, args)
    create_metabase_dashboard(
        client,
        "Session Cost Demo - BigQuery",
        bigquery_db_id,
        table_sql=f"`{args.bigquery_dataset}.{DEFAULT_TABLE}`",
        validate_cards=not args.skip_card_validation,
    )
    create_metabase_dashboard(
        client,
        "Session Cost Demo - ClickHouse",
        clickhouse_db_id,
        table_sql=DEFAULT_TABLE,
        validate_cards=not args.skip_card_validation,
    )


def create_metabase_bigquery_database(client: MetabaseClient, args: argparse.Namespace) -> int:
    if not args.bigquery_project_id:
        raise RuntimeError("Missing BIGQUERY_PROJECT_ID or --bigquery-project-id for Metabase BigQuery connection")
    payload = {
        "name": "Session Metrics Demo - BigQuery",
        "engine": "bigquery-cloud-sdk",
        "details": {
            "project-id": args.bigquery_project_id,
            "dataset-filters-type": "inclusion",
            "dataset-filters-patterns": args.bigquery_dataset,
            "service-account-json": Path(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")).read_text(encoding="utf-8")
            if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            else "",
        },
    }
    database = client.request("POST", "/api/database", payload)
    return int(database["id"])


def create_metabase_clickhouse_database(client: MetabaseClient, args: argparse.Namespace) -> int:
    if not args.clickhouse_host or not args.clickhouse_user or not args.clickhouse_password:
        raise RuntimeError("Missing ClickHouse credentials for Metabase ClickHouse connection")
    payload = {
        "name": "Session Metrics Demo - ClickHouse",
        "engine": "clickhouse",
        "details": {
            "host": args.clickhouse_host,
            "port": to_int(args.clickhouse_port) or 8443,
            "user": args.clickhouse_user,
            "password": args.clickhouse_password,
            "dbname": args.clickhouse_database,
            "ssl": True,
        },
    }
    database = client.request("POST", "/api/database", payload)
    return int(database["id"])


def create_metabase_dashboard(client: MetabaseClient, title: str, database_id: int, *, table_sql: str, validate_cards: bool) -> None:
    dashboard = client.request("POST", "/api/dashboard", {"name": title})
    dashboard_id = int(dashboard["id"])
    dashboard_cards = []
    for index, (name, display, sql_template) in enumerate(METABASE_CARDS):
        sql = sql_template.format(table=table_sql)
        card = client.request(
            "POST",
            "/api/card",
            {
                "name": f"{title}: {name}",
                "display": display,
                "dataset_query": {"type": "native", "database": database_id, "native": {"query": sql}},
                "visualization_settings": {},
            },
        )
        dashboard_cards.append(
            {
                "id": -(index + 1),
                "card_id": int(card["id"]),
                "row": (index // 2) * 6,
                "col": (index % 2) * 12,
                "size_x": 12,
                "size_y": 6,
                "parameter_mappings": [],
                "visualization_settings": {},
            },
        )
        if validate_cards:
            validate_metabase_card(client, int(card["id"]), f"{title}: {name}")
    client.request("PUT", f"/api/dashboard/{dashboard_id}/cards", {"cards": dashboard_cards})
    print(json.dumps({"dashboard": title, "dashboard_id": dashboard_id, "database_id": database_id}, indent=2))


def validate_metabase_card(client: MetabaseClient, card_id: int, name: str) -> None:
    result = client.request("POST", f"/api/card/{card_id}/query", {})
    rows = ((result.get("data") or {}).get("rows") or []) if isinstance(result, dict) else []
    if not rows:
        raise RuntimeError(f"Metabase card returned no rows: {name}")


def run_all(args: argparse.Namespace) -> None:
    export_csv(Path(args.input), Path(args.output), Path(args.summary_output), args.pricing_table, args.limit)
    bq_args = argparse.Namespace(
        **vars(args),
        project_id=os.environ.get("BIGQUERY_PROJECT_ID", ""),
        dataset=os.environ.get("BIGQUERY_DATASET", DEFAULT_DATASET),
        table=DEFAULT_TABLE,
        explorer_table=DEFAULT_EXPLORER_TABLE,
        skip_export=True,
    )
    ch_args = argparse.Namespace(
        **vars(args),
        host=os.environ.get("CLICKHOUSE_HOST", ""),
        port=os.environ.get("CLICKHOUSE_PORT", "8443"),
        user=os.environ.get("CLICKHOUSE_USER", ""),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=os.environ.get("CLICKHOUSE_DATABASE", DEFAULT_DATASET),
        table=DEFAULT_TABLE,
        explorer_table=DEFAULT_EXPLORER_TABLE,
        skip_export=True,
    )
    load_bigquery(bq_args)
    load_clickhouse(ch_args)
    if not args.skip_metabase:
        mb_args = argparse.Namespace(
            metabase_url=os.environ.get("METABASE_URL", ""),
            api_key=os.environ.get("METABASE_API_KEY", ""),
            bigquery_project_id=os.environ.get("BIGQUERY_PROJECT_ID", ""),
            bigquery_dataset=os.environ.get("BIGQUERY_DATASET", DEFAULT_DATASET),
            clickhouse_host=os.environ.get("CLICKHOUSE_HOST", ""),
            clickhouse_port=os.environ.get("CLICKHOUSE_PORT", "8443"),
            clickhouse_user=os.environ.get("CLICKHOUSE_USER", ""),
            clickhouse_password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
            clickhouse_database=os.environ.get("CLICKHOUSE_DATABASE", DEFAULT_DATASET),
            bigquery_database_id=to_int(os.environ.get("METABASE_BIGQUERY_DATABASE_ID")),
            clickhouse_database_id=to_int(os.environ.get("METABASE_CLICKHOUSE_DATABASE_ID")),
            skip_card_validation=False,
        )
        create_metabase(mb_args)


def main() -> int:
    args = parse_args()
    try:
        if args.command == "export":
            summary = export_csv(Path(args.input), Path(args.output), Path(args.summary_output), args.pricing_table, args.limit)
            print(json.dumps(summary.as_dict(), indent=2))
        elif args.command == "validate-local":
            validate_local(args)
        elif args.command == "load-bigquery":
            load_bigquery(args)
        elif args.command == "load-clickhouse":
            load_clickhouse(args)
        elif args.command == "create-metabase":
            create_metabase(args)
        elif args.command == "all":
            run_all(args)
        return 0
    except Exception as exc:
        print(f"warehouse cost demo failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
