#!/usr/bin/env python3
"""Break down PR automation repair cost and retry thrash.

Reads the local prompt-level CSV emitted by scripts/planning_vs_execution_report.py
(reports/planning-vs-execution-prompts.csv) and produces Markdown/JSON/HTML
summaries of PR automation repair cost, split into normal repair work and
"retry thrash" -- PRs that get repeatedly re-repaired.

The JSON output is the schema downstream tooling (Cost Explorer, the
interactive HTML report) should read from. Each retry-thrash target's
underlying sessions are exposed via attempts_detail so a session can be
looked up later against /api/cost-explorer-window in
scripts/splitter_metric_tree_app.py using its session_id (the session
JSONL file's stem) and prompt_index.
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
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"
DEFAULT_INPUT = DEFAULT_REPORTS_DIR / "planning-vs-execution-prompts.csv"
DEFAULT_MARKDOWN_OUT = DEFAULT_REPORTS_DIR / "pr-automation-breakdown.md"
DEFAULT_JSON_OUT = DEFAULT_REPORTS_DIR / "pr-automation-breakdown.json"
DEFAULT_HTML_OUT = DEFAULT_REPORTS_DIR / "pr-automation-breakdown.html"

THRASH_KEYWORDS = ("thrash", "retry", "repeated repair")

PR_TEXT_FIELDS = (
    "target",
    "prompt_preview",
    "first_prompt_preview",
    "previous_prompt_preview",
    "final_answer_preview",
    "request_pattern_path",
    "request_pattern",
)

PR_PATTERNS = (
    re.compile(r"https?://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<pr>\d+)", re.IGNORECASE),
    re.compile(r"\b(?P<repo>[\w.-]+/[\w.-]+)#(?P<pr>\d+)\b"),
    re.compile(r"\bPR\s*#?(?P<pr>\d+)\b(?:\s+(?:in|for|on)\s+(?P<repo>[\w.-]+/[\w.-]+))?", re.IGNORECASE),
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Break down PR automation repair cost and retry thrash.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Input prompt-level CSV.")
    parser.add_argument("--markdown-out", default=str(DEFAULT_MARKDOWN_OUT))
    parser.add_argument("--json-out", default=str(DEFAULT_JSON_OUT))
    parser.add_argument("--html-out", default=str(DEFAULT_HTML_OUT))
    parser.add_argument("--top-thrash", type=int, default=25, help="Number of thrash targets shown in the Markdown output.")
    parser.add_argument(
        "--duration-cap-seconds",
        type=float,
        default=3600.0,
        help="Per-row cap applied to active_seconds; raw_seconds is always uncapped.",
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
    for key in ("derived_total_cost_usd", "estimated_cost_usd", "cost_usd"):
        if row.get(key) not in (None, ""):
            return to_float(row.get(key))
    return 0.0


def row_raw_seconds(row: dict[str, Any]) -> float:
    for key in ("duration_seconds", "elapsed_seconds", "active_seconds", "raw_seconds"):
        if row.get(key) not in (None, ""):
            return to_float(row.get(key))
    return 0.0


def capped_active_seconds(row: dict[str, Any], duration_cap_seconds: float) -> float:
    seconds = row_raw_seconds(row)
    if duration_cap_seconds <= 0:
        return seconds
    return min(seconds, duration_cap_seconds)


def extract_repo_and_pr(row: dict[str, Any]) -> tuple[str, int] | None:
    """Extract a (repo, pr_number) target from a prompt row, if any."""
    explicit_pr = str(row.get("pr_number") or row.get("pull_request_number") or "").strip().lstrip("#")
    if explicit_pr and to_int(explicit_pr):
        explicit_repo = str(row.get("repo") or row.get("repository") or "").strip()
        return explicit_repo or "unknown", to_int(explicit_pr)

    text = " ".join(str(row.get(field) or "") for field in PR_TEXT_FIELDS)
    for pattern in PR_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groupdict()
        pr = (groups.get("pr") or "").strip()
        if pr:
            repo = (groups.get("repo") or "unknown").strip()
            return repo, to_int(pr)
    return None


def category_for_row(row: dict[str, Any]) -> str:
    for key in ("fixing_cause", "task_type_label", "task_type", "request_pattern_path", "request_pattern"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    if extract_repo_and_pr(row):
        return "PR automation"
    return "Uncategorized"


def is_thrash_row(row: dict[str, Any]) -> bool:
    if str(row.get("efficiency_label") or "").strip().lower() == "thrash":
        return True
    text = category_for_row(row).lower()
    return any(keyword in text for keyword in THRASH_KEYWORDS)


def _empty_category_row(name: str) -> dict[str, Any]:
    return {"category": name, "cost_usd": 0.0, "active_seconds": 0.0, "prompts": 0, "raw_seconds": 0.0}


def _finalize_category_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "category": row["category"],
        "cost_usd": round(to_float(row.get("cost_usd")), 4),
        "active_seconds": round(to_float(row.get("active_seconds")), 4),
        "prompts": to_int(row.get("prompts")),
        "raw_seconds": round(to_float(row.get("raw_seconds")), 4),
    }


def _attempts_detail(sessions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    detail = [
        {
            "session_id": session_id,
            "prompt_index": session.get("prompt_index", ""),
            "cost_usd": round(to_float(session.get("cost_usd")), 4),
            "session_date": session.get("session_date", ""),
            "host": session.get("host", ""),
        }
        for session_id, session in sessions.items()
    ]
    detail.sort(key=lambda item: to_float(item.get("cost_usd")), reverse=True)
    return detail


def build_window(rows: list[dict[str, str]], duration_cap_seconds: float = 3600.0) -> dict[str, Any]:
    """Build the report data for a collection of prompt CSV rows."""
    total_cost = 0.0
    thrash_cost = 0.0
    normal_repair_cost = 0.0
    categories: dict[str, dict[str, Any]] = {}
    # (repo, pr_number) -> session_id -> {cost_usd, session_date, host, prompt_index, _best_cost}
    thrash_groups: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}

    for row in rows:
        cost = row_cost_usd(row)
        raw_seconds = row_raw_seconds(row)
        active_seconds = capped_active_seconds(row, duration_cap_seconds)
        total_cost += cost

        category_name = category_for_row(row)
        category = categories.setdefault(category_name, _empty_category_row(category_name))
        category["cost_usd"] += cost
        category["active_seconds"] += active_seconds
        category["prompts"] += 1
        category["raw_seconds"] += raw_seconds

        hit = extract_repo_and_pr(row)
        if is_thrash_row(row):
            thrash_cost += cost
            if hit is not None:
                sessions = thrash_groups.setdefault(hit, {})
                session_id = Path(str(row.get("file", ""))).stem
                session = sessions.setdefault(
                    session_id,
                    {
                        "cost_usd": 0.0,
                        "session_date": row.get("session_date", ""),
                        "host": row.get("host", ""),
                        "prompt_index": row.get("prompt_index", ""),
                        "_best_cost": float("-inf"),
                    },
                )
                session["cost_usd"] += cost
                if cost > session["_best_cost"]:
                    session["_best_cost"] = cost
                    session["prompt_index"] = row.get("prompt_index", "")
        elif hit is not None:
            normal_repair_cost += cost

    category_rows = sorted(
        (_finalize_category_row(row) for row in categories.values()),
        key=lambda item: (-item["cost_usd"], item["category"]),
    )

    thrash_targets = [
        {
            "repo": repo,
            "pr_number": pr_number,
            "cost_usd": round(sum(to_float(s.get("cost_usd")) for s in sessions.values()), 4),
            "attempts": len(sessions),
            "attempts_detail": _attempts_detail(sessions),
        }
        for (repo, pr_number), sessions in thrash_groups.items()
    ]
    thrash_targets.sort(
        key=lambda item: (-to_float(item["cost_usd"]), -to_int(item["attempts"]), item["repo"], item["pr_number"])
    )

    return {
        "total_cost_usd": round(total_cost, 4),
        "categories": category_rows,
        "thrash_targets": thrash_targets,
        "thrash": {
            "thrash_cost_usd": round(thrash_cost, 4),
            "normal_repair_cost_usd": round(normal_repair_cost, 4),
            "top": thrash_targets,
        },
    }


def build_report(rows: list[dict[str, str]], args: argparse.Namespace) -> dict[str, Any]:
    data = build_window(rows, duration_cap_seconds=to_float(getattr(args, "duration_cap_seconds", 3600.0)))
    top_thrash = max(0, to_int(getattr(args, "top_thrash", 25)))
    data["thrash"] = {**data["thrash"], "top": data["thrash_targets"][:top_thrash]}
    return data


def render_markdown(data: dict[str, Any], args: argparse.Namespace) -> str:
    total_cost = to_float(data.get("total_cost_usd"))
    thrash = data.get("thrash", {})
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
            f"## Top Retry-Thrash Targets (top {to_int(getattr(args, 'top_thrash', 25))})",
            "",
            "| Target | Attempts | Cost | Share |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in thrash.get("top", []):
        repo = row.get("repo") or "unknown"
        pr_number = row.get("pr_number") or ""
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
