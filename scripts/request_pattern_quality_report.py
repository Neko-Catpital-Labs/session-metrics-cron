#!/usr/bin/env python3
"""Print request pattern cost/call share for local prompt reports."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PATH = REPO_ROOT / "scripts/mixpanel_export_usage.py"
spec = importlib.util.spec_from_file_location("mixpanel_export_usage", EXPORTER_PATH)
assert spec and spec.loader
exporter = importlib.util.module_from_spec(spec)
sys.modules["mixpanel_export_usage"] = exporter
spec.loader.exec_module(exporter)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report cost/call share by recursive request pattern.")
    parser.add_argument("--prompts-csv", default=str(REPO_ROOT / "reports/planning-vs-execution-prompts.csv"))
    parser.add_argument("--request-pattern-config", default="")
    return parser.parse_args()


def cost(row: dict[str, str]) -> float:
    return exporter.to_float(row.get("derived_total_cost_usd") or row.get("estimated_cost_usd"))


def print_table(title: str, rows: list[tuple[str, int, float]], total_calls: int, total_cost: float) -> None:
    print(title)
    print("label,calls,call_share_pct,cost_usd,cost_share_pct")
    for label, calls, label_cost in rows:
        call_share = (calls / total_calls * 100.0) if total_calls else 0.0
        cost_share = (label_cost / total_cost * 100.0) if total_cost else 0.0
        print(f"{label},{calls},{call_share:.2f},{label_cost:.6f},{cost_share:.2f}")
    print()


def main() -> int:
    args = parse_args()
    path = Path(args.prompts_csv)
    if not path.exists():
        print(f"Missing prompts CSV: {path}", file=sys.stderr)
        return 1

    config = exporter.load_request_pattern_config(args.request_pattern_config)
    categorizer = exporter.RequestPatternCategorizer(config)
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    by_pattern: dict[str, list[Any]] = defaultdict(lambda: [0, 0.0])
    by_path: dict[str, list[Any]] = defaultdict(lambda: [0, 0.0])
    total_cost = 0.0
    for row in rows:
        result = categorizer.classify(row)
        row_cost = cost(row)
        total_cost += row_cost
        by_pattern[result.request_pattern][0] += 1
        by_pattern[result.request_pattern][1] += row_cost
        by_path[result.request_pattern_path][0] += 1
        by_path[result.request_pattern_path][1] += row_cost

    total_calls = len(rows)
    pattern_rows = sorted(((label, values[0], values[1]) for label, values in by_pattern.items()), key=lambda item: item[2], reverse=True)
    path_rows = sorted(((label, values[0], values[1]) for label, values in by_path.items()), key=lambda item: item[2], reverse=True)
    print_table("request_pattern", pattern_rows, total_calls, total_cost)
    print_table("request_pattern_path", path_rows, total_calls, total_cost)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
