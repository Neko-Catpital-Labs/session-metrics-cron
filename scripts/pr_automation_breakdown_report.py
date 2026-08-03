#!/usr/bin/env python3
"""Break down PR automation repair cost by category and retry-thrash target.

Reads the prompt-level CSV produced by scripts/planning_vs_execution_report.py
(reports/planning-vs-execution-prompts.csv) and writes Markdown, JSON, and HTML
reports. Prompt rows that reference a GitHub PR are grouped into "retry-thrash"
targets -- one per (repo, pr_number) pair -- so repeat-repair PRs (for example
a PR that needed 48 automated repair sessions) are easy to spot.

Each thrash target's JSON entry also carries an attempts_detail list
identifying the individual sessions behind it: session_id (the session's
JSONL filename without its extension or directory, i.e. Path(file).stem),
prompt_index (the original CSV value), cost_usd, session_date, and host. That
(session_id, prompt_index) pair is exactly what the existing
/api/cost-explorer-window endpoint in scripts/splitter_metric_tree_app.py
accepts, so a later workflow can make the HTML report clickable down to full
session detail without this script needing its own session-detail store.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO_ROOT / "reports" / "planning-vs-execution-prompts.csv"
DEFAULT_MARKDOWN_OUT = REPO_ROOT / "reports" / "pr-automation-breakdown.md"
DEFAULT_JSON_OUT = REPO_ROOT / "reports" / "pr-automation-breakdown.json"
DEFAULT_HTML_OUT = REPO_ROOT / "reports" / "pr-automation-breakdown.html"
DEFAULT_TOP_THRASH = 25
DEFAULT_DURATION_CAP_SECONDS = 3600.0

# Row fields searched (first match wins) for a GitHub PR reference.
PR_REF_TEXT_FIELDS = (
    "target",
    "session_cwd",
    "prompt_preview",
    "first_prompt_preview",
    "previous_prompt_preview",
    "final_answer_preview",
)

PR_REF_PATTERNS = (
    re.compile(r"https?://github\.com/(?P<repo>[\w.-]+/[\w.-]+?)/pull/(?P<pr>\d+)", re.IGNORECASE),
    re.compile(r"\b(?P<repo>[\w.-]+/[\w.-]+?)#(?P<pr>\d+)\b"),
)

# Category label columns, in priority order. Falls back to a PR-automation /
# uncategorized bucket when none of these are populated on a row.
CATEGORY_FIELDS = ("task_type_label", "task_type", "request_pattern_path", "request_pattern", "bucket")

COST_FIELDS = ("derived_total_cost_usd", "estimated_cost_usd", "cost_usd")
DURATION_FIELDS = ("elapsed_seconds", "duration_seconds", "active_seconds")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Break down PR automation repair cost and retry thrash.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Prompt-level CSV to read.")
    parser.add_argument("--markdown-out", default=str(DEFAULT_MARKDOWN_OUT))
    parser.add_argument("--json-out", default=str(DEFAULT_JSON_OUT))
    parser.add_argument("--html-out", default=str(DEFAULT_HTML_OUT))
    parser.add_argument(
        "--top-thrash",
        type=int,
        default=DEFAULT_TOP_THRASH,
        help="Number of retry-thrash targets kept in thrash.top[] and the Markdown/HTML output.",
    )
    parser.add_argument(
        "--duration-cap-seconds",
        type=float,
        default=DEFAULT_DURATION_CAP_SECONDS,
        help="Per-row cap applied when summing active_seconds; raw_seconds always sums the uncapped duration.",
    )
    return parser.parse_args(argv)


def to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def to_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def money(value: float) -> str:
    return f"${value:,.2f}"


def pct(value: float, total: float) -> str:
    return f"{(value / total * 100.0) if total else 0.0:.1f}%"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def row_cost_usd(row: dict[str, Any]) -> float:
    for key in COST_FIELDS:
        if row.get(key) not in (None, ""):
            return to_float(row.get(key))
    return 0.0


def row_raw_seconds(row: dict[str, Any]) -> float:
    for key in DURATION_FIELDS:
        if row.get(key) not in (None, ""):
            return to_float(row.get(key))
    return 0.0


def extract_repo_and_pr(row: dict[str, Any]) -> tuple[str, int] | None:
    """Return the (repo, pr_number) a prompt row references, if any."""
    text = " ".join(str(row.get(field) or "") for field in PR_REF_TEXT_FIELDS)
    for pattern in PR_REF_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group("repo"), to_int(match.group("pr"))
    return None


def category_for_row(row: dict[str, Any]) -> str:
    for key in CATEGORY_FIELDS:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "PR automation" if extract_repo_and_pr(row) is not None else "Uncategorized"


def build_window(rows: list[dict[str, str]], duration_cap_seconds: float = DEFAULT_DURATION_CAP_SECONDS) -> dict[str, Any]:
    """Build the category and retry-thrash breakdown for a set of prompt CSV rows."""
    total_cost = 0.0
    categories: dict[str, dict[str, Any]] = {}
    # (repo, pr_number) -> session_id -> per-session record (cost_usd, session_date,
    # host, prompt_index of the highest-cost row seen so far for that session).
    thrash_groups: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}

    for row in rows:
        cost = row_cost_usd(row)
        raw_seconds = row_raw_seconds(row)
        active_seconds = min(raw_seconds, duration_cap_seconds) if duration_cap_seconds > 0 else raw_seconds
        total_cost += cost

        category_name = category_for_row(row)
        fact = categories.setdefault(
            category_name,
            {"category": category_name, "cost_usd": 0.0, "active_seconds": 0.0, "prompts": 0, "raw_seconds": 0.0},
        )
        fact["cost_usd"] += cost
        fact["active_seconds"] += active_seconds
        fact["prompts"] += 1
        fact["raw_seconds"] += raw_seconds

        hit = extract_repo_and_pr(row)
        if hit is None:
            continue
        sessions = thrash_groups.setdefault(hit, {})
        session_id = Path(str(row.get("file", ""))).stem
        session = sessions.get(session_id)
        if session is None:
            session = {
                "cost_usd": 0.0,
                "session_date": row.get("session_date", ""),
                "host": row.get("host", ""),
                "prompt_index": row.get("prompt_index", ""),
                "_best_cost": float("-inf"),
            }
            sessions[session_id] = session
        session["cost_usd"] += cost
        if cost > session["_best_cost"]:
            session["_best_cost"] = cost
            session["prompt_index"] = row.get("prompt_index", "")

    category_rows = sorted(
        (
            {
                "category": fact["category"],
                "cost_usd": round(fact["cost_usd"], 4),
                "active_seconds": round(fact["active_seconds"], 4),
                "prompts": fact["prompts"],
                "raw_seconds": round(fact["raw_seconds"], 4),
            }
            for fact in categories.values()
        ),
        key=lambda item: (-item["cost_usd"], item["category"]),
    )

    thrash_targets = []
    for (repo, pr_number), sessions in thrash_groups.items():
        attempts_detail = sorted(
            (
                {
                    "session_id": session_id,
                    "prompt_index": session["prompt_index"],
                    "cost_usd": round(session["cost_usd"], 4),
                    "session_date": session["session_date"],
                    "host": session["host"],
                }
                for session_id, session in sessions.items()
            ),
            key=lambda item: item["cost_usd"],
            reverse=True,
        )
        thrash_targets.append(
            {
                "repo": repo,
                "pr_number": pr_number,
                "attempts": len(sessions),
                "cost_usd": round(sum(to_float(session.get("cost_usd")) for session in sessions.values()), 4),
                "attempts_detail": attempts_detail,
            }
        )
    thrash_targets.sort(key=lambda item: (-item["cost_usd"], -item["attempts"], item["repo"], item["pr_number"]))

    thrash_cost_usd = round(sum(item["cost_usd"] for item in thrash_targets if item["attempts"] > 1), 4)
    normal_repair_cost_usd = round(sum(item["cost_usd"] for item in thrash_targets if item["attempts"] <= 1), 4)

    return {
        "total_cost_usd": round(total_cost, 4),
        "categories": category_rows,
        "thrash_targets": thrash_targets,
        "thrash": {
            "thrash_cost_usd": thrash_cost_usd,
            "normal_repair_cost_usd": normal_repair_cost_usd,
            "top": thrash_targets,
        },
    }


def build_report(rows: list[dict[str, str]], args: argparse.Namespace) -> dict[str, Any]:
    data = build_window(rows, duration_cap_seconds=to_float(getattr(args, "duration_cap_seconds", DEFAULT_DURATION_CAP_SECONDS)))
    top_thrash = max(0, to_int(getattr(args, "top_thrash", DEFAULT_TOP_THRASH)))
    # thrash_targets[] stays the full, uncapped list of every distinct (repo,
    # pr_number) target; only thrash.top[] (which render_markdown() reads) is
    # sliced to --top-thrash, same as before this change.
    data["thrash"] = {**data["thrash"], "top": data["thrash_targets"][:top_thrash]}
    return data


def render_markdown(data: dict[str, Any], args: argparse.Namespace) -> str:
    total_cost = to_float(data.get("total_cost_usd"))
    thrash = data.get("thrash", {})
    top_thrash = to_int(getattr(args, "top_thrash", DEFAULT_TOP_THRASH))
    lines = [
        "# PR Automation Breakdown",
        "",
        f"- Total cost: **{money(total_cost)}**",
        f"- Retry-thrash cost: **{money(to_float(thrash.get('thrash_cost_usd')))}**",
        f"- Normal PR repair cost: **{money(to_float(thrash.get('normal_repair_cost_usd')))}**",
        "",
        "## Categories",
        "",
        "| Category | Cost | Share | Prompts | Active seconds |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in data.get("categories", []):
        lines.append(
            f"| {row.get('category', '')} | {money(to_float(row.get('cost_usd')))} | "
            f"{pct(to_float(row.get('cost_usd')), total_cost)} | {to_int(row.get('prompts')):,} | "
            f"{to_float(row.get('active_seconds')):,.0f} |"
        )
    lines.extend(
        [
            "",
            f"## Top Retry-Thrash Targets (top {top_thrash})",
            "",
            "| Target | Attempts | Cost | Share |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in thrash.get("top", []):
        repo = row.get("repo") or "unknown"
        pr_number = row.get("pr_number")
        lines.append(
            f"| `{repo}#{pr_number}` | {to_int(row.get('attempts')):,} | "
            f"{money(to_float(row.get('cost_usd')))} | {pct(to_float(row.get('cost_usd')), total_cost)} |"
        )
    lines.extend(["", "---", "", "Generated by `scripts/pr_automation_breakdown_report.py`.", ""])
    return "\n".join(lines)


def render_html(data: dict[str, Any], args: argparse.Namespace) -> str:
    markdown = render_markdown(data, args)
    body = "<br>\n".join(html.escape(line) for line in markdown.splitlines())
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>PR Automation Breakdown</title>"
        "<style>body{font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,sans-serif;"
        "max-width:980px;margin:2rem auto;padding:0 1rem;color:#1f2328}code{background:#f6f8fa;"
        "padding:.1rem .25rem;border-radius:4px}</style></head><body><pre>" + body + "</pre></body></html>\n"
    )


def write_outputs(data: dict[str, Any], args: argparse.Namespace) -> None:
    markdown_path = Path(args.markdown_out)
    json_path = Path(args.json_out)
    html_path = Path(args.html_out)
    for path in (markdown_path, json_path, html_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(data, args), encoding="utf-8")
    json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_html(data, args), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = read_csv(Path(args.input))
    data = build_report(rows, args)
    write_outputs(data, args)
    print(f"Markdown written: {args.markdown_out}")
    print(f"JSON written: {args.json_out}")
    print(f"HTML written: {args.html_out}")
    print(f"Thrash targets: {len(data['thrash_targets'])}")
    print(f"Total cost: {money(to_float(data.get('total_cost_usd')))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
