#!/usr/bin/env python3
"""Ad-hoc end-to-end timing breakdown for a single invoker-plan-to-invoker skill run.

Parses one Claude Code session transcript (JSONL), locates where the skill was
invoked and where the final Invoker YAML plan was written, and splits that span
into model-thinking time vs tool/script execution time, with a per-script
breakdown for Bash calls into the skill's own sub-scripts.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLAUDE_DIR = Path.home() / ".claude" / "projects"
DEFAULT_CODEX_SESSIONS_DIRS = [Path.home() / ".codex" / "sessions", Path.home() / ".omp" / "agent" / "sessions"]
DEFAULT_SKILL_SCRIPTS_DIR = Path.home() / ".claude" / "skills" / "invoker-plan-to-invoker" / "scripts"
DEFAULT_CODEX_SKILL_SCRIPTS_DIR = Path.home() / ".codex" / "skills" / "invoker-plan-to-invoker" / "scripts"
SCHEMA_VERSION = "invoker_skill_run_timing_v1"

# Codex (Responses API-style) transcripts use exec_command/shell/bash function calls
# instead of Claude's dedicated Bash tool; the command string lives under a different key.
CODEX_SHELL_TOOL_NAMES = {"exec_command", "shell", "bash", "local_shell"}
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add) File: (.+)$", re.MULTILINE)

START_PHRASES = [
    "invoker-plan-to-invoker",
    "/invoker-plan-to-invoker",
    "plan-to-invoker",
    "/plan-to-invoker",
    "submit to invoker",
    "create invoker plan",
    "convert to invoker",
]
START_RE = re.compile("|".join(re.escape(p) for p in START_PHRASES), re.IGNORECASE)

DEFAULT_SKILL_SCRIPTS = {
    "skill-doctor.sh",
    "extract-assumptions.sh",
    "validate-plan.sh",
    "validate-plan.mjs",
    "validate-plan.ts",
    "build-validator.sh",
    "check-coverage-map.sh",
    "check-policy-coverage.sh",
    "check-source-plan-coverage.sh",
    "check-source-plan-coverage.mjs",
    "check-stack-manifest.sh",
    "formula-doctor.sh",
    "generate-coverage-map-template.sh",
    "generate-stack-manifest-template.sh",
    "generate-verify-plan.sh",
    "lint-review-units.mjs",
    "lint-task-atomicity.sh",
    "parse-results.sh",
    "render-formula.sh",
    "render-formula.mjs",
}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]
    ts: datetime | None
    line_no: int
    assistant_uuid: str


@dataclass
class ToolResult:
    tool_use_id: str
    ts: datetime | None
    line_no: int


@dataclass
class AssistantEvent:
    line_no: int
    uuid: str
    ts: datetime | None
    text_preview: str
    tool_use_ids: list[str]


@dataclass
class UserTextEvent:
    line_no: int
    uuid: str
    ts: datetime | None
    text: str


@dataclass
class ParsedSession:
    tool_uses: dict[str, ToolUse]
    tool_results: dict[str, ToolResult]
    user_texts: list[UserTextEvent]
    assistant_events: list[AssistantEvent]
    malformed_lines: int
    total_lines: int
    provider: str = "claude"


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def _text_preview(text: str, limit: int = 160) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: max(0, limit - 3)].rstrip() + "..."


def _read_lines(path: Path) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    out: list[tuple[int, dict[str, Any]]] = []
    malformed = 0
    text = path.read_text(errors="ignore")
    for line_no, raw in enumerate(text.splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(obj, dict):
            out.append((line_no, obj))
        else:
            malformed += 1
    return out, malformed


def _parse_claude_session_lines(lines: list[tuple[int, dict[str, Any]]], malformed: int) -> ParsedSession:
    tool_uses: dict[str, ToolUse] = {}
    tool_results: dict[str, ToolResult] = {}
    user_texts: list[UserTextEvent] = []
    assistant_events: list[AssistantEvent] = []

    for line_no, obj in lines:
        ts = parse_ts(obj.get("timestamp"))
        typ = obj.get("type")
        msg = obj.get("message") or {}
        content = msg.get("content")
        uuid = str(obj.get("uuid") or "")

        if typ == "user" and msg.get("role") == "user" and isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    text_parts.append(item["text"])
                elif item.get("type") == "tool_result":
                    tuid = str(item.get("tool_use_id") or "")
                    if tuid and tuid not in tool_results:
                        tool_results[tuid] = ToolResult(tuid, ts, line_no)
            text = "\n".join(p for p in text_parts if p.strip())
            if text.strip():
                user_texts.append(UserTextEvent(line_no, uuid, ts, text))

        elif typ == "assistant" and msg.get("role") == "assistant" and isinstance(content, list):
            tool_ids: list[str] = []
            text_parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    tid = str(item.get("id") or "")
                    if tid and tid not in tool_uses:
                        tool_uses[tid] = ToolUse(
                            tid, str(item.get("name") or "unknown"), item.get("input") or {}, ts, line_no, uuid
                        )
                        tool_ids.append(tid)
                elif item.get("type") == "text" and isinstance(item.get("text"), str):
                    text_parts.append(item["text"])
            preview = _text_preview("\n".join(p for p in text_parts if p.strip()))
            assistant_events.append(AssistantEvent(line_no, uuid, ts, preview, tool_ids))

    return ParsedSession(tool_uses, tool_results, user_texts, assistant_events, malformed, len(lines), provider="claude")


def _codex_tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or payload.get("type") or "unknown")
    if name == "apply_patch" or "input" in payload:
        return {"patch": str(payload.get("input") or "")}
    arguments = payload.get("arguments")
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {"raw": arguments}
    return {}


def _parse_codex_session_lines(lines: list[tuple[int, dict[str, Any]]], malformed: int) -> ParsedSession:
    tool_uses: dict[str, ToolUse] = {}
    tool_results: dict[str, ToolResult] = {}
    user_texts: list[UserTextEvent] = []
    assistant_events: list[AssistantEvent] = []

    # Codex logs each parallel tool call as its own response_item line (unlike Claude,
    # which bundles same-turn tool_use blocks into one assistant message/timestamp), so
    # consecutive *_call lines with no intervening reasoning/message/user text between
    # them are buffered into a single batch and flushed as one AssistantEvent -- using
    # the first call's timestamp -- exactly mirroring how Claude's batching works. Without
    # this, an early call's own result can push the cursor past the next call's earlier
    # request timestamp and produce a spurious negative-duration segment.
    pending_batch_ids: list[str] = []
    pending_batch_line: int | None = None
    pending_batch_ts: datetime | None = None

    def flush_pending() -> None:
        nonlocal pending_batch_ids, pending_batch_line, pending_batch_ts
        if pending_batch_ids:
            assistant_events.append(
                AssistantEvent(pending_batch_line, f"codex-{pending_batch_line}", pending_batch_ts, "", list(pending_batch_ids))
            )
        pending_batch_ids = []
        pending_batch_line = None
        pending_batch_ts = None

    for line_no, obj in lines:
        ts = parse_ts(obj.get("timestamp"))
        typ = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        payload_type = str(payload.get("type") or "")

        if typ == "event_msg" and payload_type == "user_message":
            flush_pending()
            text = str(payload.get("message") or "")
            if text.strip():
                user_texts.append(UserTextEvent(line_no, f"codex-{line_no}", ts, text))
            continue

        if typ == "event_msg" and payload_type == "agent_message":
            flush_pending()
            text = str(payload.get("message") or "")
            assistant_events.append(AssistantEvent(line_no, f"codex-{line_no}", ts, _text_preview(text), []))
            continue

        if typ == "response_item" and payload_type == "reasoning":
            flush_pending()
            assistant_events.append(AssistantEvent(line_no, f"codex-{line_no}", ts, "", []))
            continue

        if typ == "response_item" and payload_type.endswith("_call"):
            call_id = str(payload.get("call_id") or f"codex-call-{line_no}")
            name = str(payload.get("name") or payload_type[: -len("_call")])
            tool_uses[call_id] = ToolUse(call_id, name, _codex_tool_input(payload), ts, line_no, f"codex-{line_no}")
            if not pending_batch_ids:
                pending_batch_line = line_no
                pending_batch_ts = ts
            pending_batch_ids.append(call_id)
            continue

        if typ == "response_item" and payload_type.endswith("_output"):
            call_id = str(payload.get("call_id") or "")
            if call_id and call_id not in tool_results:
                tool_results[call_id] = ToolResult(call_id, ts, line_no)
            continue

    flush_pending()
    return ParsedSession(tool_uses, tool_results, user_texts, assistant_events, malformed, len(lines), provider="codex")


def _detect_provider_from_lines(lines: list[tuple[int, dict[str, Any]]]) -> str:
    for _, obj in lines[:20]:
        typ = obj.get("type")
        if typ in ("response_item", "event_msg", "turn_context", "session_meta", "compacted"):
            return "codex"
        if typ in ("user", "assistant") and isinstance(obj.get("message"), dict):
            return "claude"
    return "claude"


def detect_provider(path: Path) -> str:
    lines, _ = _read_lines(path)
    return _detect_provider_from_lines(lines)


def parse_session(path: Path) -> ParsedSession:
    lines, malformed = _read_lines(path)
    provider = _detect_provider_from_lines(lines)
    if provider == "codex":
        return _parse_codex_session_lines(lines, malformed)
    return _parse_claude_session_lines(lines, malformed)


# --------------------------------------------------------------------------
# Boundary detection
# --------------------------------------------------------------------------


@dataclass
class Boundary:
    kind: str  # "start" | "end"
    line_no: int
    uuid: str
    ts: datetime | None
    match_rule: str
    extra: dict[str, Any] = field(default_factory=dict)


def find_start_candidates(session: ParsedSession, pattern: re.Pattern[str]) -> list[UserTextEvent]:
    matches = [u for u in session.user_texts if pattern.search(u.text)]
    matches.sort(key=lambda u: u.line_no)
    return matches


def find_end_candidates(session: ParsedSession, path_glob: str) -> list[ToolUse]:
    candidates = []
    for tu in session.tool_uses.values():
        if any(fnmatch.fnmatch(fp, path_glob) for fp in _candidate_file_paths(tu)):
            candidates.append(tu)
    candidates.sort(key=lambda t: (t.ts or datetime.min.replace(tzinfo=timezone.utc), t.line_no))
    return candidates


def resolve_start(session: ParsedSession, args: Any) -> Boundary:
    if getattr(args, "start_line", None) is not None:
        matches = [u for u in session.user_texts if u.line_no == args.start_line]
        if not matches:
            raise SystemExit(f"--start-line {args.start_line} does not match any user message")
        u = matches[0]
        return Boundary("start", u.line_no, u.uuid, u.ts, "explicit_override", {"text_preview": _text_preview(u.text)})

    if getattr(args, "start_uuid", None):
        matches = [u for u in session.user_texts if u.uuid == args.start_uuid]
        if not matches:
            raise SystemExit(f"--start-uuid {args.start_uuid} does not match any user message")
        u = matches[0]
        return Boundary("start", u.line_no, u.uuid, u.ts, "explicit_override", {"text_preview": _text_preview(u.text)})

    pattern = re.compile(args.start_pattern, re.IGNORECASE) if getattr(args, "start_pattern", None) else START_RE
    candidates = find_start_candidates(session, pattern)
    if not candidates:
        raise SystemExit("No start-pattern match found; use --start-line/--start-uuid or --list-candidates")

    occurrence = getattr(args, "start_occurrence", "first") or "first"
    index = 0 if occurrence == "first" else int(occurrence) - 1
    if index < 0 or index >= len(candidates):
        raise SystemExit(f"--start-occurrence out of range (found {len(candidates)} candidates)")
    u = candidates[index]
    return Boundary("start", u.line_no, u.uuid, u.ts, "start_pattern", {"text_preview": _text_preview(u.text)})


def _boundary_from_tool_use(tu: ToolUse, session: ParsedSession, match_rule: str, file_path: str = "") -> Boundary:
    result = session.tool_results.get(tu.id)
    if result is not None and result.ts is not None:
        ts = result.ts
        result_missing = False
        line_no = result.line_no
    else:
        ts = tu.ts
        result_missing = True
        line_no = tu.line_no
    if not file_path:
        paths = _candidate_file_paths(tu)
        file_path = paths[0] if paths else ""
    return Boundary(
        "end",
        line_no,
        tu.assistant_uuid,
        ts,
        match_rule,
        {
            "tool_use_id": tu.id,
            "file_path": file_path,
            "end_marker_found": True,
            "end_marker_result_missing": result_missing,
        },
    )


def resolve_end(session: ParsedSession, args: Any) -> Boundary:
    if getattr(args, "end_line", None) is not None:
        matches = [t for t in session.tool_uses.values() if t.line_no == args.end_line]
        if not matches:
            raise SystemExit(f"--end-line {args.end_line} does not match any tool_use")
        return _boundary_from_tool_use(matches[0], session, "explicit_override")

    if getattr(args, "end_uuid", None):
        tu = session.tool_uses.get(args.end_uuid)
        if tu is None:
            raise SystemExit(f"--end-uuid {args.end_uuid} does not match any tool_use id")
        return _boundary_from_tool_use(tu, session, "explicit_override")

    path_glob = getattr(args, "end_path_glob", "*plans/*.yaml")
    candidates = find_end_candidates(session, path_glob)
    if not candidates:
        return Boundary("end", -1, "", None, "no_matching_write_found", {"end_marker_found": False})

    occurrence = getattr(args, "end_occurrence", "last") or "last"
    index = 0 if occurrence == "first" else len(candidates) - 1
    match_rule = "first_yaml_write" if occurrence == "first" else "last_yaml_write"
    tu = candidates[index]
    matched_path = next((fp for fp in _candidate_file_paths(tu) if fnmatch.fnmatch(fp, path_glob)), "")
    return _boundary_from_tool_use(tu, session, match_rule, file_path=matched_path)


# --------------------------------------------------------------------------
# Timing model
# --------------------------------------------------------------------------


@dataclass
class Segment:
    seq: int
    type: str  # "thinking" | "tool_batch" | "unknown"
    label: str
    start: datetime | None
    end: datetime | None
    seconds: float
    extra: dict[str, Any] = field(default_factory=dict)


def classify_bash_command(command: str, script_names: set[str]) -> str:
    for name in sorted(script_names, key=len, reverse=True):
        if re.search(rf'(?:^|[\s/"\']){re.escape(name)}(?:$|[\s"\'])', command):
            return f"bash:{name}"
    return "bash:other"


def _command_string(tu: ToolUse) -> str:
    if tu.name == "Bash":
        return str(tu.input.get("command") or "")
    if tu.name in CODEX_SHELL_TOOL_NAMES:
        return str(tu.input.get("cmd") or tu.input.get("command") or "")
    return ""


def _label_for_tool_use(tu: ToolUse, script_names: set[str]) -> str:
    if tu.name == "Bash" or tu.name in CODEX_SHELL_TOOL_NAMES:
        return classify_bash_command(_command_string(tu), script_names)
    return f"tool:{tu.name}"


def _candidate_file_paths(tu: ToolUse) -> list[str]:
    if tu.name == "Write":
        fp = str(tu.input.get("file_path") or "")
        return [fp] if fp else []
    if tu.name == "apply_patch":
        return PATCH_FILE_RE.findall(str(tu.input.get("patch") or ""))
    return []


def discover_skill_scripts(skill_scripts_dirs: list[Path]) -> set[str]:
    names: set[str] = set()
    for skill_scripts_dir in skill_scripts_dirs:
        if skill_scripts_dir and skill_scripts_dir.is_dir():
            names |= {p.name for p in skill_scripts_dir.iterdir() if p.is_file() and p.suffix in (".sh", ".mjs", ".ts")}
    return names or set(DEFAULT_SKILL_SCRIPTS)


def build_timeline(
    session: ParsedSession, start: Boundary, end: Boundary, script_names: set[str]
) -> tuple[list[Segment], list[dict[str, Any]]]:
    anomalies: list[dict[str, Any]] = []
    cursor = start.ts
    segments: list[Segment] = []
    seq = 0
    pending_unknown_lines: list[int] = []
    pending_unknown_tool_ids: list[str] = []

    if cursor is None:
        anomalies.append({"kind": "missing_timestamp", "context": "start_boundary", "line": start.line_no})

    relevant_events = [
        e for e in session.assistant_events if e.line_no >= start.line_no and (end.line_no < 0 or e.line_no <= end.line_no)
    ]

    for event in relevant_events:
        if event.ts is None:
            anomalies.append({"kind": "missing_timestamp", "context": "assistant_event", "line": event.line_no})
            pending_unknown_lines.append(event.line_no)
            pending_unknown_tool_ids.extend(event.tool_use_ids)
            for tid in event.tool_use_ids:
                anomalies.append(
                    {"kind": "missing_timestamp", "context": "tool_use", "tool_use_id": tid, "line": event.line_no}
                )
            continue

        if pending_unknown_lines:
            if cursor is not None:
                gap = max(0.0, (event.ts - cursor).total_seconds())
                seq += 1
                segments.append(
                    Segment(
                        seq,
                        "unknown",
                        "unresolved_timing",
                        cursor,
                        event.ts,
                        gap,
                        {"lines": list(pending_unknown_lines), "tool_use_ids": list(pending_unknown_tool_ids)},
                    )
                )
            pending_unknown_lines = []
            pending_unknown_tool_ids = []
            cursor = event.ts
        elif cursor is not None:
            gap = (event.ts - cursor).total_seconds()
            if gap < 0:
                anomalies.append({"kind": "negative_duration", "context": "thinking_segment", "line": event.line_no})
                gap = 0.0
            seq += 1
            segments.append(
                Segment(seq, "thinking", "model_thinking", cursor, event.ts, gap, {"text_preview": event.text_preview})
            )
        else:
            cursor = event.ts

        if event.tool_use_ids:
            batch_end = event.ts
            unterminated_ids = []
            for tid in event.tool_use_ids:
                result = session.tool_results.get(tid)
                if result is None or result.ts is None:
                    unterminated_ids.append(tid)
                    continue
                if result.ts > batch_end:
                    batch_end = result.ts
            for tid in unterminated_ids:
                tu = session.tool_uses[tid]
                anomalies.append({"kind": "unterminated_tool_call", "tool_use_id": tid, "name": tu.name, "line": tu.line_no})

            duration = max(0.0, (batch_end - event.ts).total_seconds())
            labels = [_label_for_tool_use(session.tool_uses[tid], script_names) for tid in event.tool_use_ids]
            seq += 1
            segments.append(
                Segment(
                    seq,
                    "tool_batch",
                    labels[0] if len(labels) == 1 else f"parallel[{', '.join(labels)}]",
                    event.ts,
                    batch_end,
                    duration,
                    {"tool_use_ids": list(event.tool_use_ids), "parallel": len(event.tool_use_ids) > 1, "labels": labels},
                )
            )
            cursor = batch_end
        else:
            cursor = event.ts

    if pending_unknown_lines:
        end_anchor = end.ts if end.ts is not None else cursor
        if cursor is not None and end_anchor is not None:
            gap = max(0.0, (end_anchor - cursor).total_seconds())
            seq += 1
            segments.append(
                Segment(
                    seq,
                    "unknown",
                    "unresolved_timing",
                    cursor,
                    end_anchor,
                    gap,
                    {"lines": list(pending_unknown_lines), "tool_use_ids": list(pending_unknown_tool_ids)},
                )
            )

    return segments, anomalies


def resolve_total_span(start: Boundary, end: Boundary, segments: list[Segment]) -> float:
    if start.ts is None:
        return 0.0
    if end.extra.get("end_marker_found", True) and end.ts is not None:
        anchor = end.ts
    else:
        anchor = segments[-1].end if segments else start.ts
    if anchor is None:
        return 0.0
    return max(0.0, (anchor - start.ts).total_seconds())


def compute_totals(segments: list[Segment], total_span_seconds: float) -> dict[str, Any]:
    thinking = sum(s.seconds for s in segments if s.type == "thinking")
    tool_exec = sum(s.seconds for s in segments if s.type == "tool_batch")
    unknown = sum(s.seconds for s in segments if s.type == "unknown")
    accounted = thinking + tool_exec + unknown
    unknown += max(0.0, total_span_seconds - accounted)
    return {
        "model_thinking_seconds": round(thinking, 3),
        "tool_execution_seconds": round(tool_exec, 3),
        "unknown_timing_seconds": round(unknown, 3),
        "model_thinking_pct": round((thinking / total_span_seconds * 100.0) if total_span_seconds else 0.0, 1),
        "tool_execution_pct": round((tool_exec / total_span_seconds * 100.0) if total_span_seconds else 0.0, 1),
    }


def build_tool_breakdown(
    session: ParsedSession, start: Boundary, end: Boundary, script_names: set[str]
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for tid, tu in session.tool_uses.items():
        if tu.line_no < start.line_no:
            continue
        if end.line_no >= 0 and tu.line_no > end.line_no:
            continue
        result = session.tool_results.get(tid)
        if result is None or tu.ts is None or result.ts is None:
            continue
        seconds = max(0.0, (result.ts - tu.ts).total_seconds())
        label = _label_for_tool_use(tu, script_names)
        bucket = buckets.setdefault(
            label, {"label": label, "calls": 0, "busy_seconds_sum": 0.0, "nested_agent": tu.name == "Task", "expanded": False}
        )
        bucket["calls"] += 1
        bucket["busy_seconds_sum"] += seconds

    total_busy = sum(b["busy_seconds_sum"] for b in buckets.values()) or 1.0
    rows = sorted(buckets.values(), key=lambda b: -b["busy_seconds_sum"])
    for row in rows:
        row["busy_seconds_sum"] = round(row["busy_seconds_sum"], 3)
        row["pct_of_tool_execution"] = round(row["busy_seconds_sum"] / total_busy * 100.0, 1)
    return rows


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def render_markdown(result: dict[str, Any]) -> str:
    b = result["boundaries"]
    t = result["totals"]
    lines = [
        f"# Invoker skill run timing — {result['skill']}",
        "",
        f"Session: {result['session']['path']}",
        f"Span: {b['total_span_seconds']}s | Thinking: {t['model_thinking_seconds']}s ({t['model_thinking_pct']}%) | "
        f"Tool execution: {t['tool_execution_seconds']}s ({t['tool_execution_pct']}%)",
        "",
        "## Timeline",
        "| # | Type | Label | Seconds |",
        "|---|---|---|---|",
    ]
    for seg in result["timeline"]:
        lines.append(f"| {seg['seq']} | {seg['type']} | {seg['label']} | {seg['seconds']} |")
    lines += ["", "## Tool Breakdown", "| Label | Calls | Busy seconds | % of tool execution |", "|---|---|---|---|"]
    for row in result["tool_breakdown"]:
        lines.append(f"| {row['label']} | {row['calls']} | {row['busy_seconds_sum']} | {row['pct_of_tool_execution']}% |")
    if result["anomalies"]:
        lines += ["", "## Anomalies"]
        for a in result["anomalies"]:
            lines.append(f"- {a}")
    return "\n".join(lines) + "\n"


def _boundary_to_dict(b: Boundary) -> dict[str, Any]:
    return {
        "line": b.line_no,
        "uuid": b.uuid,
        "timestamp": b.ts.isoformat() if b.ts else None,
        "match_rule": b.match_rule,
        **b.extra,
    }


def build_result(
    session_path: Path, session: ParsedSession, start: Boundary, end: Boundary, script_names: set[str]
) -> dict[str, Any]:
    segments, anomalies = build_timeline(session, start, end, script_names)
    total_span = resolve_total_span(start, end, segments)
    totals = compute_totals(segments, total_span)
    tool_breakdown = build_tool_breakdown(session, start, end, script_names)

    return {
        "schema_version": SCHEMA_VERSION,
        "session": {"path": str(session_path), "provider": session.provider},
        "skill": "invoker-plan-to-invoker",
        "boundaries": {
            "start": _boundary_to_dict(start),
            "end": _boundary_to_dict(end),
            "total_span_seconds": round(total_span, 3),
        },
        "totals": totals,
        "tool_breakdown": tool_breakdown,
        "timeline": [
            {
                "seq": s.seq,
                "type": s.type,
                "label": s.label,
                "start": s.start.isoformat() if s.start else None,
                "end": s.end.isoformat() if s.end else None,
                "seconds": round(s.seconds, 3),
                **s.extra,
            }
            for s in segments
        ],
        "anomalies": anomalies,
        "stats": {"malformed_lines": session.malformed_lines, "lines_parsed": session.total_lines},
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def resolve_session_path(args: argparse.Namespace) -> Path:
    if args.session:
        return Path(args.session).expanduser()
    if args.session_id:
        matches = sorted(DEFAULT_CLAUDE_DIR.glob(f"*/{args.session_id}.jsonl"))
        if matches:
            return matches[0]
        for codex_dir in DEFAULT_CODEX_SESSIONS_DIRS:
            matches = sorted(codex_dir.glob(f"**/*{args.session_id}*.jsonl"))
            if matches:
                return matches[0]
        searched = [str(DEFAULT_CLAUDE_DIR)] + [str(d) for d in DEFAULT_CODEX_SESSIONS_DIRS]
        raise SystemExit(f"No session found for --session-id {args.session_id} under {searched}")
    raise SystemExit("Provide --session PATH or --session-id UUID")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ad-hoc end-to-end timing breakdown for an invoker-plan-to-invoker skill run.")
    parser.add_argument("--session", help="Path to a Claude Code session JSONL transcript")
    parser.add_argument("--session-id", help="Session UUID; resolved under ~/.claude/projects/*/<uuid>.jsonl")
    parser.add_argument(
        "--skill-scripts-dir", help="Override the skill scripts directory (default: search both Claude and Codex skill installs)"
    )
    parser.add_argument("--start-pattern", help="Override the default skill-invocation regex")
    parser.add_argument("--end-path-glob", default="*plans/*.yaml")
    parser.add_argument("--start-line", type=int)
    parser.add_argument("--start-uuid")
    parser.add_argument("--end-line", type=int)
    parser.add_argument("--end-uuid")
    parser.add_argument("--start-occurrence", default="first", help="'first' or a 1-based occurrence number")
    parser.add_argument("--end-occurrence", default="last", choices=["first", "last"])
    parser.add_argument("--list-candidates", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Non-zero exit if the end marker or any tool call is unresolved")
    parser.add_argument("--json-out")
    parser.add_argument("--markdown-out")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session_path = resolve_session_path(args)
    session = parse_session(session_path)
    if args.skill_scripts_dir:
        script_names = discover_skill_scripts([Path(args.skill_scripts_dir).expanduser()])
    else:
        script_names = discover_skill_scripts([DEFAULT_SKILL_SCRIPTS_DIR, DEFAULT_CODEX_SKILL_SCRIPTS_DIR])

    if args.list_candidates:
        pattern = re.compile(args.start_pattern, re.IGNORECASE) if args.start_pattern else START_RE
        starts = find_start_candidates(session, pattern)
        ends = find_end_candidates(session, args.end_path_glob)
        print(f"Provider: {session.provider}")
        print(f"Start candidates ({len(starts)}):")
        for u in starts:
            print(f"  line={u.line_no} uuid={u.uuid} ts={u.ts} text={_text_preview(u.text, 100)!r}")
        print(f"\nEnd candidates ({len(ends)}):")
        for tu in ends:
            paths = _candidate_file_paths(tu)
            print(f"  line={tu.line_no} tool_use_id={tu.id} ts={tu.ts} file_path(s)={paths!r}")
        return 0

    start = resolve_start(session, args)
    end = resolve_end(session, args)
    result = build_result(session_path, session, start, end, script_names)

    output = json.dumps(result, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(output + "\n")
    else:
        print(output)

    if args.markdown_out:
        Path(args.markdown_out).write_text(render_markdown(result))

    if args.strict:
        unterminated = [a for a in result["anomalies"] if a["kind"] == "unterminated_tool_call"]
        if not result["boundaries"]["end"].get("end_marker_found", True) or unterminated:
            return 4
        if result["totals"]["unknown_timing_seconds"] > 0:
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
