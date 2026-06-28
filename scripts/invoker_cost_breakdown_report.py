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
import html
from datetime import datetime, timezone
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
    parser.add_argument("--html-out", default=str(REPO_ROOT / "reports" / "invoker-cost-breakdown.html"))
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

def origin_model_rollup(prompts: list[dict[str, str]]) -> dict[str, Any]:
    """Total cost/tokens grouped by (origin, model) across ALL prompts.

    origin is native (direct codex/claude CLI) or omp (Oh My Pi harness); model
    is codex or claude. This yields the four-way split: native+codex,
    native+claude, omp+codex, omp+claude.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    grand = {"cost_usd": 0.0, "total_tokens": 0.0, "cache_read_tokens": 0.0, "prompts": 0}
    for row in prompts:
        origin = row.get("origin") or "native"
        model = row.get("model") or "unknown"
        cost = prompt_cost(row)
        tokens = to_float(row.get("total_tokens_delta"))
        cache = to_float(row.get("cache_read_tokens_delta"))
        group = groups.setdefault(
            (origin, model),
            {"origin": origin, "model": model, "label": f"{origin}+{model}",
             "cost_usd": 0.0, "total_tokens": 0.0, "cache_read_tokens": 0.0, "prompts": 0},
        )
        for target in (group, grand):
            target["cost_usd"] += cost
            target["total_tokens"] += tokens
            target["cache_read_tokens"] += cache
            target["prompts"] += 1
    rows = sorted(groups.values(), key=lambda item: item["cost_usd"], reverse=True)
    return {"rows": rows, "grand_total": grand}


def over_time_rollup(prompts: list[dict[str, str]]) -> dict[str, Any]:
    """Cost/tokens per session_date across ALL prompts, for the over-time view."""
    by_date: dict[str, dict[str, Any]] = {}
    labels: set[str] = set()
    for row in prompts:
        date = (row.get("session_date") or "").strip() or "unknown"
        label = f"{row.get('origin') or 'native'}+{row.get('model') or 'unknown'}"
        labels.add(label)
        cost = prompt_cost(row)
        tokens = to_float(row.get("total_tokens_delta"))
        cache = to_float(row.get("cache_read_tokens_delta"))
        bucket = by_date.setdefault(
            date,
            {"date": date, "cost_usd": 0.0, "total_tokens": 0.0, "cache_read_tokens": 0.0, "prompts": 0, "by_label": {}},
        )
        bucket["cost_usd"] += cost
        bucket["total_tokens"] += tokens
        bucket["cache_read_tokens"] += cache
        bucket["prompts"] += 1
        lab = bucket["by_label"].setdefault(label, {"cost_usd": 0.0, "total_tokens": 0.0})
        lab["cost_usd"] += cost
        lab["total_tokens"] += tokens
    dated = sorted(k for k in by_date if k != "unknown")
    rows = [by_date[k] for k in dated]
    if "unknown" in by_date:
        rows.append(by_date["unknown"])
    return {"rows": rows, "labels": sorted(labels)}


def by_host_rollup(prompts: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Cost/tokens per host across ALL prompts (host column set by the fleet collector)."""
    hosts: dict[str, dict[str, Any]] = {}
    for row in prompts:
        host = (row.get("host") or "").strip() or "local"
        origin = row.get("origin") or "native"
        cost = prompt_cost(row)
        tokens = to_float(row.get("total_tokens_delta"))
        bucket = hosts.setdefault(
            host,
            {"host": host, "cost_usd": 0.0, "total_tokens": 0.0, "prompts": 0, "omp_cost_usd": 0.0, "native_cost_usd": 0.0},
        )
        bucket["cost_usd"] += cost
        bucket["total_tokens"] += tokens
        bucket["prompts"] += 1
        bucket["omp_cost_usd" if origin == "omp" else "native_cost_usd"] += cost
    return sorted(hosts.values(), key=lambda item: item["cost_usd"], reverse=True)



