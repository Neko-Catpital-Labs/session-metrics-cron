#!/usr/bin/env python3
"""Break down PR automation repair cost and retry thrash.

Consumes the planning/cost prompt-row CSV emitted by
scripts/planning_vs_execution_report.py and produces JSON, Markdown, and
HTML reports of automated PR-repair cost, grouped by category and by the
(repo, PR) target being repaired.

"Retry thrash" is a (repo, PR) target that got repaired across more than
one session -- each such target's JSON entry carries an `attempts_detail`
list identifying the individual sessions behind its `attempts` count.
`session_id` (a JSONL filename stem, i.e. Path(file).stem) and
`prompt_index` are exactly the identifiers scripts/splitter_metric_tree_app.py's
/api/cost-explorer-window endpoint needs to load that session's detail.
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

THRASH_KEYWORDS = ("thrash", "retry", "repair/test loop", "repeated repair")

PR_TARGET_PATTERNS = (
    re.compile(r"https?://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<pr>\d+)", re.IGNORECASE),
    re.compile(r"\b(?P<repo>[\w.-]+/[\w.-]+)#(?P<pr>\d+)\b"),
    re.compile(r"\bPR\s*#?(?P<pr>\d+)\b(?:\s+(?:in|for|on)\s+(?P<repo>[\w.-]+/[\w.-]+))?", re.IGNORECASE),
)

PR_TARGET_TEXT_FIELDS = (
    "target",
    "pr_title",
    "short_title",
    "prompt_preview",
    "first_prompt_preview",
    "request_pattern_path",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Break down PR automation repair cost and retry thrash.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Input prompt-row CSV.")
    parser.add_argument("--markdown-out", default=str(DEFAULT_MARKDOWN_OUT))
    parser.add_argument("--json-out", default=str(DEFAULT_JSON_OUT))
    parser.add_argument("--html-out", default=str(DEFAULT_HTML_OUT))
    parser.add_argument(
        "--top-thrash", type=int, default=25, help="Number of retry-thrash targets shown in Markdown/HTML."
    )
    parser.add_argument(
        "--duration-cap-seconds",
        type=float,
        default=3600.0,
        help="Per-row cap applied to active_seconds. raw_seconds is always uncapped.",
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


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def row_cost_usd(row: dict[str, Any]) -> float:
    for key in ("allocated_total_cost_usd", "cost_usd", "derived_total_cost_usd", "estimated_cost_usd"):
        if row.get(key) not in (None, ""):
            return to_float(row.get(key))
    return 0.0


def row_raw_seconds(row: dict[str, Any]) -> float:
    for key in ("raw_seconds", "duration_seconds", "elapsed_seconds", "active_seconds"):
        if row.get(key) not in (None, ""):
            return to_float(row.get(key))
    return 0.0


def row_active_seconds(row: dict[str, Any], duration_cap_seconds: float) -> float:
    raw = row_raw_seconds(row)
    if duration_cap_seconds <= 0:
        return raw
    return min(raw, duration_cap_seconds)


def extract_repo_and_pr(row: dict[str, Any]) -> tuple[str, int] | None:
    """Extract a (repo, pr_number) target from a prompt row, if any."""
    explicit_pr = str(row.get("pr_number") or row.get("pull_request_number") or "").strip().lstrip("#")
    if explicit_pr and to_int(explicit_pr):
        explicit_repo = str(row.get("repo") or row.get("repository") or "").strip()
        return explicit_repo or "unknown", to_int(explicit_pr)

    text = " ".join(str(row.get(field) or "") for field in PR_TARGET_TEXT_FIELDS)
    for pattern in PR_TARGET_PATTERNS:
        match = pattern.search(text)
        if match:
            groups = match.groupdict()
            pr = (groups.get("pr") or "").strip()
            if pr:
                repo = (groups.get("repo") or "unknown").strip()
                return repo, to_int(pr)
    return None


def category_for_row(row: dict[str, Any]) -> str:
    for key in ("category", "fixing_cause", "task_type_label", "task_type", "request_pattern"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "PR automation" if extract_repo_and_pr(row) else "Uncategorized"


def is_thrash_row(row: dict[str, Any]) -> bool:
    if str(row.get("efficiency_label") or "").strip().lower() == "thrash":
        return True
    category = category_for_row(row).lower()
    return any(keyword in category for keyword in THRASH_KEYWORDS)


def build_attempts_detail(sessions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    detail = [
        {
            "session_id": session_id,
            "prompt_index": session["prompt_index"],
            "cost_usd": round(session["cost_usd"], 4),
            "session_date": session["session_date"],
            "host": session["host"],
        }
        for session_id, session in sessions.items()
    ]
    detail.sort(key=lambda item: item["cost_usd"], reverse=True)
    return detail


def build_window(rows: list[dict[str, str]], duration_cap_seconds: float = 3600.0) -> dict[str, Any]:
    """Aggregate prompt rows into category and retry-thrash summaries."""
    total_cost = 0.0
    thrash_cost = 0.0
    normal_repair_cost = 0.0
    categories: dict[str, dict[str, Any]] = {}
    # (repo, pr_number) -> session_id -> {cost_usd, session_date, host, prompt_index, best_cost}
    thrash_groups: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}

    for row in rows:
        cost = row_cost_usd(row)
        raw_seconds = row_raw_seconds(row)
        active_seconds = row_active_seconds(row, duration_cap_seconds)
        total_cost += cost

        category_name = category_for_row(row)
        category = categories.setdefault(
            category_name,
            {"category": category_name, "cost_usd": 0.0, "active_seconds": 0.0, "prompts": 0, "raw_seconds": 0.0},
        )
        category["cost_usd"] += cost
        category["active_seconds"] += active_seconds
        category["prompts"] += 1
        category["raw_seconds"] += raw_seconds

        target = extract_repo_and_pr(row)
        if target is None:
            continue
        if not is_thrash_row(row):
            normal_repair_cost += cost
            continue

        thrash_cost += cost
        sessions = thrash_groups.setdefault(target, {})
        session_id = Path(row.get("file", "")).stem
        session = sessions.get(session_id)
        if session is None:
            session = {
                "cost_usd": 0.0,
                "session_date": row.get("session_date", ""),
                "host": row.get("host", ""),
                "prompt_index": row.get("prompt_index", ""),
                "best_cost": float("-inf"),
            }
            sessions[session_id] = session
        session["cost_usd"] += cost
        if cost > session["best_cost"]:
            session["best_cost"] = cost
            session["prompt_index"] = row.get("prompt_index", "")

    category_rows = sorted(
        (
            {
                "category": entry["category"],
                "cost_usd": round(entry["cost_usd"], 4),
                "active_seconds": round(entry["active_seconds"], 4),
                "prompts": entry["prompts"],
                "raw_seconds": round(entry["raw_seconds"], 4),
            }
            for entry in categories.values()
        ),
        key=lambda item: (-item["cost_usd"], item["category"]),
    )

    thrash_targets = [
        {
            "repo": repo,
            "pr_number": pr_number,
            "cost_usd": round(sum(session["cost_usd"] for session in sessions.values()), 4),
            "attempts": len(sessions),
            "attempts_detail": build_attempts_detail(sessions),
        }
        for (repo, pr_number), sessions in thrash_groups.items()
    ]
    thrash_targets.sort(key=lambda item: (-item["cost_usd"], -item["attempts"], item["repo"], item["pr_number"]))

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
    rows = read_rows(Path(args.input))
    data = build_report(rows, args)
    write_outputs(data, args)
    print(f"Markdown written: {args.markdown_out}")
    print(f"JSON written: {args.json_out}")
    print(f"HTML written: {args.html_out}")
    print(f"Thrash targets: {len(data['thrash_targets'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
