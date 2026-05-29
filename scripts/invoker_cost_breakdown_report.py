#!/usr/bin/env python3
"""Generate hierarchical Invoker usage cost breakdowns from local report CSVs.

The report treats request_pattern_path as the scenario level, then drills into
sessions, prompts, function tools, and shell verbs. Shell verbs are the
exec_command sub-breakdown already emitted by planning_vs_execution_report.py.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PATH = REPO_ROOT / "scripts" / "mixpanel_export_usage.py"


spec = importlib.util.spec_from_file_location("mixpanel_export_usage", EXPORTER_PATH)
assert spec and spec.loader
exporter = importlib.util.module_from_spec(spec)
sys.modules["mixpanel_export_usage"] = exporter
spec.loader.exec_module(exporter)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hierarchical cost breakdown for Invoker usage.")
    parser.add_argument("--reports-dir", default=str(REPO_ROOT / "reports"))
    parser.add_argument("--task-categorization-config", default=str(REPO_ROOT / "config" / "task-categorization.yaml"))
    parser.add_argument("--request-pattern-config", default=str(REPO_ROOT / "config" / "request-patterns.yaml"))
    parser.add_argument("--task-type-label", default="Invoker Plan Submission")
    parser.add_argument("--markdown-out", default=str(REPO_ROOT / "reports" / "invoker-cost-breakdown.md"))
    parser.add_argument("--json-out", default=str(REPO_ROOT / "reports" / "invoker-cost-breakdown.json"))
    parser.add_argument("--top-sessions", type=int, default=12)
    parser.add_argument("--top-prompts", type=int, default=8)
    parser.add_argument("--top-tools", type=int, default=12)
    return parser.parse_args()


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


def money(value: float) -> str:
    return f"${value:,.2f}"


def pct(value: float, total: float) -> str:
    return f"{(value / total * 100.0) if total else 0.0:.1f}%"


def compact(text: str, limit: int = 150) -> str:
    value = " ".join((text or "").split())
    return value[:limit]


def prompt_cost(row: dict[str, str]) -> float:
    return to_float(row.get("derived_total_cost_usd")) or to_float(row.get("estimated_cost_usd"))


def session_identity(file_path: str) -> str:
    return exporter.session_identity(file_path)


def load_task_categorizer(config_path: str) -> Any:
    config = exporter.load_task_categorization_config(config_path)
    # Keep this report deterministic and offline.
    for classifier in config.get("classifiers", []) or []:
        if classifier.get("type") == "codex":
            classifier["enabled"] = False
    return exporter.TaskCategorizer(config)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def add_metric(target: dict[str, Any], *, cost: float = 0.0, tokens: float = 0.0, cache: float = 0.0, calls: int = 0) -> None:
    target["cost_usd"] = target.get("cost_usd", 0.0) + cost
    target["total_tokens"] = target.get("total_tokens", 0.0) + tokens
    target["cache_read_tokens"] = target.get("cache_read_tokens", 0.0) + cache
    target["calls"] = target.get("calls", 0) + calls


def rollup_tools(rows: list[dict[str, str]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {"function_name": {}, "shell_verb": {}}
    for row in rows:
        dimension = row.get("dimension", "")
        if dimension not in grouped:
            continue
        name = row.get("name", "") or "unknown"
        bucket = grouped[dimension].setdefault(name, {"name": name, "calls": 0, "cost_usd": 0.0, "tokens": 0.0})
        bucket["calls"] += to_int(row.get("calls"))
        bucket["cost_usd"] += to_float(row.get("allocated_total_cost_usd"))
        bucket["tokens"] += to_float(row.get("allocated_total_tokens"))
    return {
        dimension: sorted(values.values(), key=lambda item: item["cost_usd"], reverse=True)
        for dimension, values in grouped.items()
    }


def build_hierarchy(args: argparse.Namespace) -> dict[str, Any]:
    reports_dir = Path(args.reports_dir)
    prompts = read_csv(reports_dir / "planning-vs-execution-prompts.csv")
    tool_rows = read_csv(reports_dir / "planning-vs-execution-tool-attribution.csv")

    task_categorizer = load_task_categorizer(args.task_categorization_config)
    pattern_config = exporter.load_request_pattern_config(args.request_pattern_config)
    pattern_categorizer = exporter.RequestPatternCategorizer(pattern_config)

    selected: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    scenarios: dict[str, dict[str, Any]] = {}
    total = {"cost_usd": 0.0, "total_tokens": 0.0, "cache_read_tokens": 0.0, "prompts": 0}

    for row in prompts:
        task = task_categorizer.classify(row)
        if task.task_type_label != args.task_type_label:
            continue
        pattern = pattern_categorizer.classify(row)
        cost = prompt_cost(row)
        key = (
            row.get("model", ""),
            row.get("bucket", ""),
            session_identity(row.get("file", "")),
            to_int(row.get("prompt_index")),
        )
        session_id = key[2]
        scenario_id = pattern.request_pattern_path
        scenario = scenarios.setdefault(
            scenario_id,
            {
                "scenario": scenario_id,
                "cost_usd": 0.0,
                "total_tokens": 0.0,
                "cache_read_tokens": 0.0,
                "prompts": 0,
                "sessions": {},
            },
        )
        session = scenario["sessions"].setdefault(
            session_id,
            {
                "session_id": session_id,
                "file": row.get("file", ""),
                "session_date": row.get("session_date", ""),
                "bucket": row.get("bucket", ""),
                "model": row.get("model", ""),
                "billable_model": row.get("billable_model", ""),
                "session_cwd": row.get("session_cwd", ""),
                "first_prompt_preview": compact(row.get("first_prompt_preview", ""), 180),
                "cost_usd": 0.0,
                "total_tokens": 0.0,
                "cache_read_tokens": 0.0,
                "prompts": [],
            },
        )
        prompt = {
            "key": key,
            "prompt_index": to_int(row.get("prompt_index")),
            "prompt_preview": compact(row.get("prompt_preview", ""), 220),
            "cost_usd": cost,
            "total_tokens": to_float(row.get("total_tokens_delta")),
            "cache_read_tokens": to_float(row.get("cache_read_tokens_delta")),
            "tool_rows": [],
        }
        session["prompts"].append(prompt)
        selected[key] = {"scenario": scenario_id, "session_id": session_id, "prompt": prompt}

        add_metric(scenario, cost=cost, tokens=prompt["total_tokens"], cache=prompt["cache_read_tokens"])
        scenario["prompts"] += 1
        add_metric(session, cost=cost, tokens=prompt["total_tokens"], cache=prompt["cache_read_tokens"])
        total["cost_usd"] += cost
        total["total_tokens"] += prompt["total_tokens"]
        total["cache_read_tokens"] += prompt["cache_read_tokens"]
        total["prompts"] += 1

    for row in tool_rows:
        key = (
            row.get("model", ""),
            row.get("bucket", ""),
            session_identity(row.get("file", "")),
            to_int(row.get("prompt_index")),
        )
        match = selected.get(key)
        if not match:
            continue
        match["prompt"]["tool_rows"].append(row)

    scenario_list = []
    for scenario in scenarios.values():
        scenario_tool_rows: list[dict[str, str]] = []
        sessions = []
        for session in scenario["sessions"].values():
            session_tool_rows: list[dict[str, str]] = []
            for prompt in session["prompts"]:
                prompt["tools"] = rollup_tools(prompt.pop("tool_rows"))
                session_tool_rows.extend(
                    [
                        {
                            "dimension": dimension,
                            "name": item["name"],
                            "calls": str(item["calls"]),
                            "allocated_total_cost_usd": str(item["cost_usd"]),
                            "allocated_total_tokens": str(item["tokens"]),
                        }
                        for dimension, items in prompt["tools"].items()
                        for item in items
                    ]
                )
            session["prompts"].sort(key=lambda item: item["cost_usd"], reverse=True)
            session["tools"] = rollup_tools(session_tool_rows)
            sessions.append(session)
            scenario_tool_rows.extend(session_tool_rows)
        sessions.sort(key=lambda item: item["cost_usd"], reverse=True)
        scenario["sessions"] = sessions
        scenario["tools"] = rollup_tools(scenario_tool_rows)
        scenario_list.append(scenario)

    scenario_list.sort(key=lambda item: item["cost_usd"], reverse=True)
    return {
        "task_type_label": args.task_type_label,
        "total": total,
        "scenarios": scenario_list,
    }


def render_tool_table(lines: list[str], title: str, tools: list[dict[str, Any]], *, total_cost: float, limit: int) -> None:
    if not tools:
        return
    lines.append(title)
    lines.append("")
    lines.append("| Tool | Calls | Cost | Share |")
    lines.append("|---|---:|---:|---:|")
    for row in tools[:limit]:
        lines.append(f"| `{row['name']}` | {row['calls']:,} | {money(row['cost_usd'])} | {pct(row['cost_usd'], total_cost)} |")
    lines.append("")


def render_markdown(data: dict[str, Any], args: argparse.Namespace) -> str:
    total_cost = data["total"]["cost_usd"]
    lines = [
        "# Invoker Cost Breakdown",
        "",
        f"- Task bucket: `{data['task_type_label']}`",
        f"- Total prompt cost: **{money(total_cost)}**",
        f"- Prompts: **{data['total']['prompts']:,}**",
        f"- Tokens: **{int(data['total']['total_tokens']):,}** total, **{int(data['total']['cache_read_tokens']):,}** cache-read",
        "",
        "## Scenario Flow",
        "",
        "| Scenario | Prompts | Cost | Share | Tokens | Cache-read |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for scenario in data["scenarios"]:
        lines.append(
            f"| `{scenario['scenario']}` | {scenario['prompts']:,} | {money(scenario['cost_usd'])} | "
            f"{pct(scenario['cost_usd'], total_cost)} | {int(scenario['total_tokens']):,} | {int(scenario['cache_read_tokens']):,} |"
        )
    lines.append("")

    for scenario in data["scenarios"]:
        scenario_cost = scenario["cost_usd"]
        lines.append(f"## Scenario: `{scenario['scenario']}`")
        lines.append("")
        lines.append(f"- Cost: **{money(scenario_cost)}** ({pct(scenario_cost, total_cost)} of bucket)")
        lines.append(f"- Prompts: **{scenario['prompts']:,}**")
        lines.append("")
        render_tool_table(lines, "### Function Tools", scenario["tools"]["function_name"], total_cost=scenario_cost, limit=args.top_tools)
        render_tool_table(lines, "### `exec_command` Shell Verbs", scenario["tools"]["shell_verb"], total_cost=scenario_cost, limit=args.top_tools)

        lines.append("### Top Sessions")
        lines.append("")
        lines.append("| Session | Date | Model | Cost | Share | Prompts | CWD | First prompt |")
        lines.append("|---|---|---|---:|---:|---:|---|---|")
        for session in scenario["sessions"][: args.top_sessions]:
            lines.append(
                f"| `{session['session_id']}` | {session['session_date']} | `{session['billable_model']}` | "
                f"{money(session['cost_usd'])} | {pct(session['cost_usd'], scenario_cost)} | "
                f"{len(session['prompts'])} | `{compact(session['session_cwd'], 70)}` | {session['first_prompt_preview']} |"
            )
        lines.append("")

        for session in scenario["sessions"][: args.top_sessions]:
            lines.append(f"#### Session `{session['session_id']}`")
            lines.append("")
            lines.append(f"- Cost: **{money(session['cost_usd'])}** ({pct(session['cost_usd'], scenario_cost)} of scenario)")
            lines.append(f"- Date/model: `{session['session_date']}` / `{session['billable_model']}`")
            lines.append(f"- CWD: `{session['session_cwd']}`")
            lines.append("")
            render_tool_table(lines, "Function tools", session["tools"]["function_name"], total_cost=session["cost_usd"], limit=args.top_tools)
            render_tool_table(lines, "`exec_command` shell verbs", session["tools"]["shell_verb"], total_cost=session["cost_usd"], limit=args.top_tools)
            lines.append("Top prompts")
            lines.append("")
            lines.append("| Prompt | Cost | Share | Tokens | Cache-read |")
            lines.append("|---|---:|---:|---:|---:|")
            for prompt in session["prompts"][: args.top_prompts]:
                lines.append(
                    f"| {prompt['prompt_preview']} | {money(prompt['cost_usd'])} | {pct(prompt['cost_usd'], session['cost_usd'])} | "
                    f"{int(prompt['total_tokens']):,} | {int(prompt['cache_read_tokens']):,} |"
                )
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "Notes: function-tool and shell-verb tables are separate views. Shell verbs are the nested breakdown for shell commands, "
        "so do not add them to function-tool totals."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    data = build_hierarchy(args)
    markdown_path = Path(args.markdown_out)
    json_path = Path(args.json_out)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(data, args), encoding="utf-8")
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Markdown written: {markdown_path}")
    print(f"JSON written: {json_path}")
    print(f"Scenarios: {len(data['scenarios'])}")
    print(f"Total cost: {money(data['total']['cost_usd'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