def build_hierarchy(args: argparse.Namespace) -> dict[str, Any]:
    reports_dir = Path(args.reports_dir)
    prompts = read_csv(reports_dir / "planning-vs-execution-prompts.csv")
    tool_rows = read_csv(reports_dir / "planning-vs-execution-tool-attribution.csv")
    origin_model = origin_model_rollup(prompts)
    over_time = over_time_rollup(prompts)
    by_host = by_host_rollup(prompts)

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
        "origin_model": origin_model,
        "over_time": over_time,
        "by_host": by_host,
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
        "## Cost by origin x model (native vs omp, codex vs claude)",
        "",
        "| Origin + Model | Cost | Share | Prompts | Tokens | Cache-read |",
        "|---|---:|---:|---:|---:|---:|",
        *[
            "| `{}` | {} | {} | {:,} | {:,} | {:,} |".format(
                r["label"],
                money(r["cost_usd"]),
                pct(r["cost_usd"], data["origin_model"]["grand_total"]["cost_usd"]),
                r["prompts"],
                int(r["total_tokens"]),
                int(r["cache_read_tokens"]),
            )
            for r in data["origin_model"]["rows"]
        ],
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

def render_html(data: dict[str, Any], args: argparse.Namespace) -> str:
    om = data["origin_model"]
    grand = om["grand_total"]
    grand_cost = grand["cost_usd"]
    over_time = data.get("over_time", {"rows": [], "labels": []})
    by_host = data.get("by_host", [])

    def esc(value: Any) -> str:
        return html.escape(str(value))

    css = (
        "body{font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,sans-serif;margin:2rem auto;max-width:1080px;color:#1b1f24;padding:0 1rem}"
        "h1{font-size:1.6rem;margin-bottom:.2rem}h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid #e2e6ea;padding-bottom:.25rem}"
        ".sub{color:#57606a}"
        ".kpis{display:flex;gap:1rem;flex-wrap:wrap;margin:1rem 0}"
        ".kpi{flex:1;min-width:160px;background:#f6f8fa;border:1px solid #e2e6ea;border-radius:8px;padding:.7rem 1rem}"
        ".kpi .v{font-size:1.5rem;font-weight:700}.kpi .l{color:#57606a;font-size:.78rem;text-transform:uppercase;letter-spacing:.03em}"
        "table{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem}"
        "th,td{padding:.4rem .6rem;border-bottom:1px solid #eef1f4;text-align:left}"
        "th{background:#f6f8fa;font-weight:600}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}"
        "code{background:#f3f5f7;padding:.05rem .3rem;border-radius:4px}"
        ".bar{display:flex;align-items:center;gap:.5rem;margin:.12rem 0}"
        ".bar .d{width:96px;color:#57606a;font-variant-numeric:tabular-nums}"
        ".bar .track{flex:1;background:#f0f3f6;border-radius:4px;overflow:hidden;height:15px}"
        ".bar .fill{height:15px;background:#3b82f6}.bar .fill.tok{background:#10b981}"
        ".bar .v{width:112px;text-align:right;font-variant-numeric:tabular-nums}"
    )

    def table(headers: list[str], body: str) -> str:
        head = "".join((f"<th class=\"n\">{h}</th>" if i else f"<th>{h}</th>") for i, h in enumerate(headers))
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    om_rows = "".join(
        "<tr><td><code>{}</code></td><td class='n'>{}</td><td class='n'>{}</td><td class='n'>{:,}</td><td class='n'>{:,}</td><td class='n'>{:,}</td></tr>".format(
            esc(r["label"]), money(r["cost_usd"]), pct(r["cost_usd"], grand_cost), r["prompts"], int(r["total_tokens"]), int(r["cache_read_tokens"]))
        for r in om["rows"]
    )
    host_rows = "".join(
        "<tr><td><code>{}</code></td><td class='n'>{}</td><td class='n'>{}</td><td class='n'>{}</td><td class='n'>{:,}</td><td class='n'>{:,}</td></tr>".format(
            esc(h["host"]), money(h["cost_usd"]), money(h.get("omp_cost_usd", 0.0)), money(h.get("native_cost_usd", 0.0)), h["prompts"], int(h["total_tokens"]))
        for h in by_host
    )
    rows = over_time["rows"]
    # sqrt-scale bar widths so a few peak days don't squash everything else to invisible.
    max_cost = (max((r["cost_usd"] for r in rows), default=0.0) ** 0.5) or 1.0
    max_tok = (max((r["total_tokens"] for r in rows), default=0.0) ** 0.5) or 1.0
    cost_bars = "".join(
        "<div class='bar'><span class='d'>{}</span><span class='track'><span class='fill' style='width:{:.1f}%'></span></span><span class='v'>{}</span></div>".format(
            esc(r["date"]), (r["cost_usd"] ** 0.5) / max_cost * 100.0, money(r["cost_usd"]))
        for r in rows
    )
    tok_bars = "".join(
        "<div class='bar'><span class='d'>{}</span><span class='track'><span class='fill tok' style='width:{:.1f}%'></span></span><span class='v'>{:,}</span></div>".format(
            esc(r["date"]), (r["total_tokens"] ** 0.5) / max_tok * 100.0, int(r["total_tokens"]))
        for r in rows
    )
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Invoker Fleet Cost Breakdown</title><style>" + css + "</style></head><body>"
        "<h1>Invoker Fleet Cost &amp; Token Breakdown</h1>"
        f"<p class=\"sub\">Generated {esc(generated)} &middot; native = direct codex/claude CLI; omp = Oh My Pi harness</p>"
        "<div class=\"kpis\">"
        f"<div class=\"kpi\"><div class=\"v\">{money(grand_cost)}</div><div class=\"l\">Total cost</div></div>"
        f"<div class=\"kpi\"><div class=\"v\">{int(grand['total_tokens']):,}</div><div class=\"l\">Total tokens</div></div>"
        f"<div class=\"kpi\"><div class=\"v\">{grand['prompts']:,}</div><div class=\"l\">Prompts</div></div>"
        f"<div class=\"kpi\"><div class=\"v\">{len(by_host)}</div><div class=\"l\">Hosts</div></div>"
        "</div>"
        "<h2>Cost by origin &times; model</h2>"
        "<p class=\"sub\">native = direct codex/claude CLI sessions; omp = Oh My Pi harness sessions.</p>"
        + table(["Origin + Model", "Cost", "Share", "Prompts", "Tokens", "Cache-read"], om_rows)
        + "<h2>Cost by host</h2>"
        + table(["Host", "Cost", "omp cost", "native cost", "Prompts", "Tokens"], host_rows)
        + "<h2>Cost over time</h2><p class=\"sub\">Bar width is sqrt-scaled so low-volume days stay visible; the dollar values are exact.</p><div>" + (cost_bars or "<p class='sub'>no dated rows</p>") + "</div>"
        + "<h2>Tokens over time</h2><p class=\"sub\">Bar width is sqrt-scaled; token counts are exact.</p><div>" + (tok_bars or "<p class='sub'>no dated rows</p>") + "</div>"
        + "</body></html>\n"
    )



def main() -> int:
    args = parse_args()
    data = build_hierarchy(args)
    markdown_path = Path(args.markdown_out)
    json_path = Path(args.json_out)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(data, args), encoding="utf-8")
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    html_path = Path(args.html_out)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(data, args), encoding="utf-8")
    print(f"HTML written: {html_path}")
    print(f"Markdown written: {markdown_path}")
    print(f"JSON written: {json_path}")
    print(f"Scenarios: {len(data['scenarios'])}")
    print(f"Total cost: {money(data['total']['cost_usd'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
