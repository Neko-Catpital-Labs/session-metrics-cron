#!/usr/bin/env python3
"""Generate a PR automation repair/thrash breakdown from prompt rows.

The report is intentionally small and data-oriented. It consumes the local
planning/cost CSV rows emitted by the analytics scripts and produces JSON,
Markdown, and HTML summaries centered on PR repair automation. The JSON is the
schema consumers should use; Markdown/HTML are static read-only summaries.
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

THRASH_CAUSES = {
    "failure diagnosis thrash",
    "ci/merge monitoring thrash",
    "repeated repair/test loops",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Break down PR automation repair cost and retry thrash.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Input CSV of prompt or classified command rows.")
    parser.add_argument("--markdown-out", default=str(DEFAULT_MARKDOWN_OUT))
    parser.add_argument("--json-out", default=str(DEFAULT_JSON_OUT))
    parser.add_argument("--html-out", default=str(DEFAULT_HTML_OUT))
    parser.add_argument("--top-thrash", type=int, default=25, help="Number of thrash targets to show in Markdown/HTML.")
    parser.add_argument(
        "--duration-cap-seconds",
        type=float,
        default=3600.0,
        help="Maximum per-row duration counted as active time. Raw time remains uncapped.",
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


def compact(text: str, limit: int = 180) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def row_cost(row: dict[str, Any]) -> float:
    for key in (
        "cost_usd",
        "total_cost_usd",
        "derived_total_cost_usd",
        "estimated_cost_usd",
        "allocated_total_cost_usd",
        "prompt_derived_total_cost_usd",
    ):
        if row.get(key) not in (None, ""):
            return to_float(row.get(key))
    return 0.0


def row_raw_seconds(row: dict[str, Any]) -> float:
    for key in (
        "duration_seconds",
        "raw_seconds",
        "elapsed_seconds",
        "wall_seconds",
        "prompt_duration_seconds",
        "active_seconds",
    ):
        if row.get(key) not in (None, ""):
            return to_float(row.get(key))
    return 0.0


def row_duration_seconds(row: dict[str, Any]) -> float:
    return row_raw_seconds(row)


def capped_duration(row: dict[str, Any], duration_cap_seconds: float) -> float:
    seconds = row_raw_seconds(row)
    if duration_cap_seconds <= 0:
        return seconds
    return min(seconds, duration_cap_seconds)


def session_id_for_file(file_path: str) -> str:
    return Path(file_path or "").stem


def extract_repo_and_pr(row_or_text: dict[str, Any] | str) -> tuple[str, int] | None:
    """Extract a repository and PR number from common GitHub PR references."""
    if isinstance(row_or_text, dict):
        explicit_pr = str(row_or_text.get("pr_number") or row_or_text.get("pull_request_number") or "").strip()
        explicit_repo = str(row_or_text.get("repo") or row_or_text.get("repository") or "").strip()
        if explicit_pr and to_int(explicit_pr.lstrip("#")):
            return explicit_repo or "unknown", to_int(explicit_pr.lstrip("#"))
        text = " ".join(
            str(row_or_text.get(key) or "")
            for key in (
                "target",
                "prompt_preview",
                "first_prompt_preview",
                "previous_prompt_preview",
                "final_answer_preview",
                "command_preview",
                "stdin_preview",
                "terminal_context_parent_command_preview",
                "request_pattern",
                "request_pattern_path",
                "task_label",
                "short_title",
            )
        )
    else:
        text = str(row_or_text or "")

    patterns = (
        r"https?://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<pr>\d+)",
        r"\bgh\s+pr\s+(?:view|checkout|checks|diff|comment|edit|merge|ready|close|reopen)\s+(?P<pr>\d+)\b(?:.*?\bin\s+(?P<repo>[\w.-]+/[\w.-]+))?",
        r"\b(?P<repo>[\w.-]+/[\w.-]+)#(?P<pr>\d+)\b",
        r"\bPR\s*#?(?P<pr>\d+)\b(?:\s+(?:in|for|on)\s+(?P<repo>[\w.-]+/[\w.-]+))?",
        r"\bpull\s+request\s+#?(?P<pr>\d+)\b(?:\s+(?:in|for|on)\s+(?P<repo>[\w.-]+/[\w.-]+))?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            repo = (match.groupdict().get("repo") or "unknown").strip()
            pr = (match.groupdict().get("pr") or "").strip()
            if pr:
                return repo, to_int(pr)
    return None


def fixing_cause(row: dict[str, Any]) -> str:
    explicit = str(row.get("fixing_cause") or row.get("dominant_fixing_cause") or "").strip()
    if explicit:
        return explicit
    phase = str(row.get("workflow_phase") or "").strip()
    efficiency = str(row.get("efficiency_label") or "").strip()
    if phase == "failure_diagnosis" and efficiency == "thrash":
        return "Failure diagnosis thrash"
    if phase == "ci_merge_monitoring" and efficiency == "thrash":
        return "CI/merge monitoring thrash"
    if phase == "repair_loop":
        return "Repeated repair/test loops"
    if phase == "failure_diagnosis" and efficiency == "expected_overhead":
        return "Expected failure investigation overhead"
    if phase == "orientation":
        return "Orientation in service of fixing"
    return ""


def category_for_row(row: dict[str, Any], task_categorizer: Any = None, pattern_categorizer: Any = None) -> str:
    cause = fixing_cause(row)
    if cause:
        return cause
    if task_categorizer is not None and hasattr(task_categorizer, "classify"):
        classified = task_categorizer.classify(row)
        value = str(
            getattr(classified, "task_type_label", "")
            or getattr(classified, "task_type", "")
            or (classified.get("task_type_label", "") if isinstance(classified, dict) else "")
            or (classified.get("task_type", "") if isinstance(classified, dict) else "")
        ).strip()
        if value:
            return value
    if pattern_categorizer is not None and hasattr(pattern_categorizer, "classify"):
        classified = pattern_categorizer.classify(row)
        value = str(
            getattr(classified, "request_pattern_path", "")
            or getattr(classified, "request_pattern", "")
            or (classified.get("request_pattern_path", "") if isinstance(classified, dict) else "")
            or (classified.get("request_pattern", "") if isinstance(classified, dict) else "")
        ).strip()
        if value:
            return value
    for key in ("category", "task_type_label", "task_type", "request_pattern_path", "request_pattern"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    if extract_repo_and_pr(row):
        return "PR automation"
    return "Uncategorized"


def is_thrash_row(row: dict[str, Any]) -> bool:
    cause = fixing_cause(row).lower()
    if cause in THRASH_CAUSES:
        return True
    phase = str(row.get("workflow_phase") or "").strip()
    efficiency = str(row.get("efficiency_label") or "").strip()
    if efficiency == "thrash":
        return True
    text = " ".join(
        str(row.get(key) or "")
        for key in ("category", "task_type_label", "task_type", "request_pattern_path", "request_pattern")
    ).lower()
    return "thrash" in text or "retry" in text or "repair/test loop" in text


def _empty_category(name: str) -> dict[str, Any]:
    return {
        "category": name,
        "cost_usd": 0.0,
        "active_seconds": 0.0,
        "prompts": 0,
        "raw_seconds": 0.0,
    }


def _round_category(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "cost_usd": round(to_float(row.get("cost_usd")), 4),
        "active_seconds": round(to_float(row.get("active_seconds")), 4),
        "prompts": to_int(row.get("prompts")),
        "raw_seconds": round(to_float(row.get("raw_seconds")), 4),
    }


def _attempts_detail(sessions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    details = []
    for session_id, session in sessions.items():
        details.append(
            {
                "session_id": session_id,
                "prompt_index": session.get("prompt_index", ""),
                "cost_usd": round(to_float(session.get("cost_usd")), 4),
                "session_date": session.get("session_date", ""),
                "host": session.get("host", ""),
            }
        )
    return sorted(details, key=lambda item: (-to_float(item.get("cost_usd")), str(item.get("session_id") or "")))


def build_window(
    rows: list[dict[str, str]],
    task_categorizer: Any = None,
    pattern_categorizer: Any = None,
    args: argparse.Namespace | None = None,
    *,
    duration_cap_seconds: float | None = None,
    **_ignored: Any,
) -> dict[str, Any]:
    """Build the report data for a collection of CSV rows."""
    if duration_cap_seconds is None:
        duration_cap_seconds = to_float(getattr(args, "duration_cap_seconds", 3600.0))
    if isinstance(task_categorizer, argparse.Namespace) and args is None:
        args = task_categorizer
        task_categorizer = None
        duration_cap_seconds = to_float(getattr(args, "duration_cap_seconds", duration_cap_seconds))

    total_cost = 0.0
    categories: dict[str, dict[str, Any]] = {}
    thrash_cost = 0.0
    normal_repair_cost = 0.0
    thrash_groups: dict[tuple[str, int], dict[str, Any]] = {}

    for row in rows:
        cost = row_cost(row)
        raw_seconds = row_raw_seconds(row)
        active_seconds = capped_duration(row, duration_cap_seconds)
        total_cost += cost

        category_name = category_for_row(row, task_categorizer, pattern_categorizer)
        category = categories.setdefault(category_name, _empty_category(category_name))
        category["cost_usd"] += cost
        category["active_seconds"] += active_seconds
        category["prompts"] += 1
        category["raw_seconds"] += raw_seconds

        hit = extract_repo_and_pr(row)
        if is_thrash_row(row):
            thrash_cost += cost
            if hit:
                group = thrash_groups.setdefault(
                    hit,
                    {
                        "repo": hit[0],
                        "pr_number": hit[1],
                        "cost_usd": 0.0,
                        "sessions": {},
                    },
                )
                group["cost_usd"] += cost
                session_id = session_id_for_file(row.get("file", ""))
                sessions = group["sessions"]
                session = sessions.setdefault(
                    session_id,
                    {
                        "cost_usd": 0.0,
                        "session_date": row.get("session_date", ""),
                        "host": row.get("host", ""),
                        "prompt_index": row.get("prompt_index", ""),
                        "_best_row_cost": float("-inf"),
                    },
                )
                session["cost_usd"] += cost
                if cost > to_float(session.get("_best_row_cost")):
                    session["_best_row_cost"] = cost
                    session["prompt_index"] = row.get("prompt_index", "")
        elif hit:
            normal_repair_cost += cost

    category_rows = sorted(
        (_round_category(row) for row in categories.values()),
        key=lambda item: (-to_float(item.get("cost_usd")), str(item.get("category") or "")),
    )

    thrash_rows = []
    for (repo, pr_number), group in thrash_groups.items():
        detail = _attempts_detail(group["sessions"])
        thrash_rows.append(
            {
                "repo": repo,
                "pr_number": pr_number,
                "cost_usd": round(to_float(group.get("cost_usd")), 4),
                "attempts": len(group["sessions"]),
                "attempts_detail": detail,
            }
        )
    thrash_rows.sort(key=lambda item: (-to_float(item.get("cost_usd")), -to_int(item.get("attempts")), str(item.get("repo")), str(item.get("pr_number"))))

    return {
        "total_cost_usd": round(total_cost, 4),
        "categories": category_rows,
        "thrash_targets": thrash_rows,
        "thrash": {
            "thrash_cost_usd": round(thrash_cost, 4),
            "normal_repair_cost_usd": round(normal_repair_cost, 4),
            "top": thrash_rows,
        },
    }


def build_report(
    rows: list[dict[str, str]],
    args: argparse.Namespace | None = None,
    task_categorizer: Any = None,
    pattern_categorizer: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if args is None:
        args = argparse.Namespace(top_thrash=25, duration_cap_seconds=3600.0)
    data = build_window(
        rows,
        task_categorizer=task_categorizer,
        pattern_categorizer=pattern_categorizer,
        args=args,
        duration_cap_seconds=to_float(getattr(args, "duration_cap_seconds", 3600.0)),
        **kwargs,
    )
    top_thrash = max(0, to_int(getattr(args, "top_thrash", 25)))
    full_thrash_targets = data["thrash_targets"]
    data["thrash"] = {**data["thrash"], "top": full_thrash_targets[:top_thrash]}
    data["thrash_targets"] = full_thrash_targets
    return data


def render_markdown(data: dict[str, Any], args: argparse.Namespace) -> str:
    total_cost = to_float(data.get("total_cost_usd"))
    lines = [
        "# PR Automation Breakdown",
        "",
        f"- Total cost: **{money(total_cost)}**",
        f"- Retry-thrash cost: **{money(to_float(data.get('thrash', {}).get('thrash_cost_usd')))}**",
        f"- Normal PR repair cost: **{money(to_float(data.get('thrash', {}).get('normal_repair_cost_usd')))}**",
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
    lines.extend(["", "## Top Retry-Thrash Targets", "", "| Target | Attempts | Cost | Share |", "|---|---:|---:|---:|"])
    for row in data.get("thrash", {}).get("top", []):
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
        "padding:.1rem .25rem;border-radius:4px}</style></head><body><pre>"
        + body
        + "</pre></body></html>\n"
    )


def write_outputs(data: dict[str, Any], args: argparse.Namespace) -> None:
    markdown_path = Path(args.markdown_out)
    json_path = Path(args.json_out)
    html_path = Path(args.html_out)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.parent.mkdir(parents=True, exist_ok=True)
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
