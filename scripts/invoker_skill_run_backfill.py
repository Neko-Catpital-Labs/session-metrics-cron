#!/usr/bin/env python3
"""Backfill invoker-plan-to-invoker timing breakdowns from real session history.

Scans Claude Code (~/.claude/projects) and Codex (~/.codex/sessions,
~/.omp/agent/sessions) transcripts for genuine skill runs -- a real start-pattern
match paired with a real tool-level write to the final Invoker YAML plan, not
just an incidental text mention -- and writes a full timing breakdown for each
one plus an aggregate index. This is a manual, ad-hoc sweep: run it whenever you
want an updated picture, it is not wired into any cron.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

import invoker_skill_run_timing_report as timing

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "reports" / "invoker-skill-run-timing"


def discover_session_files(claude_dir: Path, codex_dirs: list[Path]) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    if claude_dir.is_dir():
        files.extend(("claude", p) for p in claude_dir.glob("**/*.jsonl"))
    for codex_dir in codex_dirs:
        if codex_dir.is_dir():
            files.extend(("codex-declared", p) for p in codex_dir.glob("**/*.jsonl"))
    return files


def find_real_run(session: timing.ParsedSession, args: Any) -> tuple[timing.Boundary, timing.Boundary] | None:
    pattern = re.compile(args.start_pattern, re.IGNORECASE) if args.start_pattern else timing.START_RE
    starts = timing.find_start_candidates(session, pattern)
    ends = timing.find_end_candidates(session, args.end_path_glob)
    if not starts or not ends:
        return None
    start = starts[0]
    end_tu = ends[-1]
    start_boundary = timing.Boundary(
        "start", start.line_no, start.uuid, start.ts, "start_pattern", {"text_preview": timing._text_preview(start.text)}
    )
    matched_path = next((fp for fp in timing._candidate_file_paths(end_tu) if _fnmatch(fp, args.end_path_glob)), "")
    end_boundary = timing._boundary_from_tool_use(end_tu, session, "last_yaml_write", file_path=matched_path)
    return start_boundary, end_boundary


def _fnmatch(path: str, glob: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(path, glob)


def backfill_one(
    provider_hint: str, session_path: Path, script_names: set[str], args: Any
) -> dict[str, Any] | None:
    try:
        session = timing.parse_session(session_path)
    except (OSError, UnicodeDecodeError):
        return None
    match = find_real_run(session, args)
    if match is None:
        return None
    start, end = match
    result = timing.build_result(session_path, session, start, end, script_names)
    return result


def write_run(result: dict[str, Any], out_dir: Path) -> Path:
    provider = result["session"]["provider"]
    stem = Path(result["session"]["path"]).stem
    out_path = out_dir / f"{provider}-{stem}.json"
    out_path.write_text(json.dumps(result, indent=2) + "\n")
    return out_path


def index_row(result: dict[str, Any], out_path: Path) -> dict[str, Any]:
    return {
        "session_path": result["session"]["path"],
        "provider": result["session"]["provider"],
        "detail_json": str(out_path),
        "start_timestamp": result["boundaries"]["start"]["timestamp"],
        "total_span_seconds": result["boundaries"]["total_span_seconds"],
        "model_thinking_seconds": result["totals"]["model_thinking_seconds"],
        "tool_execution_seconds": result["totals"]["tool_execution_seconds"],
        "tool_execution_pct": result["totals"]["tool_execution_pct"],
        "end_marker_found": result["boundaries"]["end"].get("end_marker_found", False),
        "anomaly_count": len(result["anomalies"]),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill invoker-plan-to-invoker timing reports from real session history.")
    parser.add_argument("--claude-dir", default=str(timing.DEFAULT_CLAUDE_DIR))
    parser.add_argument("--codex-dir", action="append", default=None, help="repeatable; default: both known Codex dirs")
    parser.add_argument("--skill-scripts-dir", help="Override; default: search both Claude and Codex skill installs")
    parser.add_argument("--start-pattern")
    parser.add_argument("--end-path-glob", default="*plans/*.yaml")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--index-out", help="default: <out-dir>/backfill-index.csv")
    parser.add_argument("--limit", type=int, help="stop after scanning this many files (debugging/safety valve)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    claude_dir = Path(args.claude_dir).expanduser()
    codex_dirs = [Path(d).expanduser() for d in args.codex_dir] if args.codex_dir else list(timing.DEFAULT_CODEX_SESSIONS_DIRS)
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    index_out = Path(args.index_out).expanduser() if args.index_out else out_dir / "backfill-index.csv"

    if args.skill_scripts_dir:
        script_names = timing.discover_skill_scripts([Path(args.skill_scripts_dir).expanduser()])
    else:
        script_names = timing.discover_skill_scripts([timing.DEFAULT_SKILL_SCRIPTS_DIR, timing.DEFAULT_CODEX_SKILL_SCRIPTS_DIR])

    files = discover_session_files(claude_dir, codex_dirs)
    if args.limit:
        files = files[: args.limit]

    rows: list[dict[str, Any]] = []
    scanned = 0
    for _hint, session_path in files:
        scanned += 1
        result = backfill_one(_hint, session_path, script_names, args)
        if result is None:
            continue
        out_path = write_run(result, out_dir)
        rows.append(index_row(result, out_path))

    rows.sort(key=lambda r: r["start_timestamp"] or "")
    fieldnames = [
        "session_path",
        "provider",
        "detail_json",
        "start_timestamp",
        "total_span_seconds",
        "model_thinking_seconds",
        "tool_execution_seconds",
        "tool_execution_pct",
        "end_marker_found",
        "anomaly_count",
    ]
    with index_out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    by_provider: dict[str, int] = {}
    for row in rows:
        by_provider[row["provider"]] = by_provider.get(row["provider"], 0) + 1

    print(f"Scanned {scanned} session files across {len(files)} candidates.")
    print(f"Found {len(rows)} real invoker-plan-to-invoker runs: {by_provider}")
    print(f"Index written to {index_out}")
    print(f"Per-run detail JSON written under {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
