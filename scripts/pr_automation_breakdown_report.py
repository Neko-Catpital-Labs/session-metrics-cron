#!/usr/bin/env python3
"""Build a PR automation cost breakdown from planning-vs-execution prompt rows."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]

AUTOMATED_REQUEST_ORIGINS = {
    "generated_invoker_task",
    "invoker_auto_fix",
    "invoker_task_failure_fix",
    "merge_failure_fix",
    "ci_failure_fix",
}
AUTOMATED_TASK_TYPES = {
    "workflow_repair",
}
AUTOMATED_TEXT_MARKERS = (
    "generated task for invoker",
    "invoker generated task",
    "autofix for invoker",
    "invoker autofix",
    "fix invoker task failure",
    "invoker task failed",
    "fix with agent",
)
DURATION_FIELDS = (
    "raw_seconds",
    "duration_seconds",
    "active_seconds",
    "elapsed_seconds",
    "wall_seconds",
    "seconds",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PR automation retry-thrash breakdown.")
    parser.add_argument("--input", default=str(REPO_ROOT / "reports" / "planning-vs-execution-prompts.csv"))
    parser.add_argument("--json-out", default=str(REPO_ROOT / "reports" / "pr-automation-breakdown.json"))
    parser.add_argument("--markdown-out", default=str(REPO_ROOT / "reports" / "pr-automation-breakdown.md"))
    parser.add_argument("--html-out", default=str(REPO_ROOT / "reports" / "pr-automation-breakdown.html"))
    parser.add_argument("--top-thrash", type=int, default=20)
    parser.add_argument("--duration-cap-seconds", type=float, default=3600.0)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def prompt_cost(row: dict[str, str]) -> float:
    return (
        to_float(row.get("derived_total_cost_usd"))
        or to_float(row.get("estimated_cost_usd"))
        or to_float(row.get("allocated_total_cost_usd"))
    )


def row_duration_seconds(row: dict[str, str]) -> float:
    for field in DURATION_FIELDS:
        if row.get(field) not in (None, ""):
            return to_float(row.get(field))
    if row.get("duration_ms") not in (None, ""):
        return to_float(row.get("duration_ms")) / 1000.0
    return 0.0


def compact(text: Any, limit: int = 120) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]


def money(value: float) -> str:
    return f"${value:,.2f}"


def pct(value: float, total: float) -> str:
    return f"{(value / total * 100.0) if total else 0.0:.1f}%"


def row_text(row: dict[str, str]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in (
            "repo",
            "repository",
            "session_cwd",
            "prompt_preview",
            "previous_prompt_preview",
            "first_prompt_preview",
            "final_answer_preview",
            "request_pattern",
            "task_type",
            "task_type_label",
            "request_origin",
            "primary_why",
            "work_motivation",
            "prompt_task_kind",
        )
    )


def is_automated_row(row: dict[str, str]) -> bool:
    request_origin = str(row.get("request_origin") or row.get("primary_why") or "").strip()
    task_type = str(row.get("task_type") or "").strip()
    if request_origin in AUTOMATED_REQUEST_ORIGINS or task_type in AUTOMATED_TASK_TYPES:
        return True
    lowered = row_text(row).lower()
    return any(marker in lowered for marker in AUTOMATED_TEXT_MARKERS)


def infer_repo(row: dict[str, str], text: str) -> str:
    for key in ("repo", "repository"):
        value = str(row.get(key) or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
            return value
    match = re.search(r"github\.com[:/]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    cwd = str(row.get("session_cwd") or "").rstrip("/")
    if cwd:
        return Path(cwd).name or "unknown"
    return "unknown"


def extract_repo_and_pr(row: dict[str, str]) -> tuple[str, int] | None:
    text = row_text(row)
    url_match = re.search(
        r"github\.com[:/]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:pull|pulls|issues)/(\d+)",
        text,
        re.IGNORECASE,
    )
    if url_match:
        return url_match.group(1), int(url_match.group(2))

    repo_pr_match = re.search(
        r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s*(?:#|(?:pull request|pr)\s*#?)\s*(\d+)\b",
        text,
        re.IGNORECASE,
    )
    if repo_pr_match:
        return repo_pr_match.group(1), int(repo_pr_match.group(2))

    pr_match = re.search(r"\b(?:pull request|pr)\s*#?\s*(\d+)\b", text, re.IGNORECASE)
    if pr_match:
        return infer_repo(row, text), int(pr_match.group(1))

    return None


def category_for_row(row: dict[str, str], hit: tuple[str, int] | None) -> tuple[str, str]:
    if is_automated_row(row) and hit:
        return "automated_pr_repair", "Automated PR repair"
    if is_automated_row(row):
        return "automated_other", "Automated other"
    return "other", "Other"


def add_category_metrics(
    categories: dict[str, dict[str, Any]],
    category: str,
    label: str,
    *,
    cost: float,
    active_seconds: float,
    raw_seconds: float,
) -> None:
    bucket = categories.setdefault(
        category,
        {
            "category": category,
            "label": label,
            "cost_usd": 0.0,
            "active_seconds": 0.0,
            "prompts": 0,
            "raw_seconds": 0.0,
        },
    )
    bucket["cost_usd"] += cost
    bucket["active_seconds"] += active_seconds
    bucket["prompts"] += 1
    bucket["raw_seconds"] += raw_seconds


def rounded_category(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["cost_usd"] = round(to_float(out["cost_usd"]), 4)
    out["active_seconds"] = round(to_float(out["active_seconds"]), 4)
    out["raw_seconds"] = round(to_float(out["raw_seconds"]), 4)
    return out


def build_attempts_detail(sessions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for session_id, session in sessions.items():
        rows.append(
            {
                "session_id": session_id,
                "prompt_index": session["prompt_index"],
                "cost_usd": round(to_float(session["cost_usd"]), 4),
                "session_date": session["session_date"],
                "host": session["host"],
            }
        )
    return sorted(rows, key=lambda item: (-item["cost_usd"], item["session_id"], str(item["prompt_index"])))


def build_window(rows: list[dict[str, str]], *, duration_cap_seconds: float = 3600.0) -> dict[str, Any]:
    total_cost = 0.0
    categories: dict[str, dict[str, Any]] = {}
    thrash_groups: dict[tuple[str, int], dict[str, Any]] = {}

    for row in rows:
        cost = prompt_cost(row)
        raw_seconds = row_duration_seconds(row)
        active_seconds = min(raw_seconds, duration_cap_seconds) if duration_cap_seconds and raw_seconds else raw_seconds
        total_cost += cost

        hit = extract_repo_and_pr(row)
        category, label = category_for_row(row, hit)
        add_category_metrics(
            categories,
            category,
            label,
            cost=cost,
            active_seconds=active_seconds,
            raw_seconds=raw_seconds,
        )
        if not hit or not is_automated_row(row):
            continue

        group = thrash_groups.setdefault(hit, {"cost_usd": 0.0, "sessions": {}})
        group["cost_usd"] += cost
        file_path = str(row.get("file") or "")
        session_id = Path(file_path).stem or file_path
        if not session_id:
            session_id = "unknown"
        sessions: dict[str, dict[str, Any]] = group["sessions"]
        session = sessions.setdefault(
            session_id,
            {
                "cost_usd": 0.0,
                "session_date": str(row.get("session_date") or ""),
                "host": str(row.get("host") or ""),
                "prompt_index": str(row.get("prompt_index") or ""),
                "representative_cost_usd": -1.0,
            },
        )
        session["cost_usd"] += cost
        if cost > to_float(session.get("representative_cost_usd")):
            session["representative_cost_usd"] = cost
            session["prompt_index"] = str(row.get("prompt_index") or "")

    thrash_rows: list[dict[str, Any]] = []
    for (repo, pr_number), group in thrash_groups.items():
        attempts_detail = build_attempts_detail(group["sessions"])
        thrash_rows.append(
            {
                "repo": repo,
                "pr_number": pr_number,
                "target": f"{repo}#{pr_number}",
                "attempts": len(attempts_detail),
                "cost_usd": round(to_float(group["cost_usd"]), 4),
                "attempts_detail": attempts_detail,
            }
        )
    thrash_rows.sort(key=lambda item: (-item["cost_usd"], -item["attempts"], item["repo"], item["pr_number"]))
    category_rows = sorted(
        (rounded_category(row) for row in categories.values()),
        key=lambda item: (-item["cost_usd"], item["category"]),
    )
    return {
        "total_cost_usd": round(total_cost, 4),
        "categories": category_rows,
        "thrash_targets": thrash_rows,
        "thrash_cost_usd": round(sum(row["cost_usd"] for row in thrash_rows if row["attempts"] > 1), 4),
        "normal_repair_cost_usd": round(sum(row["cost_usd"] for row in thrash_rows if row["attempts"] <= 1), 4),
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    rows = read_csv(Path(args.input))
    window = build_window(rows, duration_cap_seconds=args.duration_cap_seconds)
    top = window["thrash_targets"][: args.top_thrash]
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "input": str(Path(args.input)),
        "total_cost_usd": window["total_cost_usd"],
        "categories": window["categories"],
        "thrash": {
            "top": top,
            "thrash_cost_usd": window["thrash_cost_usd"],
            "normal_repair_cost_usd": window["normal_repair_cost_usd"],
        },
        "thrash_targets": window["thrash_targets"],
    }


def render_markdown(data: dict[str, Any], args: argparse.Namespace) -> str:
    lines = [
        "# PR Automation Breakdown",
        "",
        f"- Total cost: **{money(data['total_cost_usd'])}**",
        f"- Thrash cost: **{money(data['thrash']['thrash_cost_usd'])}**",
        f"- Normal repair cost: **{money(data['thrash']['normal_repair_cost_usd'])}**",
        "",
        "## Categories",
        "",
        "| Category | Prompts | Cost | Active seconds | Raw seconds |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in data["categories"]:
        lines.append(
            f"| {row['label']} | {row['prompts']:,} | {money(row['cost_usd'])} | "
            f"{row['active_seconds']:,.1f} | {row['raw_seconds']:,.1f} |"
        )
    lines.extend(
        [
            "",
            f"## Top PR Retry Targets (top {args.top_thrash})",
            "",
            "| Target | Attempts | Cost | Share |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in data["thrash"]["top"]:
        lines.append(
            f"| `{row['target']}` | {row['attempts']:,} | {money(row['cost_usd'])} | "
            f"{pct(row['cost_usd'], data['total_cost_usd'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_html(data: dict[str, Any], args: argparse.Namespace) -> str:
    body = html.escape(render_markdown(data, args))
    return f"<!doctype html><html><head><meta charset='utf-8'><title>PR Automation Breakdown</title></head><body><pre>{body}</pre></body></html>\n"


def main() -> int:
    args = parse_args()
    data = build_report(args)
    json_path = Path(args.json_out)
    markdown_path = Path(args.markdown_out)
    html_path = Path(args.html_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(data, args), encoding="utf-8")
    html_path.write_text(render_html(data, args), encoding="utf-8")
    print(f"JSON written: {json_path}")
    print(f"Markdown written: {markdown_path}")
    print(f"HTML written: {html_path}")
    print(f"Thrash targets: {len(data['thrash_targets'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
