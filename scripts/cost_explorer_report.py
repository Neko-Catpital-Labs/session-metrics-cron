#!/usr/bin/env python3
"""Build local cost-explorer artifacts from v4.5 command attribution rows."""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from mixpanel_export_usage import (
    RequestPatternCategorizer,
    TaskCategorizer,
    derive_task_label,
    load_request_pattern_config,
    load_task_categorization_config,
)
from session_phase_narrative_report import (
    ClassifiedRow,
    classify_prompt_window,
    command_preview,
    compact,
    deterministic_short_title,
    fixing_cause_for,
    fixing_cause_rollup,
    prompt_windows,
    rollup_classified,
    safe_slug,
    session_id,
)
from usage_costing import load_pricing_table
from ci_session_viewer import parse_prompt_windows, summarize_line

from warehouse_cost_demo import (
    CLASSIFICATION_REVISION,
    OUTPUT_COLUMNS,
    SCHEMA_VERSION,
    component_costs,
    normalize_row,
    source_rows,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO_ROOT / "reports" / "usage-command-attribution-v4_5.csv"
DEFAULT_OUTPUT = REPO_ROOT / "reports" / "cost-explorer-v1"
DEFAULT_REQUEST_PATTERN_CONFIG = REPO_ROOT / "config" / "request-patterns.yaml"
COST_EXPLORER_HTML = REPO_ROOT / "docs" / "cost-explorer.html"
STATIC_WINDOW_ROW_FIELDS = [
    "session_date",
    "session_id",
    "prompt_index",
    "window_file",
    "short_title",
    "prompt_preview",
    "request_pattern",
    "task_type",
    "task_type_label",
    "task_label",
    "dominant_fixing_cause",
    "origin",
    "model",
    "total_cost_usd",
    "headline_context_cost_usd",
    "headline_cache_read_cost_usd",
    "headline_output_cost_usd",
]
STATIC_COMMAND_ROW_FIELDS = [
    "session_date",
    "session_id",
    "prompt_index",
    "window_file",
    "fixing_cause",
    "task_type",
    "request_pattern",
    "agent_tool_intention",
    "function_name",
    "shell_verb",
    "model",
    "origin",
    "allocated_total_cost_usd",
    "headline_context_cost_usd",
    "headline_cache_read_cost_usd",
    "headline_output_cost_usd",
]

DEFAULT_TASK_CATEGORIZATION_CONFIG = REPO_ROOT / "config" / "task-categorization.yaml"
DIAGNOSIS_VERSION = os.getenv("USAGE_DIAGNOSIS_VERSION", "request_pattern_layers_v1")
WINDOWS_COLUMNS = [
    "session_date",
    "session_id",
    "prompt_index",
    "window_file",
    "short_title",
    "source_file",
    "model",
    "origin",
    "provider",
    "billable_model",
    "usage_source",
    "bucket",
    "session_cwd",
    "prompt_preview",
    "previous_prompt_preview",
    "first_prompt_preview",
    "final_answer_preview",
    "request_pattern",
    "request_pattern_path",
    "request_pattern_depth",
    "task_type",
    "task_type_label",
    "task_label",
    "dominant_fixing_cause",
    "command_count",
    "tool_count",
    "total_cost_usd",
    "prompt_input_tokens",
    "prompt_cache_read_tokens",
    "prompt_cache_creation_tokens",
    "prompt_output_tokens",
    "prompt_reasoning_tokens",
    "prompt_total_tokens",
    "headline_context_tokens",
    "headline_context_cost_usd",
    "headline_cache_read_tokens",
    "headline_cache_read_cost_usd",
    "headline_output_tokens",
    "headline_output_cost_usd",
    "top_function_name",
    "top_shell_verb",
    "fixing_cause_rollup_json",
]
COMMANDS_APPENDED_COLUMNS = [
    "origin",
    "bucket",
    "usage_source",
    "billable_model_source",
    "file",
    "session_cwd",
    "prompt_preview",
    "previous_prompt_preview",
    "first_prompt_preview",
    "final_answer_preview",
    "command_preview",
    "workdir",
    "target_type",
    "target",
    "stdin_preview",
    "delegated_agent_action",
    "delegated_agent_type",
    "delegated_task_preview",
    "request_pattern",
    "request_pattern_path",
    "request_pattern_depth",
    "request_pattern_rule_id",
    "request_pattern_confidence",
    "request_pattern_config_version",
    "diagnosis_version",
    "task_type",
    "task_type_label",
    "task_type_confidence",
    "task_type_classifier",
    "task_type_reason",
    "task_type_source",
    "task_type_config_version",
    "task_label",
    "task_label_source",
    "task_label_confidence",
    "fixing_cause",
    "headline_context_tokens",
    "headline_context_cost_usd",
    "headline_cache_read_tokens",
    "headline_cache_read_cost_usd",
    "headline_output_tokens",
    "headline_output_cost_usd",
    "window_file",
]
COMMANDS_COLUMNS = [*OUTPUT_COLUMNS, *COMMANDS_APPENDED_COLUMNS]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--request-pattern-config", default=str(DEFAULT_REQUEST_PATTERN_CONFIG))
    parser.add_argument("--task-categorization-config", default=str(DEFAULT_TASK_CATEGORIZATION_CONFIG))
    parser.add_argument("--pricing-table", default=os.environ.get("USAGE_PRICING_TABLE", ""))
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


def tool_label(row: dict[str, Any]) -> str:
    function_name = str(row.get("function_name") or "").strip()
    shell_verb = str(row.get("shell_verb") or "").strip()
    if function_name and shell_verb:
        return f"{function_name} / {shell_verb}"
    return function_name or shell_verb or str(row.get("agent_tool_intention") or "").strip() or "unknown"


def stable_window_file(session_name: str, prompt_index: str) -> str:
    return f"{safe_slug(session_name, 64)}-p{prompt_index}.json"


def prompt_component_row(row: dict[str, str]) -> dict[str, str]:
    prompt_row = dict(row)
    prompt_row["allocated_input_tokens"] = str(row.get("prompt_input_tokens") or "0")
    prompt_row["allocated_cache_read_tokens"] = str(row.get("prompt_cache_read_tokens") or "0")
    prompt_row["allocated_cache_creation_tokens"] = str(row.get("prompt_cache_creation_tokens") or "0")
    prompt_row["allocated_output_tokens"] = str(row.get("prompt_output_tokens") or "0")
    prompt_row["allocated_reasoning_tokens"] = str(row.get("prompt_reasoning_tokens") or "0")
    prompt_row["allocated_total_tokens"] = str(row.get("prompt_total_tokens") or "0")
    prompt_row["allocated_total_cost_usd"] = str(row.get("prompt_derived_total_cost_usd") or "0")
    return prompt_row


def headline_fields(
    *,
    fresh_input_tokens: float,
    cache_read_tokens: float,
    cache_creation_tokens: float,
    output_tokens: float,
    fresh_input_cost_usd: float,
    cache_read_cost_usd: float,
    cache_creation_cost_usd: float,
    output_cost_usd: float,
) -> dict[str, float]:
    return {
        "headline_context_tokens": fresh_input_tokens + cache_creation_tokens,
        "headline_context_cost_usd": fresh_input_cost_usd + cache_creation_cost_usd,
        "headline_cache_read_tokens": cache_read_tokens,
        "headline_cache_read_cost_usd": cache_read_cost_usd,
        "headline_output_tokens": output_tokens,
        "headline_output_cost_usd": output_cost_usd,
    }


def prompt_headline_totals(row: dict[str, str], pricing_table: dict[str, Any]) -> dict[str, float]:
    prompt_row = prompt_component_row(row)
    costs = component_costs(prompt_row, pricing_table)
    input_tokens = to_float(row.get("prompt_input_tokens"))
    cache_read_tokens = to_float(row.get("prompt_cache_read_tokens"))
    cache_creation_tokens = to_float(row.get("prompt_cache_creation_tokens"))
    output_tokens = to_float(row.get("prompt_output_tokens"))
    fresh_input_tokens = max(0.0, input_tokens - cache_read_tokens - cache_creation_tokens)
    return {
        "prompt_input_tokens": input_tokens,
        "prompt_cache_read_tokens": cache_read_tokens,
        "prompt_cache_creation_tokens": cache_creation_tokens,
        "prompt_output_tokens": output_tokens,
        "prompt_reasoning_tokens": to_float(row.get("prompt_reasoning_tokens")),
        "prompt_total_tokens": to_float(row.get("prompt_total_tokens")),
        "prompt_derived_total_cost_usd": to_float(row.get("prompt_derived_total_cost_usd")),
        **headline_fields(
            fresh_input_tokens=fresh_input_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            output_tokens=output_tokens,
            fresh_input_cost_usd=to_float(costs.get("allocated_fresh_input_cost_usd")),
            cache_read_cost_usd=to_float(costs.get("allocated_cache_read_cost_usd")),
            cache_creation_cost_usd=to_float(costs.get("allocated_cache_creation_cost_usd")),
            output_cost_usd=to_float(costs.get("allocated_output_cost_usd")),
        ),
        "reasoning_tokens_tracked": to_float(row.get("prompt_reasoning_tokens")),
    }


def load_task_categorizer(config_path: str) -> TaskCategorizer:
    config = load_task_categorization_config(config_path)
    for classifier in config.get("classifiers", []) or []:
        if isinstance(classifier, dict) and classifier.get("type") == "codex":
            classifier["enabled"] = False
    return TaskCategorizer(config)


def top_cost_name(rows: list[dict[str, str]], key: str) -> str:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        name = str(row.get(key) or "").strip()
        if not name:
            continue
        totals[name] += to_float(row.get("allocated_total_cost_usd"))
    if not totals:
        return ""
    return sorted(totals.items(), key=lambda item: (-item[1], item[0]))[0][0]
def role_label_for(kind: str) -> str:
    if kind == "user":
        return "User"
    if kind == "message":
        return "Assistant"
    if kind == "tool":
        return "Tool"
    return "Meta"


def load_window_conversation_entries(source_file: str, prompt_index: str) -> list[dict[str, Any]]:
    if not source_file:
        return []
    try:
        source_path = Path(source_file)
        windows = parse_prompt_windows(source_path)
    except Exception:
        return []
    window_number = str(prompt_index)
    window_index = next(
        (index for index, window in enumerate(windows) if str(window.get("prompt_index") or "") == window_number),
        -1,
    )
    if window_index < 0:
        return []
    start_line = to_int(windows[window_index].get("start_line"))
    end_line = to_int(windows[window_index + 1].get("start_line")) - 1 if window_index + 1 < len(windows) else None
    if start_line <= 0:
        return []
    entries: list[dict[str, Any]] = []
    try:
        with source_path.open(encoding="utf-8", errors="replace") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if line_number < start_line:
                    continue
                if end_line is not None and line_number > end_line:
                    break
                stripped = raw.strip()
                if not stripped:
                    continue
                entry = summarize_line(line_number, stripped)
                if entry.get("type") == "invalid-json":
                    continue
                entries.append(
                    {
                        "entry_index": len(entries) + 1,
                        "line_number": line_number,
                        "kind": str(entry.get("kind") or "meta"),
                        "role_label": role_label_for(str(entry.get("kind") or "meta")),
                        "text": str(entry.get("text") or "").strip(),
                        "tool_name": str(entry.get("tool_name") or ""),
                        "command_index": None,
                        "billing_kind": str(entry.get("billing_kind") or "none"),
                    }
                )
    except Exception:
        return []
    return entries



def normalize_command(
    item: ClassifiedRow,
    *,
    pricing_table: dict[str, Any],
    request_pattern: Any,
    task_classification: Any,
    task_label: str,
    task_label_source: str,
    task_label_confidence: str,
    window_file: str,
) -> dict[str, Any]:
    row = dict(item.row)
    row["workflow_phase"] = item.workflow_phase
    row["efficiency_label"] = item.efficiency_label
    normalized = normalize_row(row, pricing_table)
    command = {
        "command_index": normalized["command_index"],
        "cost_usd": normalized["allocated_total_cost_usd"],
        "workflow_phase": item.workflow_phase,
        "efficiency_label": item.efficiency_label,
        "fixing_cause": fixing_cause_for(item.workflow_phase, item.efficiency_label) or "",
        "agent_tool_intention": row.get("agent_tool_intention") or "",
        "function_name": row.get("function_name") or "",
        "shell_verb": row.get("shell_verb") or "",
        "preview": command_preview(row),
        "stdin_preview": compact(row.get("stdin_preview") or "", 220),
        "terminal_context_parent_command_preview": compact(row.get("terminal_context_parent_command_preview") or "", 220),
        "allocated_fresh_input_tokens": normalized["allocated_fresh_input_tokens"],
        "allocated_cache_read_tokens": normalized["allocated_cache_read_tokens"],
        "allocated_cache_creation_tokens": normalized["allocated_cache_creation_tokens"],
        "allocated_output_tokens": normalized["allocated_output_tokens"],
        "allocated_reasoning_tokens": normalized["allocated_reasoning_tokens"],
        "allocated_fresh_input_cost_usd": normalized["allocated_fresh_input_cost_usd"],
        "allocated_cache_read_cost_usd": normalized["allocated_cache_read_cost_usd"],
        "allocated_cache_creation_cost_usd": normalized["allocated_cache_creation_cost_usd"],
        "allocated_output_cost_usd": normalized["allocated_output_cost_usd"],
        **headline_fields(
            fresh_input_tokens=normalized["allocated_fresh_input_tokens"],
            cache_read_tokens=normalized["allocated_cache_read_tokens"],
            cache_creation_tokens=normalized["allocated_cache_creation_tokens"],
            output_tokens=normalized["allocated_output_tokens"],
            fresh_input_cost_usd=normalized["allocated_fresh_input_cost_usd"],
            cache_read_cost_usd=normalized["allocated_cache_read_cost_usd"],
            cache_creation_cost_usd=normalized["allocated_cache_creation_cost_usd"],
            output_cost_usd=normalized["allocated_output_cost_usd"],
        ),
        "request_pattern": request_pattern.request_pattern,
        "task_type": task_classification.task_type,
        "task_type_label": task_classification.task_type_label,
    }
    csv_row = {
        **{column: normalized[column] for column in OUTPUT_COLUMNS},
        "origin": row.get("origin") or "",
        "bucket": row.get("bucket") or "",
        "usage_source": row.get("usage_source") or "",
        "billable_model_source": row.get("billable_model_source") or "",
        "file": row.get("file") or "",
        "session_cwd": row.get("session_cwd") or "",
        "prompt_preview": row.get("prompt_preview") or "",
        "previous_prompt_preview": row.get("previous_prompt_preview") or "",
        "first_prompt_preview": row.get("first_prompt_preview") or "",
        "final_answer_preview": row.get("final_answer_preview") or "",
        "command_preview": command["preview"],
        "workdir": row.get("workdir") or "",
        "target_type": row.get("target_type") or "",
        "target": row.get("target") or "",
        "stdin_preview": row.get("stdin_preview") or "",
        "delegated_agent_action": row.get("delegated_agent_action") or "",
        "delegated_agent_type": row.get("delegated_agent_type") or "",
        "delegated_task_preview": row.get("delegated_task_preview") or "",
        "request_pattern": request_pattern.request_pattern,
        "request_pattern_path": request_pattern.request_pattern_path,
        "request_pattern_depth": request_pattern.request_pattern_depth,
        "request_pattern_rule_id": request_pattern.request_pattern_rule_id,
        "request_pattern_confidence": request_pattern.request_pattern_confidence,
        "request_pattern_config_version": request_pattern.request_pattern_config_version,
        "diagnosis_version": DIAGNOSIS_VERSION,
        "task_type": task_classification.task_type,
        "task_type_label": task_classification.task_type_label,
        "task_type_confidence": task_classification.task_type_confidence,
        "task_type_classifier": task_classification.task_type_classifier,
        "task_type_reason": task_classification.task_type_reason,
        "task_type_source": task_classification.task_type_source,
        "task_type_config_version": task_classification.task_type_config_version,
        "task_label": task_label,
        "task_label_source": task_label_source,
        "task_label_confidence": task_label_confidence,
        "fixing_cause": command["fixing_cause"],
        "headline_context_tokens": command["headline_context_tokens"],
        "headline_context_cost_usd": command["headline_context_cost_usd"],
        "headline_cache_read_tokens": command["headline_cache_read_tokens"],
        "headline_cache_read_cost_usd": command["headline_cache_read_cost_usd"],
        "headline_output_tokens": command["headline_output_tokens"],
        "headline_output_cost_usd": command["headline_output_cost_usd"],
        "window_file": window_file,
    }
    return {
        "classified": item,
        "command": command,
        "csv_row": csv_row,
    }


def segment_timeline(
    command_records: list[dict[str, Any]],
    conversation_entries: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    segments: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_key: tuple[str, str] | None = None
    for record in command_records:
        item = record["classified"]
        key = (item.workflow_phase, item.efficiency_label)
        if current and key != current_key:
            segments.append(current)
            current = []
        current_key = key
        current.append(record)
    if current:
        segments.append(current)

    payload: list[dict[str, Any]] = []
    command_numbers: list[int] = []
    command_to_chunk: dict[int, int] = {}
    for step_index, items in enumerate(segments, start=1):
        first_item = items[0]["classified"]
        commands = [record["command"] for record in items]
        command_numbers.extend(to_int(command.get("command_index")) for command in commands)
        examples: list[str] = []
        for command in commands:
            preview = compact(str(command.get("preview") or ""), 130)
            if preview and preview not in examples:
                examples.append(preview)
            if len(examples) >= 3:
                break
        chunk = {
            "step_index": step_index,
            "display_title": f"Step {step_index} · {first_item.workflow_phase}",
            "workflow_phase": first_item.workflow_phase,
            "efficiency_label": first_item.efficiency_label,
            "fixing_cause": fixing_cause_for(first_item.workflow_phase, first_item.efficiency_label) or "",
            "cost_usd": sum(to_float(command.get("cost_usd")) for command in commands),
            "events": len(commands),
            "start_command_index": commands[0]["command_index"],
            "end_command_index": commands[-1]["command_index"],
            "confidence_counts": dict(Counter(record["classified"].confidence for record in items)),
            "reason_counts": dict(Counter(record["classified"].reason for record in items).most_common(5)),
            "examples": examples,
            "conversation_entries": [],
            "message_preview": "",
            "fresh_input_tokens": sum(to_float(command.get("allocated_fresh_input_tokens")) for command in commands),
            "cache_read_tokens": sum(to_float(command.get("allocated_cache_read_tokens")) for command in commands),
            "cache_creation_tokens": sum(to_float(command.get("allocated_cache_creation_tokens")) for command in commands),
            "output_tokens": sum(to_float(command.get("allocated_output_tokens")) for command in commands),
            "reasoning_tokens": sum(to_float(command.get("allocated_reasoning_tokens")) for command in commands),
            "fresh_input_cost_usd": sum(to_float(command.get("allocated_fresh_input_cost_usd")) for command in commands),
            "cache_read_cost_usd": sum(to_float(command.get("allocated_cache_read_cost_usd")) for command in commands),
            "cache_creation_cost_usd": sum(to_float(command.get("allocated_cache_creation_cost_usd")) for command in commands),
            "output_cost_usd": sum(to_float(command.get("allocated_output_cost_usd")) for command in commands),
            "headline_context_tokens": sum(to_float(command.get("headline_context_tokens")) for command in commands),
            "headline_context_cost_usd": sum(to_float(command.get("headline_context_cost_usd")) for command in commands),
            "headline_cache_read_tokens": sum(to_float(command.get("headline_cache_read_tokens")) for command in commands),
            "headline_cache_read_cost_usd": sum(to_float(command.get("headline_cache_read_cost_usd")) for command in commands),
            "headline_output_tokens": sum(to_float(command.get("headline_output_tokens")) for command in commands),
            "headline_output_cost_usd": sum(to_float(command.get("headline_output_cost_usd")) for command in commands),
        }
        payload.append(chunk)
        for command in commands:
            command_to_chunk[to_int(command.get("command_index"))] = len(payload) - 1

    if not payload:
        return payload

    raw_entries = conversation_entries or []
    raw_command_total = sum(1 for entry in raw_entries if entry.get("billing_kind") == "command")
    raw_command_seen = 0
    csv_command_total = len(command_numbers)
    for entry in raw_entries:
        if entry.get("billing_kind") == "command":
            raw_command_seen += 1
            command_ordinal = raw_command_seen
        elif raw_command_seen == 0:
            command_ordinal = 1
        elif raw_command_total != csv_command_total and raw_command_seen >= raw_command_total:
            command_ordinal = csv_command_total
        else:
            command_ordinal = raw_command_seen
        command_ordinal = min(max(command_ordinal, 1), csv_command_total)
        command_index = command_numbers[command_ordinal - 1]
        chunk_index = command_to_chunk.get(command_index, 0 if command_ordinal == 1 else len(payload) - 1)
        payload[chunk_index]["conversation_entries"].append(
            {
                "entry_index": entry["entry_index"],
                "line_number": entry["line_number"],
                "kind": entry["kind"],
                "role_label": entry["role_label"],
                "text": entry["text"],
                "tool_name": entry["tool_name"],
                "command_index": command_index,
            }
        )

    for chunk in payload:
        preview = next((compact(str(item.get("text") or ""), 180) for item in chunk["conversation_entries"] if str(item.get("text") or "").strip()), "")
        if not preview and chunk["examples"]:
            preview = chunk["examples"][0]
        chunk["message_preview"] = preview or ""
    return payload



def build_window_payload(
    key: tuple[str, str],
    rows: list[dict[str, str]],
    *,
    pricing_table: dict[str, Any],
    request_pattern_categorizer: RequestPatternCategorizer,
    task_categorizer: TaskCategorizer,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    session_name, prompt_index = key
    ordered = sorted(rows, key=lambda row: to_int(row.get("command_index")))
    classified = classify_prompt_window(ordered)
    window_file = stable_window_file(session_name, prompt_index)
    first_row = ordered[0]
    request_pattern = request_pattern_categorizer.classify(first_row)
    task_classification = task_categorizer.classify(first_row)
    task_label, task_label_source, task_label_confidence = derive_task_label(first_row, request_pattern.request_pattern)
    commands = [
        normalize_command(
            item,
            pricing_table=pricing_table,
            request_pattern=request_pattern,
            task_classification=task_classification,
            task_label=task_label,
            task_label_source=task_label_source,
            task_label_confidence=task_label_confidence,
            window_file=window_file,
        )
        for item in classified
    ]
    cause_rollup = fixing_cause_rollup(classified)
    prompt_totals = prompt_headline_totals(first_row, pricing_table)
    command_payloads = [record["command"] for record in commands]
    timeline = segment_timeline(
        commands,
        load_window_conversation_entries(first_row.get("file") or "", prompt_index),
    )
    payload = {
        "window_file": window_file,
        "session_id": session_name,
        "prompt_index": prompt_index,
        "session_date": first_row.get("session_date") or "",
        "source_file": first_row.get("file") or "",
        "short_title": deterministic_short_title(first_row.get("prompt_preview") or first_row.get("first_prompt_preview") or ""),
        "prompt_preview": first_row.get("prompt_preview") or "",
        "previous_prompt_preview": first_row.get("previous_prompt_preview") or "",
        "first_prompt_preview": first_row.get("first_prompt_preview") or "",
        "final_answer_preview": first_row.get("final_answer_preview") or "",
        "session_cwd": first_row.get("session_cwd") or "",
        "request_pattern": request_pattern.request_pattern,
        "request_pattern_path": request_pattern.request_pattern_path,
        "request_pattern_depth": request_pattern.request_pattern_depth,
        "task_type": task_classification.task_type,
        "task_type_label": task_classification.task_type_label,
        "task_label": task_label,
        "dominant_fixing_cause": cause_rollup[0]["cause"] if cause_rollup else "",
        "fixing_cause_rollup": cause_rollup,
        "phase_rollup": rollup_classified(classified, "workflow_phase"),
        "efficiency_rollup": rollup_classified(classified, "efficiency_label"),
        "timeline": timeline,
        **prompt_totals,
        "command_count": len(command_payloads),
        "tool_count": len({tool_label(command) for command in command_payloads}),
        "total_cost_usd": sum(to_float(command.get("cost_usd")) for command in command_payloads),
        "commands": command_payloads,
    }
    window_row = {
        "session_date": payload["session_date"],
        "session_id": payload["session_id"],
        "prompt_index": payload["prompt_index"],
        "window_file": payload["window_file"],
        "short_title": payload["short_title"],
        "source_file": payload["source_file"],
        "model": first_row.get("model") or "",
        "origin": first_row.get("origin") or "",
        "provider": first_row.get("provider") or "",
        "billable_model": first_row.get("billable_model") or "",
        "usage_source": first_row.get("usage_source") or "",
        "bucket": first_row.get("bucket") or "",
        "session_cwd": payload["session_cwd"],
        "prompt_preview": payload["prompt_preview"],
        "previous_prompt_preview": payload["previous_prompt_preview"],
        "first_prompt_preview": payload["first_prompt_preview"],
        "final_answer_preview": payload["final_answer_preview"],
        "request_pattern": payload["request_pattern"],
        "request_pattern_path": payload["request_pattern_path"],
        "request_pattern_depth": payload["request_pattern_depth"],
        "task_type": payload["task_type"],
        "task_type_label": payload["task_type_label"],
        "task_label": payload["task_label"],
        "dominant_fixing_cause": payload["dominant_fixing_cause"],
        "command_count": payload["command_count"],
        "tool_count": payload["tool_count"],
        "total_cost_usd": payload["total_cost_usd"],
        "prompt_input_tokens": payload["prompt_input_tokens"],
        "prompt_cache_read_tokens": payload["prompt_cache_read_tokens"],
        "prompt_cache_creation_tokens": payload["prompt_cache_creation_tokens"],
        "prompt_output_tokens": payload["prompt_output_tokens"],
        "prompt_reasoning_tokens": payload["prompt_reasoning_tokens"],
        "prompt_total_tokens": payload["prompt_total_tokens"],
        "headline_context_tokens": payload["headline_context_tokens"],
        "headline_context_cost_usd": payload["headline_context_cost_usd"],
        "headline_cache_read_tokens": payload["headline_cache_read_tokens"],
        "headline_cache_read_cost_usd": payload["headline_cache_read_cost_usd"],
        "headline_output_tokens": payload["headline_output_tokens"],
        "headline_output_cost_usd": payload["headline_output_cost_usd"],
        "top_function_name": top_cost_name(ordered, "function_name"),
        "top_shell_verb": top_cost_name(ordered, "shell_verb"),
        "fixing_cause_rollup_json": json.dumps(payload["fixing_cause_rollup"], separators=(",", ":"), sort_keys=True),
    }
    return payload, window_row, [record["csv_row"] for record in commands]


def aggregate_values(
    command_rows: list[dict[str, Any]],
    *,
    key: str,
    label_key: str | None = None,
    path_key: str | None = None,
    depth_key: str | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in command_rows:
        value = str(row.get(key) or "").strip()
        if not value:
            continue
        bucket = grouped.setdefault(
            value,
            {
                "value": value,
                "label": str(row.get(label_key) or value) if label_key else value,
                "cost_usd": 0.0,
                "command_count": 0,
                "prompt_windows": set(),
            },
        )
        bucket["cost_usd"] += to_float(row.get("allocated_total_cost_usd"))
        bucket["command_count"] += 1
        bucket["prompt_windows"].add((row.get("session_id"), row.get("prompt_index")))
        if path_key:
            bucket[path_key] = row.get(path_key) or ""
        if depth_key:
            bucket[depth_key] = to_int(row.get(depth_key))
    rows = []
    for bucket in grouped.values():
        rows.append(
            {
                **{key: bucket["value"]},
                **({"label": bucket["label"]} if label_key else {}),
                **({path_key: bucket.get(path_key, "")} if path_key else {}),
                **({depth_key: bucket.get(depth_key, 0)} if depth_key else {}),
                "cost_usd": bucket["cost_usd"],
                "command_count": bucket["command_count"],
                "prompt_window_count": len(bucket["prompt_windows"]),
            }
        )
    rows.sort(key=lambda item: (-to_float(item.get("cost_usd")), str(item.get(key) or "")))
    return rows


def build_summary(window_rows: list[dict[str, Any]], command_rows: list[dict[str, Any]], input_path: Path) -> dict[str, Any]:
    total_cost = sum(to_float(row.get("allocated_total_cost_usd")) for row in command_rows)
    context_cost = sum(to_float(row.get("headline_context_cost_usd")) for row in command_rows)
    cache_read_cost = sum(to_float(row.get("headline_cache_read_cost_usd")) for row in command_rows)
    output_cost = sum(to_float(row.get("headline_output_cost_usd")) for row in command_rows)
    summary = {
        "schema_version": "cost_explorer_v1",
        "source_csv": str(input_path),
        "source_schema_version": SCHEMA_VERSION,
        "source_classification_revision": CLASSIFICATION_REVISION,
        "date_range": {
            "from": min((str(row.get("session_date") or "") for row in window_rows), default=""),
            "to": max((str(row.get("session_date") or "") for row in window_rows), default=""),
        },
        "headline_totals": {
            "total_attributed_cost_usd": total_cost,
            "headline_context_cost_usd": context_cost,
            "headline_cache_read_cost_usd": cache_read_cost,
            "headline_output_cost_usd": output_cost,
            "headline_context_tokens": sum(to_float(row.get("headline_context_tokens")) for row in window_rows),
            "headline_cache_read_tokens": sum(to_float(row.get("headline_cache_read_tokens")) for row in window_rows),
            "headline_output_tokens": sum(to_float(row.get("headline_output_tokens")) for row in window_rows),
            "reasoning_tokens_tracked": sum(to_float(row.get("prompt_reasoning_tokens")) for row in window_rows),
            "prompt_window_count": len(window_rows),
            "command_count": len(command_rows),
        },
        "fixing_causes": aggregate_values(command_rows, key="fixing_cause"),
        "task_categories": aggregate_values(command_rows, key="task_type", label_key="task_type_label"),
        "request_patterns": aggregate_values(
            command_rows,
            key="request_pattern",
            path_key="request_pattern_path",
            depth_key="request_pattern_depth",
        ),
        "tool_hotspots": {
            "agent_tool_intentions": aggregate_values(command_rows, key="agent_tool_intention"),
            "function_names": aggregate_values(command_rows, key="function_name"),
            "shell_verbs": aggregate_values(command_rows, key="shell_verb"),
            "models": aggregate_values(command_rows, key="model"),
            "origins": aggregate_values(command_rows, key="origin"),
        },
        "token_composition": {
            "headline_context_cost_usd": context_cost,
            "headline_cache_read_cost_usd": cache_read_cost,
            "headline_output_cost_usd": output_cost,
            "headline_context_tokens": sum(to_float(row.get("headline_context_tokens")) for row in command_rows),
            "headline_cache_read_tokens": sum(to_float(row.get("headline_cache_read_tokens")) for row in command_rows),
            "headline_output_tokens": sum(to_float(row.get("headline_output_tokens")) for row in command_rows),
            "reasoning_tokens_tracked": sum(to_float(row.get("allocated_reasoning_tokens")) for row in command_rows),
            "rows": [
                {
                    "token_bucket": "context",
                    "label": "Context / prompt-window cost",
                    "cost_usd": context_cost,
                    "tokens": sum(to_float(row.get("headline_context_tokens")) for row in command_rows),
                },
                {
                    "token_bucket": "cache_read",
                    "label": "Cache-read cost",
                    "cost_usd": cache_read_cost,
                    "tokens": sum(to_float(row.get("headline_cache_read_tokens")) for row in command_rows),
                },
                {
                    "token_bucket": "output",
                    "label": "Output cost",
                    "cost_usd": output_cost,
                    "tokens": sum(to_float(row.get("headline_output_tokens")) for row in command_rows),
                },
            ],
        },
        "filter_options": {
            "fixing_cause": aggregate_values(command_rows, key="fixing_cause"),
            "task_type": aggregate_values(command_rows, key="task_type", label_key="task_type_label"),
            "request_pattern": aggregate_values(
                command_rows,
                key="request_pattern",
                path_key="request_pattern_path",
                depth_key="request_pattern_depth",
            ),
            "agent_tool_intention": aggregate_values(command_rows, key="agent_tool_intention"),
            "function_name": aggregate_values(command_rows, key="function_name"),
            "shell_verb": aggregate_values(command_rows, key="shell_verb"),
            "model": aggregate_values(command_rows, key="model"),
            "origin": aggregate_values(command_rows, key="origin"),
        },
    }
    return summary


def summary_markdown(summary: dict[str, Any]) -> str:
    totals = summary["headline_totals"]
    lines = [
        "# Cost explorer summary",
        "",
        f"- Source CSV: `{summary['source_csv']}`",
        f"- Date range: `{summary['date_range']['from']}` → `{summary['date_range']['to']}`",
        f"- Total attributed cost: **{money(to_float(totals['total_attributed_cost_usd']))}**",
        f"- Context / prompt-window cost: **{money(to_float(totals['headline_context_cost_usd']))}**",
        f"- Cache-read cost: **{money(to_float(totals['headline_cache_read_cost_usd']))}**",
        f"- Output cost: **{money(to_float(totals['headline_output_cost_usd']))}**",
        f"- Prompt windows: **{to_int(totals['prompt_window_count'])}**",
        f"- Commands: **{to_int(totals['command_count'])}**",
        "",
        "## Fixing / CI issues",
    ]
    for row in summary["fixing_causes"][:8]:
        lines.append(
            f"- {row['fixing_cause']}: {money(to_float(row['cost_usd']))} across {to_int(row['prompt_window_count'])} windows / {to_int(row['command_count'])} commands"
        )
    lines.extend(["", "## Task categories"])
    for row in summary["task_categories"][:8]:
        lines.append(
            f"- {row['label']} ({row['task_type']}): {money(to_float(row['cost_usd']))} across {to_int(row['prompt_window_count'])} windows"
        )
    lines.extend(["", "## Request patterns"])
    for row in summary["request_patterns"][:8]:
        lines.append(
            f"- {row['request_pattern']}: {money(to_float(row['cost_usd']))} across {to_int(row['prompt_window_count'])} windows"
        )
    lines.extend(["", "## Tool hotspots"])
    for section_name in ("function_names", "shell_verbs", "agent_tool_intentions"):
        lines.append(f"### {section_name.replace('_', ' ').title()}")
        for row in summary["tool_hotspots"][section_name][:5]:
            key = "function_name" if section_name == "function_names" else "shell_verb" if section_name == "shell_verbs" else "agent_tool_intention"
            lines.append(f"- {row[key]}: {money(to_float(row['cost_usd']))}")
    return "\n".join(lines) + "\n"


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_window_jsons(windows_dir: Path, payloads: list[dict[str, Any]]) -> None:
    windows_dir.mkdir(parents=True, exist_ok=True)
    for payload in payloads:
        (windows_dir / str(payload["window_file"])).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def static_window_row(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field, "") for field in STATIC_WINDOW_ROW_FIELDS}


def static_command_row(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field, "") for field in STATIC_COMMAND_ROW_FIELDS}


def write_js_assignment(path: Path, target: str, payload: Any) -> None:
    path.write_text(f"{target} = {json.dumps(payload, indent=2, sort_keys=True)};\n", encoding="utf-8")


def write_static_explorer(output_dir: Path, summary: dict[str, Any], window_rows: list[dict[str, Any]], command_rows: list[dict[str, Any]], payloads: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "explorer.html").write_text(COST_EXPLORER_HTML.read_text(encoding="utf-8"), encoding="utf-8")
    write_js_assignment(output_dir / "summary.js", "window.__COST_EXPLORER_STATIC_SUMMARY__", summary)
    write_js_assignment(
        output_dir / "window-rows.js",
        "window.__COST_EXPLORER_STATIC_WINDOW_ROWS__",
        [static_window_row(row) for row in window_rows],
    )
    write_js_assignment(
        output_dir / "command-rows.js",
        "window.__COST_EXPLORER_STATIC_COMMAND_ROWS__",
        [static_command_row(row) for row in command_rows],
    )
    windows_js_dir = output_dir / "windows-js"
    windows_js_dir.mkdir(parents=True, exist_ok=True)
    for payload in payloads:
        window_file = str(payload["window_file"])
        script = (
            "window.__COST_EXPLORER_STATIC_WINDOWS__ = window.__COST_EXPLORER_STATIC_WINDOWS__ || {};\n"
            f"window.__COST_EXPLORER_STATIC_WINDOWS__[{json.dumps(window_file)}] = "
            f"{json.dumps(payload, indent=2, sort_keys=True)};\n"
        )
        (windows_js_dir / f"{window_file}.js").write_text(script, encoding="utf-8")

def main() -> int:
    args = parse_args()
    rows = source_rows(args.input)
    if not rows:
        raise RuntimeError(f"no {SCHEMA_VERSION} rows found in {args.input}")
    window_groups = prompt_windows(rows)
    if not window_groups:
        raise RuntimeError(f"no prompt windows found in {args.input}")

    request_pattern_categorizer = RequestPatternCategorizer(load_request_pattern_config(args.request_pattern_config))
    task_categorizer = load_task_categorizer(args.task_categorization_config)
    pricing_table = load_pricing_table(args.pricing_table or None)

    payloads: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    command_rows: list[dict[str, Any]] = []
    for key, window_rows_raw in window_groups:
        payload, window_row, window_command_rows = build_window_payload(
            key,
            window_rows_raw,
            pricing_table=pricing_table,
            request_pattern_categorizer=request_pattern_categorizer,
            task_categorizer=task_categorizer,
        )
        payloads.append(payload)
        window_rows.append(window_row)
        command_rows.extend(window_command_rows)

    if not payloads:
        raise RuntimeError(f"no prompt windows found in {args.input}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_window_jsons(args.output_dir / "windows", payloads)
    write_csv(args.output_dir / "windows.csv", WINDOWS_COLUMNS, window_rows)
    write_csv(args.output_dir / "commands.csv", COMMANDS_COLUMNS, command_rows)

    summary = build_summary(window_rows, command_rows, args.input)
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output_dir / "summary.md").write_text(summary_markdown(summary), encoding="utf-8")
    write_static_explorer(args.output_dir, summary, window_rows, command_rows, payloads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
