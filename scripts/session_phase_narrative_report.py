#!/usr/bin/env python3
"""Generate prompt-window phase narratives from v4.5 command attribution rows."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO_ROOT / "reports" / "usage-command-attribution-v4_5.csv"
DEFAULT_OUTPUT = REPO_ROOT / "reports" / "session-phase-narratives-v1"
SCHEMA_VERSION = "usage_command_attribution_v4_5"
CLASSIFICATION_REVISION = "classifier_v4_5"

PHASES = [
    "orientation",
    "implementation",
    "failure_diagnosis",
    "repair_loop",
    "local_validation",
    "ci_merge_monitoring",
    "branch_stack_operations",
    "final_proof",
    "operational_waiting",
]
EFFICIENCY_LABELS = ["productive", "expected_overhead", "thrash"]

FIXING_CAUSE_LABELS = [
    "Failure diagnosis thrash",
    "Expected failure investigation overhead",
    "Repeated repair/test loops",
    "Orientation in service of fixing",
    "CI/merge monitoring thrash",
]


@dataclass
class ClassifiedRow:
    row: dict[str, str]
    workflow_phase: str
    efficiency_label: str
    reason: str
    confidence: str = "medium"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate prompt-window phase narrative reports.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--llm-review", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--codex-timeout-seconds", type=int, default=120)
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


def compact(text: str, limit: int = 220) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: max(0, limit - 3)].rstrip() + "..."
def deterministic_short_title(prompt: str) -> str:
    return compact(prompt, 80)


def session_id(row: dict[str, str]) -> str:
    return Path(row.get("file", "")).stem or "unknown-session"


def row_cost(row: dict[str, str]) -> float:
    return to_float(row.get("allocated_total_cost_usd"))


def rows_cost(rows: list[dict[str, str]] | list[ClassifiedRow]) -> float:
    total = 0.0
    for item in rows:
        row = item.row if isinstance(item, ClassifiedRow) else item
        total += row_cost(row)
    return total


def fixing_cause_for(workflow_phase: str, efficiency_label: str) -> str | None:
    if workflow_phase == "failure_diagnosis" and efficiency_label == "thrash":
        return FIXING_CAUSE_LABELS[0]
    if workflow_phase == "failure_diagnosis" and efficiency_label == "expected_overhead":
        return FIXING_CAUSE_LABELS[1]
    if workflow_phase == "repair_loop":
        return FIXING_CAUSE_LABELS[2]
    if workflow_phase == "orientation":
        return FIXING_CAUSE_LABELS[3]
    if workflow_phase == "ci_merge_monitoring" and efficiency_label == "thrash":
        return FIXING_CAUSE_LABELS[4]
    return None


def command_preview(row: dict[str, str]) -> str:
    for value in (
        row.get("command_preview"),
        row.get("stdin_preview"),
        row.get("terminal_context_parent_command_preview"),
        row.get("function_name"),
    ):
        preview = compact(value or "", 220)
        if preview:
            return preview
    return ""


def event_text(row: dict[str, str]) -> str:
    return " ".join(
        [
            row.get("command_preview") or "",
            row.get("stdin_preview") or "",
            row.get("terminal_context_parent_command_preview") or "",
            row.get("target") or "",
        ]
    ).lower()


def command_text(row: dict[str, str]) -> str:
    return " ".join(
        [
            row.get("command_preview") or "",
            row.get("stdin_preview") or "",
            row.get("terminal_context_parent_command_preview") or "",
        ]
    ).lower()


def display_event_text(row: dict[str, str], limit: int = 150) -> str:
    return compact(
        row.get("command_preview")
        or row.get("stdin_preview")
        or row.get("terminal_context_parent_command_preview")
        or row.get("target")
        or row.get("function_name")
        or "",
        limit,
    )


def effective_shell_verb(row: dict[str, str]) -> str:
    return ((row.get("shell_verb") or row.get("terminal_context_parent_shell_verb") or "")).lower()


def is_read_shell(shell: str) -> bool:
    return shell in {"rg", "grep", "sed", "nl", "cat", "tail", "find", "ls", "head", "wc"}


def is_edit_event(row: dict[str, str]) -> bool:
    function = (row.get("function_name") or "").lower()
    shell = effective_shell_verb(row)
    text = command_text(row)
    return (
        function in {"edit", "apply_patch"}
        or shell in {"apply_patch"}
        or bool(re.search(r"\b(apply_patch|cat >|tee |python3? .*write_text|perl -pi|sed -i)\b", text))
    )


def is_test_event(row: dict[str, str]) -> bool:
    intention = (row.get("agent_tool_intention") or "").lower()
    shell = effective_shell_verb(row)
    text = command_text(row)
    if is_read_shell(shell):
        return False
    if intention in {"test_execution", "full_validation", "visual_proof", "failure_reproduction"} and not is_read_shell(shell):
        return True
    return bool(
        re.search(
            r"\b(pnpm\s+(test|run\s+test)|npm\s+(test|run\s+test)|yarn\s+test|pytest|vitest|playwright|cargo\s+test|go\s+test)\b"
            r"|scripts/repro/|repro-|\be2e\b|--expect\s+(fixed|pass)",
            text,
        )
    )


def is_ci_event(row: dict[str, str]) -> bool:
    intention = (row.get("agent_tool_intention") or "").lower()
    text = command_text(row)
    return intention == "ci_monitoring" or bool(re.search(r"\b(gh\s+run|gh\s+pr\s+checks|mergify|merge queue)\b", text))


def is_branch_stack_event(row: dict[str, str]) -> bool:
    intention = (row.get("agent_tool_intention") or "").lower()
    shell = effective_shell_verb(row)
    text = command_text(row)
    return intention == "branch_stack_orchestration" or shell == "git" or text.startswith("git ")


def is_git_hygiene_event(row: dict[str, str]) -> bool:
    shell = effective_shell_verb(row)
    text = command_text(row)
    return shell == "git" and bool(
        re.search(
            r"\b(git\s+status|git\s+diff\s+--check|git\s+diff\s+--stat|git\s+diff\s+--name-only|git\s+log|git\s+branch|git\s+remote\s+-v|git\s+rev-parse)\b",
            text,
        )
    )


def is_git_mutation_event(row: dict[str, str]) -> bool:
    shell = effective_shell_verb(row)
    text = command_text(row)
    return shell == "git" and bool(
        re.search(r"\b(git\s+add|git\s+commit|git\s+push|git\s+fetch|git\s+checkout|git\s+rebase|git\s+merge|git\s+reset)\b", text)
    )


def is_failure_context(prompt: str, row: dict[str, str]) -> bool:
    text = command_text(row)
    return bool(
        re.search(
            r"\b(fail(?:ed|ing|ure)?|error|broken|regression|fix\s+ci|fix\s+the\s+code|debug|diagnos|repro|retry|rerun|flaky|conflict)\b",
            f"{prompt} {text}",
        )
    )


def is_repair_inspection_event(row: dict[str, str]) -> bool:
    if not is_diagnosis_event(row):
        return False
    text = event_text(row)
    return bool(re.search(r"\b(fail(?:ed|ing|ure)?|error|log|trace|repro|snapshot|stderr|stdout|ci|test)\b", text))


def is_wait_event(row: dict[str, str]) -> bool:
    function = (row.get("function_name") or "").lower()
    shell = effective_shell_verb(row)
    text = command_text(row)
    return (
        function == "write_stdin"
        or (row.get("stdin_input_kind") or "") == "wait_for_process"
        or shell == "sleep"
        or bool(re.search(r"\bsleep\s+\d+", text))
    )


def is_diagnosis_event(row: dict[str, str]) -> bool:
    intention = (row.get("agent_tool_intention") or "").lower()
    shell = effective_shell_verb(row)
    text = command_text(row)
    if intention in {"failure_diagnosis_inspection", "failure_reproduction"}:
        return True
    return shell in {"rg", "grep", "sed", "nl", "cat", "tail", "find", "ls"} and bool(
        re.search(r"fail|error|repro|log|test|ci|snapshot|mutation|recovery|status", text)
    )


def is_final_proof_event(row: dict[str, str], index: int, total_rows: int, seen_edit: bool) -> bool:
    intention = (row.get("agent_tool_intention") or "").lower()
    text = event_text(row)
    late = index >= max(0, int(total_rows * 0.65))
    if late and intention in {"full_validation", "visual_proof"}:
        return True
    if late and seen_edit and is_test_event(row) and re.search(r"full|all|final|ci|--expect\s+fixed|proof|screenshot|visual", text):
        return True
    if late and seen_edit and intention in {"diff_review", "ci_monitoring"}:
        return True
    return False


def classify_prompt_window(rows: list[dict[str, str]]) -> list[ClassifiedRow]:
    ordered = sorted(rows, key=lambda row: to_int(row.get("command_index")))
    total_rows = len(ordered)
    seen_edit = False
    seen_test = False
    seen_failure_context = False
    last_test_index = -10_000
    classified: list[ClassifiedRow] = []

    for index, row in enumerate(ordered):
        prompt = (row.get("prompt_preview") or "").lower()
        intention = (row.get("agent_tool_intention") or "").lower()
        phase = "orientation"
        efficiency = "productive"
        reason = "default_orientation"
        confidence = "medium"
        failure_context = is_failure_context(prompt, row)

        if is_final_proof_event(row, index, total_rows, seen_edit):
            phase, efficiency, reason, confidence = "final_proof", "productive", "late_full_or_ci_proof", "high"
        elif is_git_hygiene_event(row):
            phase = "final_proof" if seen_edit or seen_test or index >= total_rows * 0.65 else "branch_stack_operations"
            efficiency = "expected_overhead"
            reason = "git_hygiene_or_diff_check"
            confidence = "high"
        elif is_git_mutation_event(row):
            phase, efficiency, reason, confidence = "branch_stack_operations", "expected_overhead", "git_state_mutation", "high"
        elif is_ci_event(row):
            phase = "ci_merge_monitoring"
            efficiency = "thrash" if ("continue fixing" in prompt or "fix ci" in prompt or seen_failure_context) else "expected_overhead"
            reason = "ci_or_merge_queue_monitoring"
            confidence = "high"
        elif is_branch_stack_event(row):
            phase = "branch_stack_operations"
            efficiency = "expected_overhead"
            reason = "git_or_branch_stack_operation"
            confidence = "high"
        elif is_edit_event(row):
            phase, efficiency, reason, confidence = "implementation", "productive", "edit_or_file_mutation", "high"
            seen_edit = True
        elif is_test_event(row):
            if seen_edit and not seen_test and index < total_rows * 0.65:
                phase, efficiency, reason, confidence = "local_validation", "productive", "first_focused_test_after_edit", "high"
            elif (index - last_test_index < 8 and (failure_context or seen_failure_context)) or (seen_test and failure_context):
                phase, efficiency, reason, confidence = "repair_loop", "thrash", "repeated_or_failure_driven_test_loop", "medium"
            else:
                phase = "local_validation" if index < total_rows * 0.75 else "final_proof"
                efficiency = "expected_overhead" if phase == "local_validation" else "productive"
                reason = "test_or_repro_execution"
                confidence = "medium"
            seen_test = True
            last_test_index = index
        elif is_wait_event(row):
            phase = "operational_waiting"
            efficiency = "thrash" if seen_failure_context or "continue fixing" in prompt else "expected_overhead"
            reason = "process_wait_or_sleep"
            confidence = "medium"
        elif is_diagnosis_event(row):
            if seen_test and failure_context and is_repair_inspection_event(row):
                phase, efficiency, reason, confidence = "repair_loop", "thrash", "post_test_failure_repair_inspection", "medium"
            else:
                phase = "failure_diagnosis"
                efficiency = "thrash" if failure_context else "expected_overhead"
                reason = "failure_or_status_inspection"
                confidence = "medium" if failure_context else "low"
        elif intention in {"implementation_planning_inspection", "repo_orientation"}:
            if seen_test and failure_context:
                phase, efficiency, reason, confidence = "failure_diagnosis", "thrash", "post_test_context_inspection", "low"
            else:
                phase, efficiency, reason, confidence = "orientation", "expected_overhead", "implementation_context_inspection", "medium"
        elif intention == "diff_review":
            phase = "final_proof" if index > total_rows * 0.65 else "implementation"
            efficiency = "expected_overhead"
            reason = "diff_review"
            confidence = "medium"

        if failure_context:
            seen_failure_context = True
        classified.append(ClassifiedRow(row=row, workflow_phase=phase, efficiency_label=efficiency, reason=reason, confidence=confidence))
    return classified


def load_rows(path: Path, start_date: str | None, end_date: str | None) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = []
        for row in csv.DictReader(handle):
            if row.get("schema_version") != SCHEMA_VERSION:
                continue
            if row.get("classification_revision") != CLASSIFICATION_REVISION:
                continue
            session_date = row.get("session_date") or ""
            if start_date and session_date < start_date:
                continue
            if end_date and session_date > end_date:
                continue
            rows.append(row)
        return rows


def prompt_windows(rows: list[dict[str, str]]) -> list[tuple[tuple[str, str], list[dict[str, str]]]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(session_id(row), row.get("prompt_index") or "0")].append(row)
    return sorted(grouped.items(), key=lambda item: rows_cost(item[1]), reverse=True)


def rollup_classified(classified: list[ClassifiedRow], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[ClassifiedRow]] = defaultdict(list)
    for item in classified:
        grouped[getattr(item, key)].append(item)
    total = rows_cost(classified)
    return [
        {
            key: name,
            "cost_usd": rows_cost(items),
            "cost_pct": (rows_cost(items) / total * 100.0) if total else 0.0,
            "events": len(items),
        }
        for name, items in sorted(grouped.items(), key=lambda pair: rows_cost(pair[1]), reverse=True)
    ]


def fixing_cause_rollup(classified: list[ClassifiedRow]) -> list[dict[str, Any]]:
    grouped: dict[str, list[ClassifiedRow]] = defaultdict(list)
    total = rows_cost(classified)
    for item in classified:
        cause = fixing_cause_for(item.workflow_phase, item.efficiency_label)
        if cause:
            grouped[cause].append(item)
    return [
        {
            "cause": cause,
            "cost_usd": rows_cost(items),
            "cost_pct": round(rows_cost(items) / total * 100.0, 4) if total else 0.0,
            "events": len(items),
        }
        for cause, items in sorted(grouped.items(), key=lambda item: rows_cost(item[1]), reverse=True)
    ]


def command_rows(classified: list[ClassifiedRow]) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    for item in sorted(classified, key=lambda current: to_int(current.row.get("command_index"))):
        row = item.row
        commands.append(
            {
                "command_index": to_int(row.get("command_index")),
                "cost_usd": row_cost(row),
                "workflow_phase": item.workflow_phase,
                "efficiency_label": item.efficiency_label,
                "fixing_cause": fixing_cause_for(item.workflow_phase, item.efficiency_label),
                "agent_tool_intention": row.get("agent_tool_intention") or "",
                "function_name": row.get("function_name") or "",
                "shell_verb": row.get("shell_verb") or "",
                "preview": command_preview(row),
                "stdin_preview": compact(row.get("stdin_preview") or "", 220),
                "terminal_context_parent_command_preview": compact(row.get("terminal_context_parent_command_preview") or "", 220),
            }
        )
    return commands


def top_events(rows: list[dict[str, str]], limit: int = 10) -> list[dict[str, Any]]:
    events = []
    for row in sorted(rows, key=row_cost, reverse=True)[:limit]:
        events.append(
            {
                "cost_usd": row_cost(row),
                "command_index": to_int(row.get("command_index")),
                "agent_tool_intention": row.get("agent_tool_intention") or "",
                "function_name": row.get("function_name") or "",
                "shell_verb": row.get("shell_verb") or "",
                "preview": display_event_text(row, 220),
            }
        )
    return events


def segment_timeline(classified: list[ClassifiedRow]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    current: list[ClassifiedRow] = []
    current_key: tuple[str, str] | None = None
    for item in classified:
        key = (item.workflow_phase, item.efficiency_label)
        if current and key != current_key:
            segments.append(segment_payload(current, current_key or ("unknown", "unknown")))
            current = []
        current_key = key
        current.append(item)
    if current:
        segments.append(segment_payload(current, current_key or ("unknown", "unknown")))
    return segments


def segment_payload(items: list[ClassifiedRow], key: tuple[str, str]) -> dict[str, Any]:
    rows = [item.row for item in items]
    commands = []
    for row in rows:
        preview = display_event_text(row, 130)
        if preview and preview not in commands:
            commands.append(preview)
        if len(commands) >= 3:
            break
    fixing_cause = fixing_cause_for(key[0], key[1])
    return {
        "workflow_phase": key[0],
        "efficiency_label": key[1],
        "fixing_cause": fixing_cause,
        "cost_usd": rows_cost(rows),
        "events": len(rows),
        "start_command_index": to_int(rows[0].get("command_index")),
        "end_command_index": to_int(rows[-1].get("command_index")),
        "confidence_counts": dict(Counter(item.confidence for item in items)),
        "reason_counts": dict(Counter(item.reason for item in items).most_common(5)),
        "examples": commands,
    }


def deterministic_narrative(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload["prompt_preview"]
    phases = payload["phase_rollup"]
    efficiencies = payload["efficiency_rollup"]
    top_phase = phases[0]["workflow_phase"] if phases else "unknown"
    top_efficiency = efficiencies[0]["efficiency_label"] if efficiencies else "unknown"
    why = (
        f"The highest-cost phase was {top_phase} and the dominant efficiency label was {top_efficiency}. "
        f"The prompt window spent {money(payload['total_cost_usd'])} across {payload['event_count']} tool events."
    )
    bullets = [
        f"Prompt: {compact(prompt, 180)}",
        f"Total cost: {money(payload['total_cost_usd'])} across {payload['event_count']} events.",
        "Phase split: "
        + ", ".join(f"{item['workflow_phase']} {money(item['cost_usd'])}" for item in phases[:4]),
        "Efficiency split: "
        + ", ".join(f"{item['efficiency_label']} {money(item['cost_usd'])}" for item in efficiencies[:3]),
    ]
    for event in payload["top_events"][:3]:
        bullets.append(
            f"Costly event: {money(event['cost_usd'])} {event['function_name']}/{event['shell_verb']} {compact(event['preview'], 130)}"
        )
    return {
        "review_status": "deterministic_only",
        "short_title": deterministic_short_title(prompt),
        "what_happened": bullets,
        "why_expensive": why,
        "confidence": "medium",
    }


def llm_review(payload: dict[str, Any], cache_dir: Path, timeout_seconds: int) -> dict[str, Any] | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    cache_path = cache_dir / f"{cache_key}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "short_title": {"type": "string"},
            "what_happened": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 10},
            "why_expensive": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["short_title", "what_happened", "why_expensive", "confidence"],
    }
    prompt = (
        "Write a standardized narrative for this expensive AI prompt window.\n"
        "Do not invent commands or costs. Use only the JSON facts.\n"
        "Explain phases, likely thrash, and why the window was expensive.\n\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="session-phase-review-") as tmpdir:
            tmp = Path(tmpdir)
            schema_path = tmp / "schema.json"
            output_path = tmp / "output.json"
            schema_path.write_text(json.dumps(schema))
            subprocess.run(
                [
                    "codex",
                    "exec",
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "-",
                ],
                input=prompt,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=True,
            )
            result = json.loads(output_path.read_text())
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TimeoutError):
        return None
    if not isinstance(result, dict):
        return None
    result["review_status"] = "llm_reviewed"
    cache_path.write_text(json.dumps(result, indent=2, sort_keys=True))
    return result


def build_window_payload(
    rank: int,
    key: tuple[str, str],
    rows: list[dict[str, str]],
    output_dir: Path,
    llm_enabled: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    sid, prompt_index = key
    ordered = sorted(rows, key=lambda row: to_int(row.get("command_index")))
    classified = classify_prompt_window(ordered)
    cause_rollup = fixing_cause_rollup(classified)
    payload = {
        "rank": rank,
        "session_id": sid,
        "prompt_index": prompt_index,
        "source_file": ordered[0].get("file") or "",
        "session_date": ordered[0].get("session_date") or "",
        "prompt_preview": ordered[0].get("prompt_preview") or ordered[0].get("first_prompt_preview") or "",
        "final_answer_preview": ordered[-1].get("final_answer_preview") or "",
        "total_cost_usd": rows_cost(ordered),
        "event_count": len(ordered),
        "phase_rollup": rollup_classified(classified, "workflow_phase"),
        "efficiency_rollup": rollup_classified(classified, "efficiency_label"),
        "fixing_cause_rollup": cause_rollup,
        "dominant_fixing_cause": cause_rollup[0]["cause"] if cause_rollup else "",
        "timeline": segment_timeline(classified),
        "commands": command_rows(classified),
        "top_events": top_events(ordered),
        "agent_tool_intentions": [
            {"agent_tool_intention": name, "events": count}
            for name, count in Counter(row.get("agent_tool_intention") or "unknown" for row in ordered).most_common(10)
        ],
    }
    review = llm_review(payload, output_dir / "cache", timeout_seconds) if llm_enabled else None
    if review is None:
        review = deterministic_narrative(payload)
        if llm_enabled:
            review["review_status"] = "failed_fallback"
    payload["narrative"] = review
    return payload


def safe_slug(value: str, limit: int = 90) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return slug[:limit] or "window"


def write_window_files(payload: dict[str, Any], windows_dir: Path) -> tuple[Path, Path]:
    name = f"{payload['rank']:03d}-{safe_slug(payload['session_id'], 64)}-p{payload['prompt_index']}"
    payload["window_file"] = f"{name}.json"
    json_path = windows_dir / payload["window_file"]
    md_path = windows_dir / f"{name}.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    md_path.write_text(window_markdown(payload))
    return md_path, json_path


def window_markdown(payload: dict[str, Any]) -> str:
    narrative = payload["narrative"]
    lines = [
        f"# #{payload['rank']} {narrative.get('short_title') or payload['session_id']}",
        "",
        f"- Session ID: `{payload['session_id']}`",
        f"- Prompt index: `{payload['prompt_index']}`",
        f"- Date: `{payload['session_date']}`",
        f"- Total cost: `{money(payload['total_cost_usd'])}`",
        f"- Events: `{payload['event_count']}`",
        f"- Review status: `{narrative.get('review_status', '')}`",
        "",
        "## Prompt",
        "",
        compact(payload["prompt_preview"], 900),
        "",
        "## Narrative",
        "",
    ]
    for item in narrative.get("what_happened") or []:
        lines.append(f"- {item}")
    lines.extend(["", f"**Why expensive:** {narrative.get('why_expensive', '')}", "", "## Phase Rollup", ""])
    for item in payload["phase_rollup"]:
        lines.append(f"- `{item['workflow_phase']}`: {money(item['cost_usd'])}, {pct(item['cost_usd'], payload['total_cost_usd'])}, {item['events']} events")
    lines.extend(["", "## Efficiency Rollup", ""])
    for item in payload["efficiency_rollup"]:
        lines.append(f"- `{item['efficiency_label']}`: {money(item['cost_usd'])}, {pct(item['cost_usd'], payload['total_cost_usd'])}, {item['events']} events")
    lines.extend(["", "## Timeline", ""])
    for item in payload["timeline"]:
        lines.append(
            f"- `{item['start_command_index']}-{item['end_command_index']}` `{item['workflow_phase']}` / `{item['efficiency_label']}`: "
            f"{money(item['cost_usd'])}, {item['events']} events"
        )
        for example in item.get("examples", [])[:2]:
            lines.append(f"  - {example}")
    lines.extend(["", "## Top Costly Events", ""])
    for event in payload["top_events"]:
        lines.append(
            f"- {money(event['cost_usd'])} `{event['agent_tool_intention']}` `{event['function_name']}/{event['shell_verb']}`: {event['preview']}"
        )
    lines.append("")
    return "\n".join(lines)


def write_summary(payloads: list[dict[str, Any]], output_dir: Path) -> None:
    total = sum(payload["total_cost_usd"] for payload in payloads)
    phase_totals: dict[str, float] = defaultdict(float)
    efficiency_totals: dict[str, float] = defaultdict(float)
    for payload in payloads:
        for item in payload["phase_rollup"]:
            phase_totals[item["workflow_phase"]] += item["cost_usd"]
        for item in payload["efficiency_rollup"]:
            efficiency_totals[item["efficiency_label"]] += item["cost_usd"]

    summary = {
        "schema_version": "session_phase_narratives_v1",
        "window_count": len(payloads),
        "total_cost_usd": total,
        "phase_totals": dict(sorted(phase_totals.items(), key=lambda pair: pair[1], reverse=True)),
        "efficiency_totals": dict(sorted(efficiency_totals.items(), key=lambda pair: pair[1], reverse=True)),
        "windows": [
            {
                "rank": payload["rank"],
                "session_id": payload["session_id"],
                "prompt_index": payload["prompt_index"],
                "session_date": payload["session_date"],
                "total_cost_usd": payload["total_cost_usd"],
                "event_count": payload["event_count"],
                "window_file": payload.get("window_file", ""),
                "dominant_fixing_cause": payload.get("dominant_fixing_cause", ""),
                "fixing_cause_rollup": payload.get("fixing_cause_rollup", []),
                "short_title": payload["narrative"].get("short_title", ""),
                "review_status": payload["narrative"].get("review_status", ""),
            }
            for payload in payloads
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))

    with (output_dir / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        fieldnames = ["rank", "session_id", "prompt_index", "session_date", "total_cost_usd", "event_count", "window_file", "dominant_fixing_cause", "fixing_cause_rollup", "short_title", "review_status"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary["windows"])

    lines = [
        "# Session Phase Narrative Summary",
        "",
        f"- Windows: `{len(payloads)}`",
        f"- Total cost: `{money(total)}`",
        "",
        "## Phase Totals",
        "",
    ]
    for name, value in summary["phase_totals"].items():
        lines.append(f"- `{name}`: {money(value)}, {pct(value, total)}")
    lines.extend(["", "## Efficiency Totals", ""])
    for name, value in summary["efficiency_totals"].items():
        lines.append(f"- `{name}`: {money(value)}, {pct(value, total)}")
    lines.extend(["", "## Windows", ""])
    for item in summary["windows"][:100]:
        lines.append(
            f"- #{item['rank']} {money(item['total_cost_usd'])} `{item['session_id']}` p`{item['prompt_index']}`: {item['short_title']}"
        )
    lines.append("")
    (output_dir / "summary.md").write_text("\n".join(lines))


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    windows_dir = output_dir / "windows"
    windows_dir.mkdir(parents=True, exist_ok=True)
    rows = load_rows(input_path, args.start_date, args.end_date)
    selected = prompt_windows(rows)[: args.limit]
    payloads = []
    for rank, (key, group_rows) in enumerate(selected, 1):
        payload = build_window_payload(rank, key, group_rows, output_dir, args.llm_review, args.codex_timeout_seconds)
        payloads.append(payload)
        write_window_files(payload, windows_dir)
    write_summary(payloads, output_dir)
    print(f"Wrote {len(payloads)} prompt-window narratives to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
