#!/usr/bin/env python3
"""Planning vs execution report across Codex + Claude + combined.

Outputs:
- reports/planning-vs-execution-report.json
- reports/planning-vs-execution-sessions.csv
- reports/planning-vs-execution-prompts.csv
- reports/planning-vs-execution-tool-breakdown.csv
- reports/planning-vs-execution-summary.md
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shlex
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from usage_costing import (  # noqa: E402
    DEFAULT_PRICING_URL,
    default_billable_model,
    derive_cost,
    load_pricing_table,
    provider_for_session_family,
    resolve_billable_model,
)
DEFAULT_AUDIT_REPORT = REPO_ROOT / "cache-hit-audit-report.json"
DEFAULT_CODEX_DIR = Path.home() / ".codex" / "sessions"
DEFAULT_CLAUDE_DIR = Path.home() / ".claude" / "projects"

PLANNING_PHRASES = [
    "plan-to-invoker",
    "/plan-to-invoker",
    "submit to invoker",
    "create invoker plan",
    "convert to invoker",
]
PLANNING_RE = re.compile("|".join(re.escape(p) for p in PLANNING_PHRASES), re.IGNORECASE)
SHELL_FUNCTION_NAMES = {"exec_command", "shell", "bash"}
COMMAND_ATTRIBUTION_SCHEMA_VERSION = "usage_command_attribution_v4"
COMMAND_ATTRIBUTION_SCHEMA_VERSION_V4_1 = "usage_command_attribution_v4_1"
COMMAND_ATTRIBUTION_SCHEMA_VERSION_V4_2 = "usage_command_attribution_v4_2"
COMMAND_ATTRIBUTION_SCHEMA_VERSION_V4_3 = "usage_command_attribution_v4_3"
COMMAND_ATTRIBUTION_SCHEMA_VERSION_V4_4 = "usage_command_attribution_v4_4"
COMMAND_ATTRIBUTION_SERVICE_CLASSIFIER_REVISION = "service_context_v2"
COMMAND_ATTRIBUTION_CLASSIFIER_REVISION_V4_2 = "classifier_v4_2"
COMMAND_ATTRIBUTION_CLASSIFIER_REVISION_V4_3 = "classifier_v4_3"
COMMAND_ATTRIBUTION_CLASSIFIER_REVISION_V4_4 = "classifier_v4_4"
COMMAND_COST_ALLOCATION_METHOD = "prompt_cost_output_weighted_v1"

PRIMARY_WHY_BUCKETS_V4_2 = {
    "generated_invoker_task",
    "invoker_auto_fix",
    "invoker_task_failure_fix",
    "merge_failure_fix",
    "invoker_create_pr",
    "ci_failure_fix",
    "branch_stack_maintenance",
    "previous_agent_plan",
    "prompt_file_task_needs_review",
    "human_direct_request",
    "needs_review",
    "needs_review_low_cost",
}
PROMPT_TASK_KIND_BUCKETS_V4_2 = {
    "implementation",
    "failure_diagnosis",
    "test_validation",
    "pr_review",
    "pr_authoring",
    "branch_stack",
    "analytics_reporting",
    "visual_proof",
    "environment_setup",
    "skill_plugin",
    "planning",
    "simple_request",
    "needs_review",
    "needs_review_low_cost",
}
AGENT_TOOL_INTENTION_BUCKETS_V4_2 = {
    "repo_orientation",
    "environment_initialization",
    "failure_diagnosis_inspection",
    "implementation_planning_inspection",
    "diff_review",
    "ci_monitoring",
    "analytics_inspection",
    "bug_fix_edit",
    "feature_implementation_edit",
    "refactor_edit",
    "test_or_proof_edit",
    "documentation_edit",
    "generated_artifact_edit",
    "failure_reproduction",
    "test_execution",
    "full_validation",
    "pr_creation_or_update",
    "remote_orchestration",
    "process_control",
    "planning_or_task_tracking",
    "analytics_reporting",
    "needs_review",
    "needs_review_low_cost",
}
AGENT_TOOL_INTENTION_BUCKETS_V4_3 = AGENT_TOOL_INTENTION_BUCKETS_V4_2 | {
    "branch_stack_orchestration",
}
AGENT_TOOL_INTENTION_BUCKETS_V4_4 = AGENT_TOOL_INTENTION_BUCKETS_V4_3 | {
    "fixing_failure",
}
AGENT_TOOL_INTENTION_SOURCES_V4_2 = {
    "preceding_assistant_message",
    "command_mechanics",
    "prompt_context",
    "codex_cluster_review",
    "needs_review",
}
AGENT_TOOL_INTENTION_SOURCES_V4_3 = {
    "delegated_task_message",
    "delegated_agent_context",
    "command_mechanics",
    "prompt_context",
    "needs_review",
}
TOOL_EXECUTION_MODES_V4_3 = {
    "direct_tool",
    "agent_delegated",
    "remote_command",
    "process_control",
    "needs_review",
}
DELEGATED_AGENT_ACTIONS_V4_3 = {"none", "spawn", "send_input", "wait", "resume", "close"}


@dataclass
class PromptWindow:
    prompt_index: int
    prompt_text: str
    previous_prompt: str = ""
    first_prompt: str = ""
    final_answer: str = ""
    input_delta: int = 0
    cached_delta: int = 0
    cache_creation_delta: int = 0
    output_delta: int = 0
    reasoning_delta: int = 0
    total_delta: int = 0
    tool_calls: int = 0
    agent_messages: int = 0
    response_messages: int = 0
    function_outputs: int = 0
    function_name_counts: Counter[str] = field(default_factory=Counter)
    shell_verb_counts: Counter[str] = field(default_factory=Counter)
    command_calls: list["CommandCall"] = field(default_factory=list)


@dataclass
class CommandCall:
    command_index: int
    function_name: str
    call_id: str = ""
    command_text: str = ""
    command_preview: str = ""
    command_hash: str = ""
    shell_verb: str = ""
    workdir: str = ""
    target_type: str = ""
    target: str = ""
    output_chars: int = 0
    output_token_estimate: int = 0
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    delegated_agent_action: str = "none"
    delegated_agent_id: str = ""
    delegated_agent_type: str = ""
    delegated_agent_nickname: str = ""
    delegated_task_preview: str = ""
    delegated_task_hash: str = ""


@dataclass
class SessionStats:
    model: str  # codex | claude
    file: str
    session_date: str
    bucket: str  # planning | execution
    provider: str = ""
    billable_model: str = ""
    billable_model_source: str = ""
    usage_source: str = ""
    user_prompts: int = 0
    agent_messages: int = 0
    tool_calls: int = 0
    function_outputs: int = 0
    final_input: int = 0
    final_cached: int = 0
    final_cache_creation: int = 0
    final_output: int = 0
    final_reasoning: int = 0
    final_total: int = 0
    session_cwd: str = ""
    first_prompt: str = ""
    prompt_windows: list[PromptWindow] = field(default_factory=list)
    function_name_counts: Counter[str] = field(default_factory=Counter)
    shell_verb_counts: Counter[str] = field(default_factory=Counter)


def safe_div(num: float, denom: float) -> float:
    return float(num / denom) if denom else 0.0


def shorten(text: str, n: int = 220) -> str:
    return " ".join(text.split())[:n]


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assistant_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
    return "\n".join(part for part in parts if part.strip())


def attach_prompt_context(windows: list[PromptWindow], first_prompt: str) -> None:
    previous = ""
    for window in windows:
        window.previous_prompt = previous
        window.first_prompt = first_prompt
        previous = window.prompt_text


def iso_date(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.astimezone(timezone.utc).date().isoformat()


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def session_distribution(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0, "sum": 0.0, "mean": 0.0, "median": 0.0, "p75": 0.0, "p90": 0.0, "p95": 0.0, "max": 0.0, "min": 0.0}
    return {
        "count": len(values),
        "sum": float(sum(values)),
        "mean": float(statistics.mean(values)),
        "median": float(statistics.median(values)),
        "p75": float(percentile(values, 0.75)),
        "p90": float(percentile(values, 0.90)),
        "p95": float(percentile(values, 0.95)),
        "max": float(max(values)),
        "min": float(min(values)),
    }


def pareto_cut(rows: list[dict[str, Any]], key: str, threshold: float = 0.80) -> dict[str, Any]:
    sorted_rows = sorted(rows, key=lambda r: r.get(key, 0), reverse=True)
    total = sum(r.get(key, 0) for r in sorted_rows) or 0.0
    selected: list[dict[str, Any]] = []
    cumulative = 0.0
    for row in sorted_rows:
        cumulative += row.get(key, 0)
        selected.append(row)
        if total and cumulative / total >= threshold:
            break
    return {
        "key": key,
        "threshold": threshold,
        "total": float(total),
        "selected_count": len(selected),
        "selected_share": safe_div(cumulative, total),
        "selected": selected,
    }


def extract_shell_verb(arguments_raw: Any) -> str | None:
    if not arguments_raw:
        return None
    if isinstance(arguments_raw, str):
        try:
            args_obj = json.loads(arguments_raw)
        except json.JSONDecodeError:
            return None
    elif isinstance(arguments_raw, dict):
        args_obj = arguments_raw
    else:
        return None
    cmd = args_obj.get("cmd") or args_obj.get("command") or args_obj.get("input", {}).get("command") or ""
    if not isinstance(cmd, str):
        return None
    cmd = cmd.strip()
    if not cmd:
        return None
    tokens = [t for t in cmd.split() if t]
    if not tokens:
        return None
    verb = tokens[0]
    if verb == "cd" and len(tokens) >= 4 and tokens[2] in {"&&", "||", ";"}:
        verb = tokens[3]
    if verb.startswith("/"):
        return verb.split("/")[-1]
    return verb


def parse_tool_arguments(arguments_raw: Any) -> dict[str, Any]:
    if not arguments_raw:
        return {}
    if isinstance(arguments_raw, str):
        try:
            obj = json.loads(arguments_raw)
        except json.JSONDecodeError:
            return {}
    elif isinstance(arguments_raw, dict):
        obj = arguments_raw
    else:
        return {}
    return obj if isinstance(obj, dict) else {}


def command_text_from_arguments(arguments_raw: Any) -> str:
    args_obj = parse_tool_arguments(arguments_raw)
    command = args_obj.get("cmd") or args_obj.get("command") or (args_obj.get("input") or {}).get("command")
    return command if isinstance(command, str) else ""


def workdir_from_arguments(arguments_raw: Any) -> str:
    args_obj = parse_tool_arguments(arguments_raw)
    value = args_obj.get("workdir") or args_obj.get("cwd") or (args_obj.get("input") or {}).get("cwd")
    return value if isinstance(value, str) else ""


def target_from_tool_arguments(arguments_raw: Any) -> tuple[str, str]:
    args_obj = parse_tool_arguments(arguments_raw)
    for key in ("file_path", "path", "target", "uri", "url", "query"):
        value = args_obj.get(key)
        if isinstance(value, str) and value.strip():
            target_type = "search_query" if key == "query" else "path"
            return target_type, value.strip()[:180]
    nested = args_obj.get("input")
    if isinstance(nested, dict):
        for key in ("file_path", "path", "target", "uri", "url", "query"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                target_type = "search_query" if key == "query" else "path"
                return target_type, value.strip()[:180]
    return "", ""


def delegated_message_from_arguments(args_obj: dict[str, Any]) -> str:
    message = args_obj.get("message")
    if isinstance(message, str):
        return message
    items = args_obj.get("items")
    if isinstance(items, list):
        parts: list[str] = []
        for item in items:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    nested = args_obj.get("input")
    if isinstance(nested, dict):
        return delegated_message_from_arguments(nested)
    return ""


def delegated_target_ids_from_arguments(args_obj: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("target", "id", "agent_id"):
        if key in args_obj:
            values.append(args_obj.get(key))
    for key in ("targets", "ids", "agent_ids"):
        if key in args_obj:
            values.append(args_obj.get(key))
    nested = args_obj.get("input")
    if isinstance(nested, dict):
        values.extend(delegated_target_ids_from_arguments(nested))
    ids: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            ids.append(value.strip())
        elif isinstance(value, list):
            ids.extend(str(item).strip() for item in value if str(item).strip())
    return list(dict.fromkeys(ids))


def delegated_metadata_from_arguments(function_name: str, args_obj: dict[str, Any]) -> dict[str, str]:
    fn = function_name.lower()
    action_by_tool = {
        "spawn_agent": "spawn",
        "send_input": "send_input",
        "wait_agent": "wait",
        "resume_agent": "resume",
        "close_agent": "close",
    }
    action = action_by_tool.get(fn, "none")
    message = delegated_message_from_arguments(args_obj) if fn in {"spawn_agent", "send_input"} else ""
    target_ids = delegated_target_ids_from_arguments(args_obj)
    return {
        "delegated_agent_action": action,
        "delegated_agent_id": target_ids[0] if target_ids else "",
        "delegated_agent_type": str(args_obj.get("agent_type") or args_obj.get("type") or ""),
        "delegated_agent_nickname": "",
        "delegated_task_preview": shorten(message, 280),
        "delegated_task_hash": digest_text(message) if message else "",
    }


def delegated_metadata_from_output(output: str) -> dict[str, str]:
    text = output.strip()
    metadata = {"delegated_agent_id": "", "delegated_agent_nickname": ""}
    if not text:
        return metadata
    parsed: Any = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                parsed = None
    if isinstance(parsed, dict):
        for key in ("agent_id", "id"):
            if isinstance(parsed.get(key), str):
                metadata["delegated_agent_id"] = parsed[key]
                break
        for key in ("nickname", "name"):
            if isinstance(parsed.get(key), str):
                metadata["delegated_agent_nickname"] = parsed[key]
                break
    if not metadata["delegated_agent_id"]:
        match = re.search(r"\bagent[_ -]?id[\"':=\s]+([A-Za-z0-9_.:-]+)", text, re.IGNORECASE)
        if match:
            metadata["delegated_agent_id"] = match.group(1)
    if not metadata["delegated_agent_nickname"]:
        match = re.search(r"\bnickname[\"':=\s]+([A-Za-z0-9_.:-]+)", text, re.IGNORECASE)
        if match:
            metadata["delegated_agent_nickname"] = match.group(1)
    return metadata


def output_text_from_payload(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("output", "stdout", "stderr", "content", "text"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
        return json.dumps(value, sort_keys=True)
    return str(value)


def command_target(command: str, workdir: str) -> tuple[str, str]:
    if not command.strip():
        return "", ""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return "", ""
    verb = tokens[0].split("/")[-1]
    path_flags = {"-C", "--work-tree", "--git-dir", "-f", "--file", "-p", "--project", "--filter"}
    candidates: list[str] = []
    for index, token in enumerate(tokens[1:], start=1):
        if token in path_flags and index + 1 < len(tokens):
            candidates.append(tokens[index + 1])
            continue
        if token.startswith("-") or token in {"&&", "||", ";", "|"}:
            continue
        if "/" in token or token.startswith(".") or token.endswith((".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml", ".md")):
            candidates.append(token)
    if verb == "git" and len(tokens) > 1:
        sub = tokens[1]
        if sub in {"status", "branch", "log", "show", "diff"}:
            return "git", sub
    if candidates:
        target = candidates[0]
        if workdir and target.startswith("."):
            target = str(Path(workdir) / target)
        return "path", target[:180]
    return "verb", verb


def classify_command_why(function_name: str, shell_verb: str, command: str, target: str, request_pattern: str) -> tuple[str, str]:
    text = " ".join([function_name, shell_verb, command, target, request_pattern]).lower()
    rules: list[tuple[str, list[str]]] = [
        ("autofix_or_failure_repair", ["autofix", "fix", "repair", "rerun", "failure", "failed"]),
        ("ci_log_diagnosis", ["ci", "gh run", "workflow", "actions", "log"]),
        ("test_or_build_execution", ["pytest", "pnpm test", "npm test", "yarn test", "make test", "cargo test", "go test", "build", "tsc"]),
        ("source_inspection", ["rg ", "grep", "sed ", "cat ", "nl ", "less ", "head ", "tail ", "find ", "ls "]),
        ("git_branch_stack_ops", ["git ", "rebase", "merge", "branch", "checkout", "cherry-pick", "worktree"]),
        ("remote_machine_orchestration", ["ssh ", "rsync", "scp ", "remote", "tmux"]),
        ("db_or_state_inspection", ["sqlite", "psql", "mysql", "redis", "state", "database", ".db"]),
        ("visual_proof_or_ui_debug", ["playwright", "screenshot", "video", "browser", "ui", "visual"]),
        ("dependency_setup", ["npm install", "pnpm install", "pip install", "bundle install", "uv sync", "composer install"]),
        ("reporting_or_analytics", ["report", "metrics", "mixpanel", "analytics", "csv", "json summary"]),
    ]
    for why, needles in rules:
        if any(needle in text for needle in needles):
            return why, "rules_v1"
    return "uncategorized", "rules_v1"


def command_attribution_tool_action(function_name: str, shell_verb: str, command: str) -> tuple[str, str]:
    fn = function_name.lower()
    verb = shell_verb.lower()
    text = command.lower()
    if fn in {"read", "cat"} or verb in {"cat", "sed", "nl", "less", "head", "tail"}:
        return "file_read", "tool_rule"
    if fn in {"glob", "toolsearch", "grep", "search"} or verb in {"rg", "grep", "find", "fd", "ls"}:
        return "file_search", "tool_rule"
    if fn in {"edit", "write", "apply_patch", "patch"} or verb in {"apply_patch"}:
        return "source_modification", "tool_rule"
    if fn == "write_stdin":
        return "terminal_input", "tool_rule"
    if fn in {"spawn_agent", "wait_agent", "agent", "send_input", "resume_agent", "close_agent"}:
        return "agent_delegation", "tool_rule"
    if fn in {"todowrite", "taskupdate", "taskcreate", "update_plan"}:
        return "planning_or_task_tracking", "tool_rule"
    if fn in {"run_query", "dashboard", "property", "insights_query"} or any(
        needle in text for needle in ("mixpanel", "analytics", "dashboard", "run_query")
    ):
        return "analytics_query", "tool_rule"
    if fn in SHELL_FUNCTION_NAMES:
        trivial = {"pwd", "echo", "printf", "sleep", "ps", "kill", "jobs", "fg", "bg", "true", "false", "date"}
        if verb in trivial:
            return "environment_or_process_control", "tool_rule"
        return "terminal_command", "tool_rule"
    return "unknown_tool", "tool_rule"


def service_context_from_text(text: str) -> tuple[str, str, str]:
    haystack = text.lower()
    service_rules: list[tuple[str, str, str, list[str]]] = [
        ("autofix_or_failure_repair", "high", "prompt_context", ["autofix", "fix the code", "failed", "failure", "repair", "retry", "rerun"]),
        ("ci_log_diagnosis", "high", "prompt_context", ["ci", "github actions", "workflow", "gh run", "build log"]),
        ("test_or_build_execution", "high", "prompt_context", ["pytest", "npm test", "pnpm test", "yarn test", "make test", "cargo test", "go test", "build", "tsc"]),
        ("pr_review", "high", "prompt_context", ["pull request", "pr body", "pr review", "review", "release note", "changelog"]),
        ("reporting_or_analytics", "high", "prompt_context", ["report", "metrics", "mixpanel", "analytics", "dashboard", "csv"]),
        ("implementation", "medium", "prompt_context", ["implement", "add ", "update ", "change ", "refactor", "feature"]),
        ("planning_or_task_tracking", "medium", "prompt_context", ["plan", "tasks", "todo", "checklist"]),
    ]
    for why, confidence, source, needles in service_rules:
        if any(needle in haystack for needle in needles):
            return why, confidence, source
    return "uncategorized", "low", "missing_context"


def classify_command_service_of(
    row: dict[str, Any],
    tool_action: str,
    previous_context: dict[str, str] | None = None,
) -> tuple[str, str, str, str]:
    primary_why = str(row.get("primary_why") or "uncategorized")
    fn = str(row.get("function_name") or "").lower()
    if fn == "write_stdin" and previous_context and previous_context.get("service_of_why"):
        return previous_context["service_of_why"], "high", "previous_command", ""

    context_text = " ".join(
        str(row.get(key) or "")
        for key in ("prompt_preview", "previous_prompt_preview", "first_prompt_preview", "final_answer_preview", "request_pattern", "command_preview", "target")
    )
    service_of_why, confidence, source = service_context_from_text(context_text)
    if service_of_why != "uncategorized":
        return service_of_why, confidence, source, ""

    if primary_why != "uncategorized" and primary_why != "source_inspection":
        return primary_why, "high", "tool_rule", ""

    if tool_action in {"planning_or_task_tracking", "analytics_query", "environment_or_process_control"}:
        mapped = {
            "planning_or_task_tracking": "planning_or_task_tracking",
            "analytics_query": "reporting_or_analytics",
            "environment_or_process_control": "environment_or_process_control",
        }[tool_action]
        return mapped, "medium", "tool_rule", ""
    if primary_why == "source_inspection":
        return "source_inspection", "low", "tool_rule", ""
    if tool_action == "unknown_tool":
        return "uncategorized", "low", "tool_rule", "unknown_tool"
    if not context_text.strip():
        return "uncategorized", "low", "missing_context", "missing_context"
    return "uncategorized", "low", "missing_context", "missing_context"


def build_command_attribution_v4_1_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    previous_by_prompt: dict[tuple[str, Any, Any], dict[str, str]] = {}
    for row in rows:
        out = dict(row)
        out["schema_version"] = COMMAND_ATTRIBUTION_SCHEMA_VERSION_V4_1
        out["service_classifier_revision"] = COMMAND_ATTRIBUTION_SERVICE_CLASSIFIER_REVISION
        session_id = Path(str(row.get("file") or "")).stem or digest_text(str(row.get("file") or ""))[:16]
        out["session_id"] = session_id
        tool_action, tool_action_source = command_attribution_tool_action(
            str(row.get("function_name") or ""),
            str(row.get("shell_verb") or ""),
            str(row.get("command_preview") or ""),
        )
        key = (str(row.get("file") or ""), row.get("bucket"), row.get("prompt_index"))
        service_of_why, service_confidence, service_source, uncategorized_reason = classify_command_service_of(
            out,
            tool_action,
            previous_by_prompt.get(key),
        )
        out["tool_action"] = tool_action
        out["tool_action_source"] = tool_action_source
        out["service_of_why"] = service_of_why
        out["service_of_confidence"] = service_confidence
        out["service_of_source"] = service_source
        out["uncategorized_reason"] = uncategorized_reason if service_of_why == "uncategorized" else ""
        out["session_root_cause_summary"] = f"{service_of_why} / {tool_action}"
        if tool_action in {"terminal_command", "terminal_input"} or service_of_why != "uncategorized":
            previous_by_prompt[key] = {
                "primary_why": str(out.get("primary_why") or ""),
                "service_of_why": service_of_why,
                "tool_action": tool_action,
            }
        enriched.append(out)
    return enriched


def _text_has_any(text: str, needles: list[str]) -> bool:
    lowered = text.lower()
    return any(needle in lowered for needle in needles)


def _prompt_context_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("prompt_preview", "previous_prompt_preview", "first_prompt_preview", "final_answer_preview", "request_pattern")
    )


def classify_primary_why_v4_2(row: dict[str, Any]) -> tuple[str, str]:
    prompt = _prompt_context_text(row)
    prompt_lower = prompt.lower()
    if prompt.startswith("A previous agent produced the plan below"):
        return "previous_agent_plan", "exact_previous_agent_plan_prefix"
    if prompt.startswith("Generated task for invoker") or prompt.startswith("Invoker generated task"):
        return "generated_invoker_task", "exact_invoker_generated_prefix"
    if prompt.startswith("Autofix for invoker") or prompt.startswith("Invoker autofix"):
        return "invoker_auto_fix", "exact_invoker_autofix_prefix"
    if prompt.startswith("Fix invoker task failure") or prompt.startswith("Invoker task failed"):
        return "invoker_task_failure_fix", "exact_invoker_failure_prefix"
    if prompt.startswith("Resolve merge failure") or prompt.startswith("Fix merge failure"):
        return "merge_failure_fix", "exact_merge_failure_prefix"
    if prompt.startswith("Create PR body for invoker") or prompt.startswith("Invoker create PR"):
        return "invoker_create_pr", "exact_invoker_pr_prefix"
    if "/tmp/invoker-agent-prompt-" in prompt_lower:
        return "prompt_file_task_needs_review", "invoker_prompt_file_wrapper"
    if _text_has_any(prompt_lower, ["failed checks", "github actions", "gh run", "ci failure", "workflow failed"]):
        return "ci_failure_fix", "strong_ci_terms"
    if _text_has_any(prompt_lower, ["rebase", "upstream/master", "stack maintenance", "branch stack", "stacked pr"]):
        return "branch_stack_maintenance", "strong_branch_stack_terms"
    if _text_has_any(prompt_lower, ["merge conflict", "merge failed", "merge failure"]):
        return "merge_failure_fix", "merge_failure_terms"
    if _text_has_any(prompt_lower, ["autofix", "auto-fix"]):
        return "invoker_auto_fix", "autofix_terms"
    if prompt.strip():
        return "human_direct_request", "human_prompt_fallback"
    return "needs_review", "missing_prompt_context"


def classify_prompt_task_kind_v4_2(row: dict[str, Any]) -> tuple[str, str]:
    text = " ".join([_prompt_context_text(row), str(row.get("command_preview") or ""), str(row.get("target") or "")]).lower()
    ordered_rules: list[tuple[str, list[str], str]] = [
        ("visual_proof", ["visual proof", "screenshot", "playwright", "video"], "visual_terms"),
        ("pr_authoring", ["pr body", "pull request body", "create pr", "gh pr create"], "pr_authoring_terms"),
        ("pr_review", ["review this pr", "pr review", "pull request review"], "pr_review_terms"),
        ("branch_stack", ["rebase", "upstream/master", "branch stack", "stacked pr", "cherry-pick"], "branch_stack_terms"),
        ("analytics_reporting", ["mixpanel", "analytics", "metrics", "report", "csv"], "analytics_terms"),
        ("skill_plugin", ["skill", "plugin", ".codex-plugin"], "skill_plugin_terms"),
        ("environment_setup", ["install", "dependency", "setup", "env file", "configuration"], "environment_terms"),
        ("test_validation", ["run tests", "test validation", "pytest", "pnpm test", "npm test", "make test", "full validation"], "test_terms"),
        ("failure_diagnosis", ["diagnose", "investigate", "failure", "failed", "repro", "log"], "failure_terms"),
        ("planning", ["plan", "planning", "todo", "checklist"], "planning_terms"),
        ("implementation", ["implement", "fix", "update", "add ", "change", "refactor"], "implementation_terms"),
        ("simple_request", ["what is", "show me", "print ", "date"], "simple_request_terms"),
    ]
    for bucket, needles, reason in ordered_rules:
        if _text_has_any(text, needles):
            return bucket, reason
    if text.strip():
        return "implementation", "default_nonempty_prompt"
    return "needs_review", "missing_prompt_context"


def classify_agent_tool_intention_v4_2(row: dict[str, Any]) -> tuple[str, str]:
    fn = str(row.get("function_name") or "").lower()
    verb = str(row.get("shell_verb") or "").lower()
    command = str(row.get("command_preview") or "").lower()
    target = str(row.get("target") or "").lower()
    prompt = _prompt_context_text(row).lower()
    text = " ".join([fn, verb, command, target, prompt])

    if fn in {"spawn_agent", "send_input", "wait_agent", "resume_agent", "close_agent"}:
        return "remote_orchestration", "agent_orchestration_tool"
    if fn in {"todowrite", "taskupdate", "taskcreate", "update_plan"}:
        return "planning_or_task_tracking", "planning_tool"
    if fn == "write_stdin":
        return "process_control", "terminal_stdin"
    if fn in {"run_query", "dashboard", "property", "insights_query"}:
        return "analytics_inspection", "analytics_tool_or_terms"
    if _text_has_any(text, ["gh pr create", "gh pr edit", "pull request", "pr body"]):
        return "pr_creation_or_update", "pr_terms"
    if _text_has_any(text, ["gh run", "github actions", "failed checks", "workflow"]):
        return "ci_monitoring", "ci_terms"
    if _text_has_any(text, ["ssh ", "rsync", "scp ", "remote", "worktree"]):
        return "remote_orchestration", "remote_terms"
    if verb in {"pwd", "echo", "printf", "sleep", "ps", "kill", "jobs", "fg", "bg", "true", "false", "date"}:
        return "process_control", "trivial_process_command"
    if _text_has_any(text, ["pytest", "pnpm test", "npm test", "yarn test", "make test", "cargo test", "go test"]):
        return "test_execution", "test_command"
    if _text_has_any(text, ["build", "tsc", "lint", "test:all"]):
        return "full_validation", "validation_command"

    is_edit = fn in {"edit", "write", "apply_patch", "patch"} or verb == "apply_patch"
    if is_edit:
        if _text_has_any(text, ["docs/", ".md", "readme", "documentation"]):
            return "documentation_edit", "documentation_edit_terms"
        if _text_has_any(text, ["generated", "artifact", "csv", "json summary", "report"]):
            return "generated_artifact_edit", "generated_artifact_terms"
        if _text_has_any(text, ["refactor", "rename", "cleanup"]):
            return "refactor_edit", "refactor_terms"
        if _text_has_any(text, ["fix ", "fixing", "bug", "failure", "failed", "regression"]):
            return "bug_fix_edit", "bug_fix_terms"
        if _text_has_any(text, ["test", "proof", "fixture", "golden"]):
            return "test_or_proof_edit", "test_or_proof_edit_terms"
        return "feature_implementation_edit", "default_edit"

    is_read_or_search = fn in {"read", "cat", "glob", "toolsearch", "grep", "search"} or verb in {"rg", "grep", "find", "fd", "ls", "cat", "sed", "nl", "less", "head", "tail"}
    if _text_has_any(text, ["mixpanel", "analytics", "dashboard"]):
        return "analytics_inspection", "analytics_terms"
    if is_read_or_search:
        if _text_has_any(text, ["git diff", "diff", "review"]):
            return "diff_review", "diff_terms"
        if _text_has_any(text, ["failure", "failed", "error", "diagnose", "repro"]):
            return "failure_diagnosis_inspection", "failure_inspection_terms"
        if _text_has_any(text, ["implement", "update", "change", "plan", "architecture"]):
            return "implementation_planning_inspection", "implementation_inspection_terms"
        return "repo_orientation", "default_read_or_search"

    if _text_has_any(text, ["repro", "reproduce", "failed output"]):
        return "failure_reproduction", "reproduction_terms"
    if fn in SHELL_FUNCTION_NAMES:
        return "environment_initialization", "default_terminal_command"
    return "needs_review", "unknown_tool"


def classify_agent_tool_intention_from_text_v4_3(text: str) -> tuple[str, str]:
    lowered = text.lower()
    if _text_has_any(lowered, ["rebase", "cherry-pick", "cherrypick", "worktree", "branch stack", "stacked pr", "stacked branch"]):
        return "branch_stack_orchestration", "branch_stack_terms"
    if _text_has_any(lowered, ["open pr", "create pr", "update pr", "pr body", "pull request"]):
        return "pr_creation_or_update", "pr_terms"
    if _text_has_any(lowered, ["full validation", "test:all", "lint", "typecheck", "build"]):
        return "full_validation", "validation_terms"
    if _text_has_any(lowered, ["run tests", "pytest", "pnpm test", "npm test", "yarn test", "make test", "verify"]):
        return "test_execution", "test_terms"
    if _text_has_any(lowered, ["fix bug", "bug", "failure", "failed", "regression", "repair"]):
        return "bug_fix_edit", "bug_fix_terms"
    if _text_has_any(lowered, ["refactor", "rename", "cleanup"]):
        return "refactor_edit", "refactor_terms"
    if _text_has_any(lowered, ["documentation", "docs", "readme"]):
        return "documentation_edit", "documentation_terms"
    if _text_has_any(lowered, ["implement", "add ", "update ", "feature", "change"]):
        return "feature_implementation_edit", "implementation_terms"
    if _text_has_any(lowered, ["investigate", "diagnose", "inspect", "find why"]):
        return "failure_diagnosis_inspection", "inspection_terms"
    return "needs_review", "missing_delegated_intent_terms"


FAILURE_REPAIR_TERMS_V4_4 = [
    "build/test command failed",
    "fix the code so the command succeeds",
    "failed checks",
    "failing test",
    "failing tests",
    "failed test",
    "failed tests",
    "regression",
    "ci failure",
    "workflow failed",
    "build failed",
    "test failed",
    "tests failed",
]
BRANCH_STACK_TEXT_TERMS_V4_4 = [
    "git rebase",
    "git cherry-pick",
    "git cherrypick",
    "upstream/master",
    "rebase stack",
    "branch stack",
    "stacked pr",
    "stacked branch",
]
BRANCH_STACK_TARGET_TERMS_V4_4 = [
    "branch-stack",
    "branch_stack",
    "stacked-pr",
    "stacked_pr",
    "stacked-branch",
    "stacked_branch",
]
MERGIFY_QUEUE_TERMS_V4_4 = ["mergify", "merge queue", "merge-queue", "auto-merge queue"]
MERGIFY_QUEUE_OP_TERMS_V4_4 = [
    "enqueue",
    "dequeue",
    "requeue",
    "queue orchestration",
    "orchestrate",
    "operate",
    "manage",
    "merge queue",
    "merge-queue",
    "auto-merge queue",
]


def _has_failure_repair_context_v4_4(text: str) -> bool:
    lowered = text.lower()
    return _text_has_any(lowered, FAILURE_REPAIR_TERMS_V4_4)


def _has_branch_stack_orchestration_terms_v4_4(text: str, include_mergify: bool = True) -> bool:
    lowered = text.lower()
    if _text_has_any(lowered, BRANCH_STACK_TEXT_TERMS_V4_4):
        return True
    if include_mergify and _text_has_any(lowered, MERGIFY_QUEUE_TERMS_V4_4):
        return _text_has_any(lowered, MERGIFY_QUEUE_OP_TERMS_V4_4)
    return False


def _has_branch_stack_target_terms_v4_4(target: str) -> bool:
    lowered = target.lower()
    return _text_has_any(lowered, BRANCH_STACK_TARGET_TERMS_V4_4)


def classify_agent_tool_intention_from_text_v4_4(text: str) -> tuple[str, str]:
    lowered = text.lower()
    if _has_failure_repair_context_v4_4(lowered):
        return "fixing_failure", "failure_repair_terms"
    if _has_branch_stack_orchestration_terms_v4_4(lowered):
        return "branch_stack_orchestration", "explicit_branch_stack_or_queue_terms"
    if _text_has_any(lowered, ["open pr", "create pr", "update pr", "pr body", "pull request"]):
        return "pr_creation_or_update", "pr_terms"
    if _text_has_any(lowered, ["full validation", "test:all", "lint", "typecheck", "build"]):
        return "full_validation", "validation_terms"
    if _text_has_any(lowered, ["run tests", "pytest", "pnpm test", "npm test", "yarn test", "make test", "verify"]):
        return "test_execution", "test_terms"
    if _text_has_any(lowered, ["fix bug", "bug", "failure", "failed", "repair"]):
        return "bug_fix_edit", "bug_fix_terms"
    if _text_has_any(lowered, ["refactor", "rename", "cleanup"]):
        return "refactor_edit", "refactor_terms"
    if _text_has_any(lowered, ["documentation", "docs", "readme"]):
        return "documentation_edit", "documentation_terms"
    if _text_has_any(lowered, ["implement", "add ", "update ", "feature", "change"]):
        return "feature_implementation_edit", "implementation_terms"
    if _text_has_any(lowered, ["investigate", "diagnose", "inspect", "find why"]):
        return "failure_diagnosis_inspection", "inspection_terms"
    return "needs_review", "missing_delegated_intent_terms"


def tool_execution_mode_v4_3(row: dict[str, Any]) -> tuple[str, str]:
    fn = str(row.get("function_name") or "").lower()
    verb = str(row.get("shell_verb") or "").lower()
    command = str(row.get("command_preview") or "").lower()
    text = " ".join([fn, verb, command])
    if fn in {"spawn_agent", "send_input", "wait_agent", "resume_agent", "close_agent"}:
        return "agent_delegated", "agent_control_tool"
    if _text_has_any(text, ["ssh ", "scp ", "rsync"]):
        return "remote_command", "remote_command_terms"
    if fn == "write_stdin" or verb in {"pwd", "echo", "printf", "sleep", "ps", "kill", "jobs", "fg", "bg", "true", "false", "date"}:
        return "process_control", "process_control_terms"
    return "direct_tool", "default_direct_tool"


def classify_agent_tool_intention_v4_3(row: dict[str, Any]) -> tuple[str, str, str]:
    fn = str(row.get("function_name") or "").lower()
    delegated_text = str(row.get("delegated_task_preview") or "")
    if fn in {"spawn_agent", "send_input"} and delegated_text:
        intention, reason = classify_agent_tool_intention_from_text_v4_3(delegated_text)
        source = "delegated_task_message" if intention != "needs_review" else "needs_review"
        return intention, source, reason
    if _text_has_any(
        " ".join(str(row.get(key) or "") for key in ("command_preview", "target", "prompt_preview")).lower(),
        ["rebase", "cherry-pick", "cherrypick", "worktree", "branch stack", "stacked pr"],
    ):
        return "branch_stack_orchestration", "command_mechanics", "branch_stack_terms"
    intention, reason = classify_agent_tool_intention_v4_2(row)
    if intention == "remote_orchestration" and _text_has_any(
        " ".join(str(row.get(key) or "") for key in ("function_name", "shell_verb", "command_preview")).lower(),
        ["ssh ", "scp ", "rsync"],
    ):
        return "remote_orchestration", "command_mechanics", reason
    return intention, "command_mechanics" if intention != "needs_review" else "needs_review", reason


def classify_agent_tool_intention_v4_4(row: dict[str, Any]) -> tuple[str, str, str]:
    fn = str(row.get("function_name") or "").lower()
    delegated_text = str(row.get("delegated_task_preview") or "")
    prompt_text = str(row.get("prompt_preview") or "")
    command_text = str(row.get("command_preview") or "")
    target_text = str(row.get("target") or "")

    if fn in {"spawn_agent", "send_input"} and delegated_text:
        intention, reason = classify_agent_tool_intention_from_text_v4_4(delegated_text)
        source = "delegated_task_message" if intention != "needs_review" else "needs_review"
        return intention, source, reason

    prompt_and_delegated = " ".join([prompt_text, delegated_text])
    command_and_prompt = " ".join([command_text, prompt_text, delegated_text])
    queue_command = _has_branch_stack_orchestration_terms_v4_4(command_text)
    if _has_failure_repair_context_v4_4(prompt_and_delegated) and not queue_command:
        return "fixing_failure", "prompt_context", "failure_repair_terms"
    if _has_branch_stack_orchestration_terms_v4_4(command_and_prompt) or _has_branch_stack_target_terms_v4_4(target_text):
        return "branch_stack_orchestration", "command_mechanics", "explicit_branch_stack_or_queue_terms"

    intention, reason = classify_agent_tool_intention_v4_2(row)
    if intention == "remote_orchestration" and _text_has_any(
        " ".join(str(row.get(key) or "") for key in ("function_name", "shell_verb", "command_preview")).lower(),
        ["ssh ", "scp ", "rsync"],
    ):
        return "remote_orchestration", "command_mechanics", reason
    if intention == "remote_orchestration" and reason == "remote_terms":
        verb = str(row.get("shell_verb") or "").lower()
        fn_name = str(row.get("function_name") or "").lower()
        command_only = " ".join(str(row.get(key) or "") for key in ("function_name", "shell_verb", "command_preview")).lower()
        if verb in {"pwd", "echo", "printf", "sleep", "ps", "kill", "jobs", "fg", "bg", "true", "false", "date"}:
            return "process_control", "command_mechanics", "trivial_process_command"
        if _text_has_any(command_only, ["pytest", "pnpm test", "npm test", "yarn test", "make test", "cargo test", "go test"]):
            return "test_execution", "command_mechanics", "test_command"
        if _text_has_any(command_only, ["build", "tsc", "lint", "test:all"]):
            return "full_validation", "command_mechanics", "validation_command"
        if fn_name in {"read", "cat", "glob", "toolsearch", "grep", "search"} or verb in {"rg", "grep", "find", "fd", "ls", "cat", "sed", "nl", "less", "head", "tail"}:
            return "repo_orientation", "command_mechanics", "default_read_or_search"
        without_target = dict(row)
        without_target["target"] = ""
        without_target["workdir"] = ""
        without_target["prompt_preview"] = ""
        retry_intention, retry_reason = classify_agent_tool_intention_v4_2(without_target)
        if retry_intention != "remote_orchestration":
            return retry_intention, "command_mechanics" if retry_intention != "needs_review" else "needs_review", retry_reason
    return intention, "command_mechanics" if intention != "needs_review" else "needs_review", reason


def _cluster_key_v4_2(row: dict[str, Any]) -> str:
    parts = [
        shorten(str(row.get("prompt_preview") or ""), 100).lower(),
        str(row.get("function_name") or "").lower(),
        str(row.get("shell_verb") or "").lower(),
        shorten(str(row.get("command_preview") or ""), 100).lower(),
        shorten(str(row.get("target") or ""), 80).lower(),
    ]
    return digest_text("\n".join(parts))[:16]


def _coerce_cluster_labels(cluster_labels: dict[str, dict[str, str]] | None, cluster_key: str, deterministic: dict[str, str]) -> dict[str, str]:
    if cluster_labels is None:
        return {
            "primary_why": deterministic["primary_why"],
            "prompt_task_kind": deterministic["prompt_task_kind"],
            "agent_tool_intention": deterministic["agent_tool_intention"],
        }
    return cluster_labels.get(cluster_key, {})


def _validate_cluster_labels_v4_2(cluster_labels: dict[str, dict[str, str]] | None) -> None:
    if not cluster_labels:
        return
    field_allowed = {
        "primary_why": PRIMARY_WHY_BUCKETS_V4_2,
        "prompt_task_kind": PROMPT_TASK_KIND_BUCKETS_V4_2,
        "agent_tool_intention": AGENT_TOOL_INTENTION_BUCKETS_V4_2,
    }
    unapproved: list[str] = []
    for cluster_key, labels in cluster_labels.items():
        for field, allowed in field_allowed.items():
            value = labels.get(field, "")
            if value and value not in allowed:
                unapproved.append(f"{cluster_key}.{field}={value}")
    if unapproved:
        raise ValueError(f"Unapproved v4.2 classifier bucket(s): {', '.join(unapproved)}")


def _agreement_confidence(deterministic_value: str, codex_value: str, allowed: set[str]) -> tuple[str, str]:
    if codex_value and codex_value not in allowed:
        return "needs_review", "codex_suggested_unapproved_bucket"
    if deterministic_value == codex_value and deterministic_value not in {"needs_review", "needs_review_low_cost"}:
        return "high", "deterministic_and_codex_cluster_agree"
    if not codex_value:
        return "needs_review", "missing_codex_cluster_label"
    return "needs_review", "deterministic_codex_disagreement"


def _finalize_value_for_confidence(value: str, confidence: str, low_cost: bool) -> str:
    if confidence == "high":
        return value
    return "needs_review_low_cost" if low_cost else "needs_review"


def build_command_attribution_v4_2_rows(
    rows: list[dict[str, Any]],
    cluster_labels: dict[str, dict[str, str]] | None = None,
    low_cost_threshold_usd: float = 0.01,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _validate_cluster_labels_v4_2(cluster_labels)
    enriched: list[dict[str, Any]] = []
    ambiguous_clusters: dict[str, dict[str, Any]] = {}
    for row in rows:
        out = {
            key: value
            for key, value in row.items()
            if key not in {"prompt_primary_why", "row_primary_why", "why_tags", "why_classifier", "tool_action", "tool_action_source", "service_of_why", "service_of_confidence", "service_of_source", "uncategorized_reason", "session_root_cause_summary"}
        }
        primary_why, primary_reason = classify_primary_why_v4_2(row)
        prompt_task_kind, task_reason = classify_prompt_task_kind_v4_2(row)
        agent_tool_intention, intention_reason = classify_agent_tool_intention_v4_2(row)
        deterministic = {
            "primary_why": primary_why,
            "prompt_task_kind": prompt_task_kind,
            "agent_tool_intention": agent_tool_intention,
        }
        cluster_key = _cluster_key_v4_2(row)
        codex = _coerce_cluster_labels(cluster_labels, cluster_key, deterministic)
        primary_conf, primary_review = _agreement_confidence(primary_why, codex.get("primary_why", ""), PRIMARY_WHY_BUCKETS_V4_2)
        task_conf, task_review = _agreement_confidence(prompt_task_kind, codex.get("prompt_task_kind", ""), PROMPT_TASK_KIND_BUCKETS_V4_2)
        intention_conf, intention_review = _agreement_confidence(
            agent_tool_intention,
            codex.get("agent_tool_intention", ""),
            AGENT_TOOL_INTENTION_BUCKETS_V4_2,
        )
        review_reasons = [reason for reason in (primary_review, task_review, intention_review) if reason != "deterministic_and_codex_cluster_agree"]
        allocated_cost = float(row.get("allocated_total_cost_usd") or 0.0)
        low_cost = allocated_cost <= low_cost_threshold_usd
        out["schema_version"] = COMMAND_ATTRIBUTION_SCHEMA_VERSION_V4_2
        out["classification_revision"] = COMMAND_ATTRIBUTION_CLASSIFIER_REVISION_V4_2
        out["classification_cluster_key"] = cluster_key
        out["classification_agreement"] = "agree" if not review_reasons else "needs_review"
        out["review_reason"] = ";".join(dict.fromkeys(review_reasons))
        out["primary_why"] = _finalize_value_for_confidence(primary_why, primary_conf, low_cost)
        out["prompt_task_kind"] = _finalize_value_for_confidence(prompt_task_kind, task_conf, low_cost)
        out["agent_tool_intention"] = _finalize_value_for_confidence(agent_tool_intention, intention_conf, low_cost)
        out["agent_tool_intention_source"] = "command_mechanics" if intention_conf == "high" else "needs_review"
        out["primary_why_confidence"] = primary_conf
        out["prompt_task_kind_confidence"] = task_conf
        out["agent_tool_intention_confidence"] = intention_conf
        out["deterministic_primary_why"] = primary_why
        out["deterministic_prompt_task_kind"] = prompt_task_kind
        out["deterministic_agent_tool_intention"] = agent_tool_intention
        out["codex_primary_why"] = codex.get("primary_why", "")
        out["codex_prompt_task_kind"] = codex.get("prompt_task_kind", "")
        out["codex_agent_tool_intention"] = codex.get("agent_tool_intention", "")
        if review_reasons:
            cluster = ambiguous_clusters.setdefault(
                cluster_key,
                {
                    "classification_cluster_key": cluster_key,
                    "proposed_primary_why": primary_why,
                    "proposed_prompt_task_kind": prompt_task_kind,
                    "proposed_agent_tool_intention": agent_tool_intention,
                    "codex_primary_why": codex.get("primary_why", ""),
                    "codex_prompt_task_kind": codex.get("prompt_task_kind", ""),
                    "codex_agent_tool_intention": codex.get("agent_tool_intention", ""),
                    "examples": shorten(str(row.get("prompt_preview") or ""), 220),
                    "row_count": 0,
                    "allocated_total_cost_usd": 0.0,
                    "reason_for_review": out["review_reason"],
                },
            )
            cluster["row_count"] += 1
            cluster["allocated_total_cost_usd"] += allocated_cost
        enriched.append(out)

    review_rows = sorted(ambiguous_clusters.values(), key=lambda item: item["allocated_total_cost_usd"], reverse=True)
    total_review_cost = sum(float(row.get("allocated_total_cost_usd") or 0.0) for row in review_rows)
    cumulative = 0.0
    selected_review_rows: list[dict[str, Any]] = []
    for row in review_rows:
        cumulative += float(row.get("allocated_total_cost_usd") or 0.0)
        row["ambiguous_cost_coverage"] = safe_div(cumulative, total_review_cost)
        selected_review_rows.append(row)
        if total_review_cost and cumulative / total_review_cost >= 0.95:
            break
    return enriched, selected_review_rows


def build_command_attribution_v4_3_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    enriched: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    delegated_agent_context: dict[str, dict[str, str]] = {}
    for row in rows:
        out = {
            key: value
            for key, value in row.items()
            if key not in {"prompt_primary_why", "row_primary_why", "why_tags", "why_classifier", "tool_action", "tool_action_source", "service_of_why", "service_of_confidence", "service_of_source", "uncategorized_reason", "session_root_cause_summary"}
        }
        fn = str(row.get("function_name") or "").lower()
        primary_why, primary_reason = classify_primary_why_v4_2(row)
        prompt_task_kind, task_reason = classify_prompt_task_kind_v4_2(row)
        execution_mode, execution_reason = tool_execution_mode_v4_3(row)
        intention, intention_source, intention_reason = classify_agent_tool_intention_v4_3(row)
        delegated_action = str(row.get("delegated_agent_action") or "none")
        delegated_id = str(row.get("delegated_agent_id") or "")
        if fn in {"wait_agent", "resume_agent", "close_agent"}:
            context = delegated_agent_context.get(delegated_id)
            if context:
                intention = context["agent_tool_intention"]
                prompt_task_kind = context["prompt_task_kind"]
                intention_source = "delegated_agent_context"
                intention_reason = "target_agent_context"
                out["delegated_agent_type"] = out.get("delegated_agent_type") or context.get("delegated_agent_type", "")
                out["delegated_agent_nickname"] = out.get("delegated_agent_nickname") or context.get("delegated_agent_nickname", "")
                out["delegated_task_preview"] = out.get("delegated_task_preview") or context.get("delegated_task_preview", "")
                out["delegated_task_hash"] = out.get("delegated_task_hash") or context.get("delegated_task_hash", "")
            else:
                intention = "needs_review"
                intention_source = "needs_review"
                intention_reason = "missing_delegated_agent_context"
        if fn == "spawn_agent" and delegated_id:
            delegated_agent_context[delegated_id] = {
                "agent_tool_intention": intention,
                "prompt_task_kind": prompt_task_kind,
                "delegated_agent_type": str(out.get("delegated_agent_type") or ""),
                "delegated_agent_nickname": str(out.get("delegated_agent_nickname") or ""),
                "delegated_task_preview": str(out.get("delegated_task_preview") or ""),
                "delegated_task_hash": str(out.get("delegated_task_hash") or ""),
            }

        review_reasons: list[str] = []
        if intention not in AGENT_TOOL_INTENTION_BUCKETS_V4_3:
            review_reasons.append(f"unapproved_agent_tool_intention:{intention}")
            intention = "needs_review"
            intention_source = "needs_review"
        if execution_mode not in TOOL_EXECUTION_MODES_V4_3:
            review_reasons.append(f"unapproved_tool_execution_mode:{execution_mode}")
            execution_mode = "needs_review"
        if delegated_action not in DELEGATED_AGENT_ACTIONS_V4_3:
            review_reasons.append(f"unapproved_delegated_agent_action:{delegated_action}")
            delegated_action = "none"
        if intention == "needs_review":
            review_reasons.append(intention_reason)
        cluster_key = _cluster_key_v4_2(row)
        agreement = "needs_review" if review_reasons else "agree"
        out["schema_version"] = COMMAND_ATTRIBUTION_SCHEMA_VERSION_V4_3
        out["classification_revision"] = COMMAND_ATTRIBUTION_CLASSIFIER_REVISION_V4_3
        out["classification_cluster_key"] = cluster_key
        out["classification_agreement"] = agreement
        out["review_reason"] = ";".join(dict.fromkeys(reason for reason in review_reasons if reason))
        out["primary_why"] = primary_why
        out["prompt_task_kind"] = prompt_task_kind
        out["agent_tool_intention"] = intention
        out["agent_tool_intention_source"] = intention_source
        out["tool_execution_mode"] = execution_mode
        out["tool_execution_mode_source"] = execution_reason
        out["delegated_agent_action"] = delegated_action
        out["primary_why_confidence"] = "high" if primary_why != "needs_review" else "needs_review"
        out["prompt_task_kind_confidence"] = "high" if prompt_task_kind != "needs_review" else "needs_review"
        out["agent_tool_intention_confidence"] = "high" if intention != "needs_review" else "needs_review"
        out["deterministic_primary_why"] = primary_why
        out["deterministic_prompt_task_kind"] = prompt_task_kind
        out["deterministic_agent_tool_intention"] = intention
        out["deterministic_tool_execution_mode"] = execution_mode
        out["codex_primary_why"] = primary_why
        out["codex_prompt_task_kind"] = prompt_task_kind
        out["codex_agent_tool_intention"] = intention
        enriched.append(out)
        if agreement == "needs_review":
            review_rows.append(
                {
                    "classification_cluster_key": cluster_key,
                    "proposed_primary_why": primary_why,
                    "proposed_prompt_task_kind": prompt_task_kind,
                    "proposed_agent_tool_intention": intention,
                    "tool_execution_mode": execution_mode,
                    "delegated_agent_action": delegated_action,
                    "examples": shorten(str(row.get("prompt_preview") or row.get("delegated_task_preview") or ""), 220),
                    "row_count": 1,
                    "allocated_total_cost_usd": float(row.get("allocated_total_cost_usd") or 0.0),
                    "reason_for_review": out["review_reason"],
                }
            )
    return enriched, review_rows


def build_command_attribution_v4_4_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    enriched: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    delegated_agent_context: dict[str, dict[str, str]] = {}
    for row in rows:
        out = {
            key: value
            for key, value in row.items()
            if key not in {"prompt_primary_why", "row_primary_why", "why_tags", "why_classifier", "tool_action", "tool_action_source", "service_of_why", "service_of_confidence", "service_of_source", "uncategorized_reason", "session_root_cause_summary"}
        }
        fn = str(row.get("function_name") or "").lower()
        primary_why, primary_reason = classify_primary_why_v4_2(row)
        prompt_task_kind, task_reason = classify_prompt_task_kind_v4_2(row)
        execution_mode, execution_reason = tool_execution_mode_v4_3(row)
        intention, intention_source, intention_reason = classify_agent_tool_intention_v4_4(row)
        delegated_action = str(row.get("delegated_agent_action") or "none")
        delegated_id = str(row.get("delegated_agent_id") or "")
        if fn in {"wait_agent", "resume_agent", "close_agent"}:
            context = delegated_agent_context.get(delegated_id)
            if context:
                intention = context["agent_tool_intention"]
                prompt_task_kind = context["prompt_task_kind"]
                intention_source = "delegated_agent_context"
                intention_reason = "target_agent_context"
                out["delegated_agent_type"] = out.get("delegated_agent_type") or context.get("delegated_agent_type", "")
                out["delegated_agent_nickname"] = out.get("delegated_agent_nickname") or context.get("delegated_agent_nickname", "")
                out["delegated_task_preview"] = out.get("delegated_task_preview") or context.get("delegated_task_preview", "")
                out["delegated_task_hash"] = out.get("delegated_task_hash") or context.get("delegated_task_hash", "")
            else:
                intention = "needs_review"
                intention_source = "needs_review"
                intention_reason = "missing_delegated_agent_context"
        if fn == "spawn_agent" and delegated_id:
            delegated_agent_context[delegated_id] = {
                "agent_tool_intention": intention,
                "prompt_task_kind": prompt_task_kind,
                "delegated_agent_type": str(out.get("delegated_agent_type") or ""),
                "delegated_agent_nickname": str(out.get("delegated_agent_nickname") or ""),
                "delegated_task_preview": str(out.get("delegated_task_preview") or ""),
                "delegated_task_hash": str(out.get("delegated_task_hash") or ""),
            }

        review_reasons: list[str] = []
        if intention not in AGENT_TOOL_INTENTION_BUCKETS_V4_4:
            review_reasons.append(f"unapproved_agent_tool_intention:{intention}")
            intention = "needs_review"
            intention_source = "needs_review"
        if execution_mode not in TOOL_EXECUTION_MODES_V4_3:
            review_reasons.append(f"unapproved_tool_execution_mode:{execution_mode}")
            execution_mode = "needs_review"
        if delegated_action not in DELEGATED_AGENT_ACTIONS_V4_3:
            review_reasons.append(f"unapproved_delegated_agent_action:{delegated_action}")
            delegated_action = "none"
        if intention == "needs_review":
            review_reasons.append(intention_reason)
        cluster_key = _cluster_key_v4_2(row)
        agreement = "needs_review" if review_reasons else "agree"
        out["schema_version"] = COMMAND_ATTRIBUTION_SCHEMA_VERSION_V4_4
        out["classification_revision"] = COMMAND_ATTRIBUTION_CLASSIFIER_REVISION_V4_4
        out["classification_cluster_key"] = cluster_key
        out["classification_agreement"] = agreement
        out["review_reason"] = ";".join(dict.fromkeys(reason for reason in review_reasons if reason))
        out["primary_why"] = primary_why
        out["prompt_task_kind"] = prompt_task_kind
        out["agent_tool_intention"] = intention
        out["agent_tool_intention_source"] = intention_source
        out["tool_execution_mode"] = execution_mode
        out["tool_execution_mode_source"] = execution_reason
        out["delegated_agent_action"] = delegated_action
        out["primary_why_confidence"] = "high" if primary_why != "needs_review" else "needs_review"
        out["prompt_task_kind_confidence"] = "high" if prompt_task_kind != "needs_review" else "needs_review"
        out["agent_tool_intention_confidence"] = "high" if intention != "needs_review" else "needs_review"
        out["deterministic_primary_why"] = primary_why
        out["deterministic_prompt_task_kind"] = prompt_task_kind
        out["deterministic_agent_tool_intention"] = intention
        out["deterministic_tool_execution_mode"] = execution_mode
        out["codex_primary_why"] = primary_why
        out["codex_prompt_task_kind"] = prompt_task_kind
        out["codex_agent_tool_intention"] = intention
        enriched.append(out)
        if agreement == "needs_review":
            review_rows.append(
                {
                    "classification_cluster_key": cluster_key,
                    "proposed_primary_why": primary_why,
                    "proposed_prompt_task_kind": prompt_task_kind,
                    "proposed_agent_tool_intention": intention,
                    "tool_execution_mode": execution_mode,
                    "delegated_agent_action": delegated_action,
                    "examples": shorten(str(row.get("prompt_preview") or row.get("delegated_task_preview") or ""), 220),
                    "row_count": 1,
                    "allocated_total_cost_usd": float(row.get("allocated_total_cost_usd") or 0.0),
                    "reason_for_review": out["review_reason"],
                }
            )
    return enriched, review_rows


def load_v4_2_cluster_labels(path: str | Path | None) -> dict[str, dict[str, str]] | None:
    if not path:
        return None
    label_path = Path(path)
    if not label_path.exists():
        raise FileNotFoundError(f"Missing v4.2 cluster labels file: {label_path}")
    raw = json.loads(label_path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("v4.2 cluster labels must be a JSON object keyed by classification_cluster_key")
    labels: dict[str, dict[str, str]] = {}
    for cluster_key, value in raw.items():
        if not isinstance(cluster_key, str) or not isinstance(value, dict):
            raise ValueError("v4.2 cluster labels must map string cluster keys to label objects")
        labels[cluster_key] = {str(k): str(v) for k, v in value.items() if v is not None}
    _validate_cluster_labels_v4_2(labels)
    return labels


def add_command_call(window: PromptWindow | None, function_name: str, arguments: Any, call_id: str = "") -> CommandCall | None:
    if window is None:
        return None
    args_obj = parse_tool_arguments(arguments)
    command = command_text_from_arguments(arguments)
    delegated = delegated_metadata_from_arguments(function_name, args_obj)
    if not command and delegated["delegated_task_preview"]:
        command = delegated["delegated_task_preview"]
    workdir = workdir_from_arguments(arguments)
    shell_verb = extract_shell_verb(arguments) or ""
    target_type, target = command_target(command, workdir)
    if not target:
        target_type, target = target_from_tool_arguments(arguments)
    if not target and delegated["delegated_agent_id"]:
        target_type, target = "agent_id", delegated["delegated_agent_id"]
    call = CommandCall(
        command_index=len(window.command_calls) + 1,
        function_name=function_name,
        call_id=call_id,
        command_text=command,
        command_preview=shorten(command, 180),
        command_hash=digest_text(command) if command else "",
        shell_verb=shell_verb,
        workdir=workdir,
        target_type=target_type,
        target=target,
        tool_arguments=args_obj,
        **delegated,
    )
    window.command_calls.append(call)
    return call


def attach_command_output(window: PromptWindow | None, call_id: str, output: str) -> None:
    if window is None or not output:
        return
    candidates = window.command_calls
    if call_id:
        candidates = [call for call in window.command_calls if call.call_id == call_id]
    if not candidates:
        candidates = [call for call in window.command_calls if call.output_chars == 0]
    if not candidates:
        return
    call = candidates[-1]
    call.output_chars += len(output)
    call.output_token_estimate += max(1, round(len(output) / 4))
    if call.function_name == "spawn_agent":
        metadata = delegated_metadata_from_output(output)
        if metadata["delegated_agent_id"]:
            call.delegated_agent_id = metadata["delegated_agent_id"]
            if not call.target:
                call.target_type = "agent_id"
                call.target = metadata["delegated_agent_id"]
        if metadata["delegated_agent_nickname"]:
            call.delegated_agent_nickname = metadata["delegated_agent_nickname"]


def raw_total_tokens(
    model: str,
    *,
    input_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
    provider_total_tokens: int,
) -> int:
    if provider_total_tokens:
        return provider_total_tokens
    if model == "codex":
        return input_tokens + output_tokens + reasoning_tokens
    return input_tokens + cache_read_tokens + cache_creation_tokens + output_tokens + reasoning_tokens


def load_model_totals(audit_path: Path) -> dict[str, dict[str, float]]:
    obj = json.loads(audit_path.read_text())
    by_host_c = obj.get("baselineCcusage", {}).get("codexDailyByHost", {})
    by_host_a = obj.get("baselineCcusage", {}).get("claudeDailyByHost", {})
    codex = by_host_c.get("local_this_machine", {})
    claude = by_host_a.get("local_this_machine", {})
    return {
        "codex": {
            "inputTokens": float(codex.get("inputTokens") or 0),
            "cachedInputTokens": float(codex.get("cachedInputTokens") or 0),
            "cacheCreationTokens": 0.0,
            "outputTokens": float(codex.get("outputTokens") or 0),
            "reasoningOutputTokens": float(codex.get("reasoningOutputTokens") or 0),
            "costUSD": float(codex.get("costUSD") or 0),
        },
        "claude": {
            "inputTokens": float(claude.get("inputTokens") or 0),
            "cachedInputTokens": float(claude.get("cacheReadTokens") or 0),
            "cacheCreationTokens": float(claude.get("cacheCreationTokens") or 0),
            "outputTokens": float(claude.get("outputTokens") or 0),
            "reasoningOutputTokens": 0.0,
            "costUSD": float(claude.get("totalCost") or 0),
        },
    }


def load_repeat_breakdown(audit_path: Path) -> dict[str, Any]:
    obj = json.loads(audit_path.read_text())
    return obj.get("repeatBreakdown", {}) or {}


def parse_codex_session(path: Path) -> SessionStats | None:
    user_prompts: list[str] = []
    windows: list[PromptWindow] = []
    current: PromptWindow | None = None
    cum_input = cum_cached = cum_output = cum_reasoning = cum_total = 0
    last_total: dict[str, int] | None = None
    agent_message_count = tool_calls_total = func_outputs_total = 0
    fn_counts: Counter[str] = Counter()
    verb_counts: Counter[str] = Counter()
    billable_model = ""
    billable_model_source = ""
    session_date = ""
    session_cwd = ""

    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None

    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not session_date:
            session_date = iso_date(obj.get("timestamp"))
        t = obj.get("type")
        if t == "session_meta":
            payload = obj.get("payload") or {}
            if not session_date:
                session_date = iso_date(payload.get("timestamp"))
            if isinstance(payload.get("cwd"), str) and not session_cwd:
                session_cwd = payload["cwd"]
        if t == "event_msg":
            payload = obj.get("payload") or {}
            ptype = payload.get("type")
            if ptype == "user_message" and isinstance(payload.get("message"), str):
                if current is not None:
                    windows.append(current)
                user_prompts.append(payload["message"])
                current = PromptWindow(prompt_index=len(user_prompts), prompt_text=payload["message"])
            elif ptype == "agent_message":
                agent_message_count += 1
                if current is not None:
                    current.agent_messages += 1
                    if isinstance(payload.get("message"), str):
                        current.final_answer = payload["message"]
            elif ptype == "token_count":
                tt = ((payload.get("info") or {}).get("total_token_usage") or {})
                if tt:
                    ni = int(tt.get("input_tokens") or 0)
                    nc = int(tt.get("cached_input_tokens") or 0)
                    no = int(tt.get("output_tokens") or 0)
                    nr = int(tt.get("reasoning_output_tokens") or 0)
                    nt = int(tt.get("total_tokens") or 0)
                    di, dc, do, dr, dt = max(0, ni - cum_input), max(0, nc - cum_cached), max(0, no - cum_output), max(0, nr - cum_reasoning), max(0, nt - cum_total)
                    if current is not None:
                        current.input_delta += di
                        current.cached_delta += dc
                        current.output_delta += do
                        current.reasoning_delta += dr
                        current.total_delta += dt
                    cum_input, cum_cached, cum_output, cum_reasoning, cum_total = ni, nc, no, nr, nt
                    last_total = {"input": ni, "cached": nc, "output": no, "reasoning": nr, "total": nt}
        elif t == "turn_context":
            payload = obj.get("payload") or {}
            candidate = payload.get("model")
            if isinstance(candidate, str) and candidate.strip() and not billable_model:
                billable_model, billable_model_source = resolve_billable_model("codex", candidate)
        elif t == "response_item":
            payload = obj.get("payload") or {}
            ptype = payload.get("type")
            if ptype == "function_call":
                tool_calls_total += 1
                if current is not None:
                    current.tool_calls += 1
                fname = (payload.get("name") or "unknown").lower()
                fn_counts[fname] += 1
                if current is not None:
                    current.function_name_counts[fname] += 1
                    add_command_call(current, fname, payload.get("arguments"), str(payload.get("call_id") or payload.get("id") or ""))
                if fname in SHELL_FUNCTION_NAMES:
                    verb = extract_shell_verb(payload.get("arguments"))
                    if verb:
                        verb_counts[verb] += 1
                        if current is not None:
                            current.shell_verb_counts[verb] += 1
            elif ptype == "function_call_output":
                func_outputs_total += 1
                if current is not None:
                    current.function_outputs += 1
                    attach_command_output(
                        current,
                        str(payload.get("call_id") or payload.get("id") or ""),
                        output_text_from_payload(payload.get("output") or payload.get("content")),
                    )
            elif ptype == "message" and payload.get("role") in {"assistant", "agent"}:
                if current is not None:
                    current.response_messages += 1
                    text = assistant_text_from_content(payload.get("content"))
                    if text:
                        current.final_answer = text

    if current is not None:
        windows.append(current)
    if not user_prompts and last_total is None:
        return None

    final = last_total or {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "total": 0}
    if not billable_model:
        billable_model, billable_model_source = default_billable_model("codex")
    bucket = "planning" if PLANNING_RE.search("\n".join(user_prompts)) else "execution"
    attach_prompt_context(windows, user_prompts[0] if user_prompts else "")
    return SessionStats(
        model="codex",
        file=str(path),
        session_date=session_date,
        bucket=bucket,
        provider=provider_for_session_family("codex"),
        billable_model=billable_model,
        billable_model_source=billable_model_source,
        usage_source="codex_token_count",
        user_prompts=len(user_prompts),
        agent_messages=agent_message_count,
        tool_calls=tool_calls_total,
        function_outputs=func_outputs_total,
        final_input=final["input"],
        final_cached=final["cached"],
        final_output=final["output"],
        final_reasoning=final["reasoning"],
        final_total=final["total"],
        session_cwd=session_cwd,
        first_prompt=user_prompts[0] if user_prompts else "",
        prompt_windows=windows,
        function_name_counts=fn_counts,
        shell_verb_counts=verb_counts,
    )


def _extract_user_text(msg: dict[str, Any]) -> str | None:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("type")
                if t == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
        out = "\n".join(p for p in parts if p.strip())
        return out if out.strip() else None
    return None


def parse_claude_session(path: Path) -> SessionStats | None:
    user_prompts: list[str] = []
    windows: list[PromptWindow] = []
    current: PromptWindow | None = None
    agent_message_count = tool_calls_total = 0
    fn_counts: Counter[str] = Counter()
    verb_counts: Counter[str] = Counter()
    seen_usage_ids: set[str] = set()
    seen_tool_ids: set[str] = set()
    fi = fc = fcc = fo = fr = ft = 0
    billable_model = ""
    billable_model_source = ""
    session_date = ""
    session_cwd = ""

    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None

    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not session_date:
            session_date = iso_date(obj.get("timestamp"))
        for cwd_key in ("cwd", "projectPath"):
            candidate_cwd = obj.get(cwd_key)
            if isinstance(candidate_cwd, str) and candidate_cwd.strip() and not session_cwd:
                session_cwd = candidate_cwd

        typ = obj.get("type")
        if typ == "user":
            msg = obj.get("message") or {}
            if msg.get("role") == "user":
                prompt = _extract_user_text(msg)
                if prompt:
                    if current is not None:
                        windows.append(current)
                    user_prompts.append(prompt)
                    current = PromptWindow(prompt_index=len(user_prompts), prompt_text=prompt)
                content = msg.get("content")
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_result":
                            attach_command_output(
                                current,
                                str(item.get("tool_use_id") or item.get("id") or ""),
                                output_text_from_payload(item.get("content")),
                            )
        elif typ == "assistant":
            msg = obj.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            candidate = msg.get("model")
            if isinstance(candidate, str) and candidate.strip() and not billable_model:
                billable_model, billable_model_source = resolve_billable_model("claude", candidate)
            agent_message_count += 1
            if current is not None:
                current.agent_messages += 1
                current.response_messages += 1
                text = assistant_text_from_content(msg.get("content"))
                if text:
                    current.final_answer = text

            uid = str(msg.get("id") or obj.get("uuid") or "")
            if uid and uid not in seen_usage_ids:
                seen_usage_ids.add(uid)
                usage = msg.get("usage") or {}
                di = int(usage.get("input_tokens") or 0)
                dc = int(usage.get("cache_read_input_tokens") or 0)
                dcc = int(usage.get("cache_creation_input_tokens") or 0)
                do = int(usage.get("output_tokens") or 0)
                dr = 0
                dt = di + dc + dcc + do
                fi += di
                fc += dc
                fcc += dcc
                fo += do
                fr += dr
                ft += dt
                if current is not None:
                    current.input_delta += di
                    current.cached_delta += dc
                    current.cache_creation_delta += dcc
                    current.output_delta += do
                    current.reasoning_delta += dr
                    current.total_delta += dt

            content = msg.get("content") or []
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "tool_use":
                        continue
                    tuid = str(item.get("id") or "")
                    if tuid and tuid in seen_tool_ids:
                        continue
                    if tuid:
                        seen_tool_ids.add(tuid)
                    name = str(item.get("name") or "unknown").lower()
                    tool_calls_total += 1
                    fn_counts[name] += 1
                    if current is not None:
                        current.function_name_counts[name] += 1
                        add_command_call(current, name, item.get("input"), str(item.get("id") or ""))
                    if current is not None:
                        current.tool_calls += 1
                    if name in SHELL_FUNCTION_NAMES:
                        verb = extract_shell_verb(item.get("input"))
                        if verb:
                            verb_counts[verb] += 1
                            if current is not None:
                                current.shell_verb_counts[verb] += 1

    if current is not None:
        windows.append(current)
    if not user_prompts and ft == 0:
        return None

    if not billable_model:
        billable_model, billable_model_source = default_billable_model("claude")
    bucket = "planning" if PLANNING_RE.search("\n".join(user_prompts)) else "execution"
    attach_prompt_context(windows, user_prompts[0] if user_prompts else "")
    return SessionStats(
        model="claude",
        file=str(path),
        session_date=session_date,
        bucket=bucket,
        provider=provider_for_session_family("claude"),
        billable_model=billable_model,
        billable_model_source=billable_model_source,
        usage_source="claude_message_usage",
        user_prompts=len(user_prompts),
        agent_messages=agent_message_count,
        tool_calls=tool_calls_total,
        function_outputs=0,
        final_input=fi,
        final_cached=fc,
        final_cache_creation=fcc,
        final_output=fo,
        final_reasoning=fr,
        final_total=ft,
        session_cwd=session_cwd,
        first_prompt=user_prompts[0] if user_prompts else "",
        prompt_windows=windows,
        function_name_counts=fn_counts,
        shell_verb_counts=verb_counts,
    )


def build_rows_for_model(
    sessions: list[SessionStats],
    model_totals: dict[str, float],
    pricing_table: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    eff_corpus = sum(s.final_input + 0.1 * s.final_cached for s in sessions) or 0.0
    cost_total = float(model_totals.get("costUSD", 0.0))
    cost_per_eff = safe_div(cost_total, eff_corpus)

    session_rows: list[dict[str, Any]] = []
    prompt_rows: list[dict[str, Any]] = []
    attribution_rows: list[dict[str, Any]] = []
    command_rows: list[dict[str, Any]] = []

    for s in sessions:
        eff = s.final_input + 0.1 * s.final_cached
        cost = eff * cost_per_eff
        chp = safe_div(s.final_cached, s.final_input + s.final_cached) * 100.0
        session_cost = derive_cost(
            pricing_table,
            s.billable_model,
            input_tokens=s.final_input,
            cache_read_tokens=s.final_cached,
            cache_creation_tokens=s.final_cache_creation,
            output_tokens=s.final_output,
            input_includes_cache=s.model == "codex",
        )
        session_total_tokens = raw_total_tokens(
            s.model,
            input_tokens=s.final_input,
            cache_read_tokens=s.final_cached,
            cache_creation_tokens=s.final_cache_creation,
            output_tokens=s.final_output,
            reasoning_tokens=s.final_reasoning,
            provider_total_tokens=s.final_total,
        )
        session_rows.append(
            {
                "model": s.model,
                "provider": s.provider,
                "billable_model": s.billable_model,
                "billable_model_source": s.billable_model_source,
                "usage_source": s.usage_source,
                "file": s.file,
                "session_date": s.session_date,
                "bucket": s.bucket,
                "user_prompts": s.user_prompts,
                "agent_messages": s.agent_messages,
                "tool_calls": s.tool_calls,
                "function_outputs": s.function_outputs,
                "input_tokens": s.final_input,
                "cache_read_input_tokens": s.final_cached,
                "cached_input_tokens": s.final_cached,
                "cache_creation_input_tokens": s.final_cache_creation,
                "output_tokens": s.final_output,
                "reasoning_output_tokens": s.final_reasoning,
                "total_tokens": session_total_tokens or s.final_total,
                "effective_input_10pct": eff,
                "cache_hit_pct": chp,
                "estimated_cost_usd": cost,
                **session_cost,
                "session_cwd": s.session_cwd,
                "first_prompt_preview": shorten(s.first_prompt, 280),
            }
        )
        for w in s.prompt_windows:
            weff = w.input_delta + 0.1 * w.cached_delta
            wcost = weff * cost_per_eff
            wchp = safe_div(w.cached_delta, w.input_delta + w.cached_delta) * 100.0
            prompt_cost = derive_cost(
                pricing_table,
                s.billable_model,
                input_tokens=w.input_delta,
                cache_read_tokens=w.cached_delta,
                cache_creation_tokens=w.cache_creation_delta,
                output_tokens=w.output_delta,
                input_includes_cache=s.model == "codex",
            )
            prompt_total_tokens = raw_total_tokens(
                s.model,
                input_tokens=w.input_delta,
                cache_read_tokens=w.cached_delta,
                cache_creation_tokens=w.cache_creation_delta,
                output_tokens=w.output_delta,
                reasoning_tokens=w.reasoning_delta,
                provider_total_tokens=w.total_delta,
            )
            prompt_rows.append(
                {
                    "model": s.model,
                    "provider": s.provider,
                    "billable_model": s.billable_model,
                    "billable_model_source": s.billable_model_source,
                    "usage_source": s.usage_source,
                    "file": s.file,
                    "session_date": s.session_date,
                    "bucket": s.bucket,
                    "prompt_index": w.prompt_index,
                    "prompt_preview": shorten(w.prompt_text, 280),
                    "session_cwd": s.session_cwd,
                    "previous_prompt_preview": shorten(w.previous_prompt, 280),
                    "first_prompt_preview": shorten(w.first_prompt, 280),
                    "final_answer_preview": shorten(w.final_answer, 280),
                    "tool_calls": w.tool_calls,
                    "agent_messages": w.agent_messages,
                    "response_messages": w.response_messages,
                    "function_outputs": w.function_outputs,
                    "input_tokens_delta": w.input_delta,
                    "cache_read_tokens_delta": w.cached_delta,
                    "cached_tokens_delta": w.cached_delta,
                    "cache_creation_tokens_delta": w.cache_creation_delta,
                    "output_tokens_delta": w.output_delta,
                    "reasoning_tokens_delta": w.reasoning_delta,
                    "total_tokens_delta": prompt_total_tokens or w.total_delta,
                    "effective_input_10pct": weff,
                    "cache_hit_pct": wchp,
                    "estimated_cost_usd": wcost,
                    **prompt_cost,
                }
            )
            prompt_total_cost = prompt_cost["derived_total_cost_usd"]
            session_total_cost = session_cost["derived_total_cost_usd"]
            command_weight_total = sum(max(1, call.output_token_estimate) for call in w.command_calls) or len(w.command_calls)
            for call in w.command_calls:
                weight = safe_div(max(1, call.output_token_estimate), command_weight_total) if command_weight_total else 0.0
                primary_why, why_classifier = classify_command_why(
                    call.function_name,
                    call.shell_verb,
                    call.command_text,
                    call.target,
                    "",
                )
                command_rows.append(
                    {
                        "schema_version": COMMAND_ATTRIBUTION_SCHEMA_VERSION,
                        "model": s.model,
                        "provider": s.provider,
                        "billable_model": s.billable_model,
                        "billable_model_source": s.billable_model_source,
                        "usage_source": s.usage_source,
                        "file": s.file,
                        "session_date": s.session_date,
                        "bucket": s.bucket,
                        "prompt_index": w.prompt_index,
                        "command_index": call.command_index,
                        "prompt_preview": shorten(w.prompt_text, 280),
                        "session_cwd": s.session_cwd,
                        "previous_prompt_preview": shorten(w.previous_prompt, 280),
                        "first_prompt_preview": shorten(w.first_prompt, 280),
                        "final_answer_preview": shorten(w.final_answer, 280),
                        "function_name": call.function_name,
                        "shell_verb": call.shell_verb,
                        "command_preview": call.command_preview,
                        "command_hash": call.command_hash,
                        "workdir": call.workdir,
                        "target_type": call.target_type,
                        "target": call.target,
                        "delegated_agent_action": call.delegated_agent_action,
                        "delegated_agent_id": call.delegated_agent_id,
                        "delegated_agent_type": call.delegated_agent_type,
                        "delegated_agent_nickname": call.delegated_agent_nickname,
                        "delegated_task_preview": call.delegated_task_preview,
                        "delegated_task_hash": call.delegated_task_hash,
                        "output_chars": call.output_chars,
                        "output_token_estimate": call.output_token_estimate,
                        "prompt_input_tokens": w.input_delta,
                        "prompt_cache_read_tokens": w.cached_delta,
                        "prompt_cache_creation_tokens": w.cache_creation_delta,
                        "prompt_output_tokens": w.output_delta,
                        "prompt_reasoning_tokens": w.reasoning_delta,
                        "prompt_total_tokens": prompt_total_tokens or w.total_delta,
                        "prompt_derived_total_cost_usd": prompt_total_cost,
                        "allocated_input_tokens": w.input_delta * weight,
                        "allocated_cache_read_tokens": w.cached_delta * weight,
                        "allocated_cache_creation_tokens": w.cache_creation_delta * weight,
                        "allocated_output_tokens": w.output_delta * weight,
                        "allocated_reasoning_tokens": w.reasoning_delta * weight,
                        "allocated_total_tokens": (prompt_total_tokens or w.total_delta) * weight,
                        "allocated_total_cost_usd": prompt_total_cost * weight if prompt_total_cost is not None else None,
                        "allocation_weight": weight,
                        "cost_is_estimated": True,
                        "cost_allocation_method": COMMAND_COST_ALLOCATION_METHOD,
                        "primary_why": primary_why,
                        "why_tags": primary_why,
                        "why_classifier": why_classifier,
                    }
                )

            for dimension, counts in (
                ("function_name", w.function_name_counts),
                ("shell_verb", w.shell_verb_counts),
            ):
                total_calls = sum(counts.values())
                if not total_calls:
                    continue
                for name, calls in counts.items():
                    share = safe_div(calls, total_calls)
                    attribution_rows.append(
                        {
                            "model": s.model,
                            "provider": s.provider,
                            "billable_model": s.billable_model,
                            "billable_model_source": s.billable_model_source,
                            "usage_source": s.usage_source,
                            "file": s.file,
                            "session_date": s.session_date,
                            "bucket": s.bucket,
                            "prompt_index": w.prompt_index,
                            "prompt_preview": shorten(w.prompt_text, 280),
                            "session_cwd": s.session_cwd,
                            "previous_prompt_preview": shorten(w.previous_prompt, 280),
                            "first_prompt_preview": shorten(w.first_prompt, 280),
                            "final_answer_preview": shorten(w.final_answer, 280),
                            "dimension": dimension,
                            "name": name,
                            "calls": calls,
                            "prompt_input_tokens": w.input_delta,
                            "prompt_cache_read_tokens": w.cached_delta,
                            "prompt_cache_creation_tokens": w.cache_creation_delta,
                            "prompt_output_tokens": w.output_delta,
                            "prompt_reasoning_tokens": w.reasoning_delta,
                            "prompt_total_tokens": prompt_total_tokens or w.total_delta,
                            "session_input_tokens": s.final_input,
                            "session_cache_read_tokens": s.final_cached,
                            "session_cache_creation_tokens": s.final_cache_creation,
                            "session_output_tokens": s.final_output,
                            "session_reasoning_tokens": s.final_reasoning,
                            "session_total_tokens": session_total_tokens or s.final_total,
                            "prompt_derived_total_cost_usd": prompt_total_cost,
                            "session_derived_total_cost_usd": session_total_cost,
                            "allocated_input_tokens": w.input_delta * share,
                            "allocated_cache_read_tokens": w.cached_delta * share,
                            "allocated_cache_creation_tokens": w.cache_creation_delta * share,
                            "allocated_output_tokens": w.output_delta * share,
                            "allocated_reasoning_tokens": w.reasoning_delta * share,
                            "allocated_total_tokens": (prompt_total_tokens or w.total_delta) * share,
                            "allocated_total_cost_usd": prompt_total_cost * share if prompt_total_cost is not None else None,
                            "call_share_pct": share * 100.0,
                            "allocation_method": "prompt_window_even_split",
                            "pricing_missing": prompt_cost["pricing_missing"],
                        }
                    )
    return session_rows, prompt_rows, attribution_rows, command_rows


def bucket_view(session_rows: list[dict[str, Any]], prompt_rows: list[dict[str, Any]], bucket_name: str) -> dict[str, Any]:
    b_sessions = [r for r in session_rows if r["bucket"] == bucket_name]
    b_prompts = [r for r in prompt_rows if r["bucket"] == bucket_name]
    cost_p = pareto_cut(b_sessions, "estimated_cost_usd", 0.80)
    tok_p = pareto_cut(b_sessions, "effective_input_10pct", 0.80)
    tool_p = pareto_cut(b_sessions, "tool_calls", 0.80)
    top_files = {r["file"] for r in cost_p["selected"]}
    prompts_in_top = [p for p in b_prompts if p["file"] in top_files]

    def total(k: str) -> float:
        return float(sum(r.get(k, 0) for r in b_sessions))

    inp = total("input_tokens")
    cache = total("cached_input_tokens")
    cache_creation = total("cache_creation_input_tokens")
    return {
        "session_count": len(b_sessions),
        "totals": {
            "input_tokens": int(inp),
            "cached_input_tokens": int(cache),
            "cache_read_input_tokens": int(cache),
            "cache_creation_input_tokens": int(cache_creation),
            "output_tokens": int(total("output_tokens")),
            "reasoning_output_tokens": int(total("reasoning_output_tokens")),
            "total_tokens": int(total("total_tokens")),
            "effective_input_10pct": float(total("effective_input_10pct")),
            "estimated_cost_usd": float(total("estimated_cost_usd")),
            "cache_hit_pct": safe_div(cache, inp + cache) * 100.0,
        },
        "distributions": {
            "estimated_cost_usd": session_distribution([r["estimated_cost_usd"] for r in b_sessions]),
            "effective_input_10pct": session_distribution([r["effective_input_10pct"] for r in b_sessions]),
            "tool_calls": session_distribution([float(r["tool_calls"]) for r in b_sessions]),
            "input_tokens": session_distribution([float(r["input_tokens"]) for r in b_sessions]),
            "cached_input_tokens": session_distribution([float(r["cached_input_tokens"]) for r in b_sessions]),
        },
        "pareto80_by_cost": cost_p,
        "pareto80_by_tokens": tok_p,
        "pareto80_by_tool_calls": tool_p,
        "top_driver_prompts_by_cost_in_pareto": sorted(prompts_in_top, key=lambda r: r["estimated_cost_usd"], reverse=True)[:25],
        "top_driver_prompts_by_tool_calls_in_pareto": sorted(prompts_in_top, key=lambda r: r["tool_calls"], reverse=True)[:25],
    }


def aggregate_tools(sessions: list[SessionStats], session_rows: list[dict[str, Any]], bucket_name: str) -> dict[str, Any]:
    index = {(r["model"], r["file"]): r for r in session_rows}
    fn_calls: Counter[str] = Counter()
    fn_cost: dict[str, float] = {}
    fn_sess: dict[str, set[str]] = {}
    v_calls: Counter[str] = Counter()
    v_cost: dict[str, float] = {}
    v_sess: dict[str, set[str]] = {}
    bucket_cost = 0.0
    for s in sessions:
        if s.bucket != bucket_name:
            continue
        row = index.get((s.model, s.file))
        if not row:
            continue
        sc = float(row["estimated_cost_usd"])
        bucket_cost += sc
        per_call = sc / max(1, int(row["tool_calls"]))
        for n, c in s.function_name_counts.items():
            fn_calls[n] += c
            fn_cost[n] = fn_cost.get(n, 0.0) + per_call * c
            fn_sess.setdefault(n, set()).add(s.file)
        for n, c in s.shell_verb_counts.items():
            v_calls[n] += c
            v_cost[n] = v_cost.get(n, 0.0) + per_call * c
            v_sess.setdefault(n, set()).add(s.file)

    def rows(calls: Counter[str], costs: dict[str, float], smap: dict[str, set[str]]) -> list[dict[str, Any]]:
        tot_calls = sum(calls.values()) or 1
        out: list[dict[str, Any]] = []
        for n, c in calls.items():
            co = costs.get(n, 0.0)
            out.append(
                {
                    "name": n,
                    "calls": c,
                    "calls_share_pct": c / tot_calls * 100.0,
                    "sessions_with_tool": len(smap.get(n, set())),
                    "avg_calls_per_using_session": c / max(1, len(smap.get(n, set()))),
                    "projected_cost_usd": co,
                    "projected_cost_share_pct": (co / bucket_cost * 100.0) if bucket_cost else 0.0,
                }
            )
        out.sort(key=lambda r: r["calls"], reverse=True)
        return out

    return {
        "bucket": bucket_name,
        "bucket_total_cost_usd": bucket_cost,
        "by_function_name": rows(fn_calls, fn_cost, fn_sess),
        "by_shell_verb": rows(v_calls, v_cost, v_sess),
    }


def section_from_rows(name: str, sessions: list[SessionStats], session_rows: list[dict[str, Any]], prompt_rows: list[dict[str, Any]]) -> dict[str, Any]:
    planning = bucket_view(session_rows, prompt_rows, "planning")
    execution = bucket_view(session_rows, prompt_rows, "execution")
    return {
        "name": name,
        "totals": {
            "session_count": len(session_rows),
            "planning_session_count": planning["session_count"],
            "execution_session_count": execution["session_count"],
            "estimated_cost_usd": planning["totals"]["estimated_cost_usd"] + execution["totals"]["estimated_cost_usd"],
            "effective_input_10pct": planning["totals"]["effective_input_10pct"] + execution["totals"]["effective_input_10pct"],
        },
        "planning": planning,
        "execution": execution,
        "tool_breakdown": {
            "planning": aggregate_tools(sessions, session_rows, "planning"),
            "execution": aggregate_tools(sessions, session_rows, "execution"),
            "methodology": (
                "Projected tool cost uses uniform per-call attribution within each session: "
                "session_cost/session_tool_calls multiplied by tool call counts."
            ),
        },
    }


def source_shares(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    c: Counter[str] = Counter()
    for r in entries:
        c[str(r.get("source", "unknown"))] += int(r.get("tokenEstimateTotal", 0) or 0)
    total = sum(c.values()) or 1
    return [{"source": k, "estimated_repeated_tokens": v, "share_pct": v / total * 100.0} for k, v in sorted(c.items(), key=lambda kv: kv[1], reverse=True)]


def audit_dedup_dirs(audit_path: Path, codex_dir: Path, claude_dir: Path) -> tuple[Path, Path]:
    try:
        audit = json.loads(audit_path.read_text())
    except (OSError, json.JSONDecodeError):
        return codex_dir, claude_dir
    dedup = audit.get("dedup", {}) if isinstance(audit, dict) else {}
    codex_raw = (dedup.get("codex") or {}).get("mergedSessionsDir")
    claude_raw = (dedup.get("claude") or {}).get("mergedRootDir")
    if isinstance(codex_raw, str) and codex_raw.strip():
        codex_candidate = Path(codex_raw).expanduser()
        if codex_candidate.exists():
            codex_dir = codex_candidate
    if isinstance(claude_raw, str) and claude_raw.strip():
        claude_candidate = Path(claude_raw).expanduser()
        if claude_candidate.exists():
            claude_dir = claude_candidate
    return codex_dir, claude_dir


def build_report(
    out_dir: Path,
    audit_path: Path,
    codex_dir: Path,
    claude_dir: Path,
    v4_2_cluster_labels: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    codex_dir, claude_dir = audit_dedup_dirs(audit_path, codex_dir, claude_dir)
    model_totals = load_model_totals(audit_path)
    pricing_table = load_pricing_table(None)
    repeat_breakdown = load_repeat_breakdown(audit_path)

    codex_sessions = [s for fp in sorted(codex_dir.rglob("*.jsonl")) if (s := parse_codex_session(fp))]
    claude_sessions = [s for fp in sorted(claude_dir.rglob("*.jsonl")) if (s := parse_claude_session(fp))]

    codex_session_rows, codex_prompt_rows, codex_attribution_rows, codex_command_rows = build_rows_for_model(codex_sessions, model_totals["codex"], pricing_table)
    claude_session_rows, claude_prompt_rows, claude_attribution_rows, claude_command_rows = build_rows_for_model(claude_sessions, model_totals["claude"], pricing_table)

    all_sessions = codex_sessions + claude_sessions
    all_session_rows = codex_session_rows + claude_session_rows
    all_prompt_rows = codex_prompt_rows + claude_prompt_rows
    all_attribution_rows = codex_attribution_rows + claude_attribution_rows
    all_command_rows = codex_command_rows + claude_command_rows
    all_command_rows_v4_1 = build_command_attribution_v4_1_rows(all_command_rows)
    all_command_rows_v4_2, all_command_rows_v4_2_review = build_command_attribution_v4_2_rows(all_command_rows, v4_2_cluster_labels)
    all_command_rows_v4_3, all_command_rows_v4_3_review = build_command_attribution_v4_3_rows(all_command_rows)
    all_command_rows_v4_4, all_command_rows_v4_4_review = build_command_attribution_v4_4_rows(all_command_rows)

    codex_section = section_from_rows("codex", codex_sessions, codex_session_rows, codex_prompt_rows)
    claude_section = section_from_rows("claude", claude_sessions, claude_session_rows, claude_prompt_rows)
    combined_section = section_from_rows("combined", all_sessions, all_session_rows, all_prompt_rows)

    codex_repeats = sorted((repeat_breakdown.get("topCodexRepeatedValues") or []), key=lambda r: r.get("tokenEstimateTotal", 0), reverse=True)
    claude_repeats = sorted((repeat_breakdown.get("topClaudeRepeatedValues") or []), key=lambda r: r.get("tokenEstimateTotal", 0), reverse=True)
    combined_repeats = sorted(codex_repeats + claude_repeats, key=lambda r: r.get("tokenEstimateTotal", 0), reverse=True)

    report = {
        "scope": {
            "codex_sessions_dir": str(codex_dir),
            "claude_sessions_dir": str(claude_dir),
            "audit_report": str(audit_path),
            "planning_phrases": PLANNING_PHRASES,
            "cost_model": {
                "method": "proportional-to-ccusage",
                "effective_formula": "input_tokens + 0.1 * cached_input_tokens",
                "model_totals": model_totals,
                "pricing_source": DEFAULT_PRICING_URL,
            },
        },
        "combined": combined_section,
        "codex": codex_section,
        "claude": claude_section,
        "cache_hit_drivers": {
            "combined": {
                "source_shares": source_shares(combined_repeats),
                "top_repeated_values": combined_repeats[:15],
            },
            "codex": {
                "source_shares": source_shares(codex_repeats),
                "top_repeated_values": codex_repeats[:15],
            },
            "claude": {
                "source_shares": source_shares(claude_repeats),
                "top_repeated_values": claude_repeats[:15],
            },
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "planning-vs-execution-report.json").write_text(json.dumps(report, indent=2))

    def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as f:
            if rows:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            else:
                f.write("empty\n")

    write_csv(out_dir / "planning-vs-execution-sessions.csv", all_session_rows)
    write_csv(out_dir / "planning-vs-execution-prompts.csv", all_prompt_rows)
    write_csv(out_dir / "planning-vs-execution-tool-attribution.csv", all_attribution_rows)

    tool_rows: list[dict[str, Any]] = []
    for sec_name, sec in [("combined", combined_section), ("codex", codex_section), ("claude", claude_section)]:
        for bucket in ("planning", "execution"):
            v = sec["tool_breakdown"][bucket]
            for row in v["by_function_name"]:
                tool_rows.append({**row, "section": sec_name, "bucket": bucket, "dimension": "function_name"})
            for row in v["by_shell_verb"]:
                tool_rows.append({**row, "section": sec_name, "bucket": bucket, "dimension": "shell_verb"})
    write_csv(out_dir / "planning-vs-execution-tool-breakdown.csv", tool_rows)
    write_csv(out_dir / "usage-command-attribution-v4.csv", all_command_rows)
    (out_dir / "usage-command-attribution-v4-summary.json").write_text(json.dumps(command_summary(all_command_rows), indent=2))
    (out_dir / "usage-command-attribution-v4-report.md").write_text(render_command_markdown(all_command_rows))
    write_csv(out_dir / "usage-command-attribution-v4_1.csv", all_command_rows_v4_1)
    (out_dir / "usage-command-attribution-v4_1-summary.json").write_text(json.dumps(command_summary(all_command_rows_v4_1), indent=2))
    (out_dir / "usage-command-attribution-v4_1-report.md").write_text(render_command_markdown(all_command_rows_v4_1))
    write_csv(out_dir / "usage-command-attribution-v4_2.csv", all_command_rows_v4_2)
    write_csv(out_dir / "usage-command-attribution-v4_2-review.csv", all_command_rows_v4_2_review)
    (out_dir / "usage-command-attribution-v4_2-summary.json").write_text(json.dumps(command_summary(all_command_rows_v4_2), indent=2))
    (out_dir / "usage-command-attribution-v4_2-report.md").write_text(render_command_markdown(all_command_rows_v4_2))
    write_csv(out_dir / "usage-command-attribution-v4_3.csv", all_command_rows_v4_3)
    write_csv(out_dir / "usage-command-attribution-v4_3-review.csv", all_command_rows_v4_3_review)
    (out_dir / "usage-command-attribution-v4_3-summary.json").write_text(json.dumps(command_summary(all_command_rows_v4_3), indent=2))
    (out_dir / "usage-command-attribution-v4_3-report.md").write_text(render_command_markdown(all_command_rows_v4_3))
    write_csv(out_dir / "usage-command-attribution-v4_4.csv", all_command_rows_v4_4)
    write_csv(out_dir / "usage-command-attribution-v4_4-review.csv", all_command_rows_v4_4_review)
    (out_dir / "usage-command-attribution-v4_4-summary.json").write_text(json.dumps(command_summary(all_command_rows_v4_4), indent=2))
    (out_dir / "usage-command-attribution-v4_4-report.md").write_text(render_command_markdown(all_command_rows_v4_4))

    (out_dir / "planning-vs-execution-summary.md").write_text(render_markdown(report))
    return report


def fmt_int(n: float) -> str:
    return f"{int(n):,}"


def fmt_money(n: float) -> str:
    return f"${n:,.2f}"


def fmt_pct(n: float) -> str:
    return f"{n:.2f}%"


def top_counter_rows(rows: list[dict[str, Any]], key: str, cost_key: str = "allocated_total_cost_usd", limit: int = 20) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    costs: dict[str, float] = {}
    for row in rows:
        value = str(row.get(key) or "")
        if not value:
            continue
        counts[value] += 1
        costs[value] = costs.get(value, 0.0) + float(row.get(cost_key) or 0.0)
    return [
        {"name": name, "commands": count, "allocated_total_cost_usd": costs.get(name, 0.0)}
        for name, count in counts.most_common(limit)
    ]


def command_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "schema_version": rows[0].get("schema_version", COMMAND_ATTRIBUTION_SCHEMA_VERSION) if rows else COMMAND_ATTRIBUTION_SCHEMA_VERSION,
        "command_count": len(rows),
        "estimated_cost_usd": sum(float(row.get("allocated_total_cost_usd") or 0.0) for row in rows),
        "cost_allocation_method": COMMAND_COST_ALLOCATION_METHOD,
        "cost_is_estimated": True,
        "by_primary_why": top_counter_rows(rows, "primary_why"),
        "by_service_of_why": top_counter_rows(rows, "service_of_why"),
        "by_tool_action": top_counter_rows(rows, "tool_action"),
        "by_uncategorized_reason": top_counter_rows(rows, "uncategorized_reason"),
        "by_function_name": top_counter_rows(rows, "function_name"),
        "by_shell_verb": top_counter_rows(rows, "shell_verb"),
        "by_session": top_counter_rows(rows, "file", limit=25),
    }
    if rows and rows[0].get("schema_version") in {
        COMMAND_ATTRIBUTION_SCHEMA_VERSION_V4_2,
        COMMAND_ATTRIBUTION_SCHEMA_VERSION_V4_3,
        COMMAND_ATTRIBUTION_SCHEMA_VERSION_V4_4,
    }:
        high_rows = [
            row
            for row in rows
            if row.get("primary_why_confidence") == "high"
            and row.get("prompt_task_kind_confidence") == "high"
            and row.get("agent_tool_intention_confidence") == "high"
        ]
        classifier_revision = (
            COMMAND_ATTRIBUTION_CLASSIFIER_REVISION_V4_4
            if rows[0].get("schema_version") == COMMAND_ATTRIBUTION_SCHEMA_VERSION_V4_4
            else (
                COMMAND_ATTRIBUTION_CLASSIFIER_REVISION_V4_3
                if rows[0].get("schema_version") == COMMAND_ATTRIBUTION_SCHEMA_VERSION_V4_3
                else COMMAND_ATTRIBUTION_CLASSIFIER_REVISION_V4_2
            )
        )
        summary.update(
            {
                "classifier_revision": classifier_revision,
                "high_confidence_rows": len(high_rows),
                "high_confidence_share": safe_div(len(high_rows), len(rows)),
                "by_prompt_task_kind": top_counter_rows(rows, "prompt_task_kind"),
                "by_agent_tool_intention": top_counter_rows(rows, "agent_tool_intention"),
                "by_tool_execution_mode": top_counter_rows(rows, "tool_execution_mode"),
                "by_delegated_agent_action": top_counter_rows(rows, "delegated_agent_action"),
                "by_classification_agreement": top_counter_rows(rows, "classification_agreement"),
                "by_review_reason": top_counter_rows(rows, "review_reason"),
            }
        )
    return summary


def render_command_markdown(rows: list[dict[str, Any]]) -> str:
    summary = command_summary(rows)
    lines = [
        "# Usage Why / Command Cost",
        "",
        f"- Schema: `{summary['schema_version']}`",
        f"- Commands: **{fmt_int(summary['command_count'])}**",
        f"- Estimated allocated cost: **{fmt_money(summary['estimated_cost_usd'])}**",
        f"- Methodology: exact prompt costs allocated to commands by output-token estimate; command costs are estimated.",
        "",
        "## Cost by why",
        "",
        "| Why | Commands | Estimated cost |",
        "|---|---:|---:|",
    ]
    for row in summary["by_primary_why"]:
        lines.append(f"| `{row['name']}` | {fmt_int(row['commands'])} | {fmt_money(row['allocated_total_cost_usd'])} |")
    if summary["by_service_of_why"]:
        lines.extend(["", "## Cost by service reason", "", "| Service reason | Commands | Estimated cost |", "|---|---:|---:|"])
        for row in summary["by_service_of_why"]:
            lines.append(f"| `{row['name']}` | {fmt_int(row['commands'])} | {fmt_money(row['allocated_total_cost_usd'])} |")
    if summary["by_tool_action"]:
        lines.extend(["", "## Cost by tool action", "", "| Tool action | Commands | Estimated cost |", "|---|---:|---:|"])
        for row in summary["by_tool_action"]:
            lines.append(f"| `{row['name']}` | {fmt_int(row['commands'])} | {fmt_money(row['allocated_total_cost_usd'])} |")
    if summary.get("by_agent_tool_intention"):
        lines.extend(["", "## Cost by tool intention", "", "| Tool intention | Commands | Estimated cost |", "|---|---:|---:|"])
        for row in summary["by_agent_tool_intention"]:
            lines.append(f"| `{row['name']}` | {fmt_int(row['commands'])} | {fmt_money(row['allocated_total_cost_usd'])} |")
    lines.extend(["", "## Why x tool matrix", "", "| Why | Tool | Commands | Estimated cost |", "|---|---|---:|---:|"])
    matrix: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        key = (str(row.get("primary_why") or "uncategorized"), str(row.get("function_name") or "unknown"))
        current = matrix.setdefault(key, {"commands": 0.0, "cost": 0.0})
        current["commands"] += 1
        current["cost"] += float(row.get("allocated_total_cost_usd") or 0.0)
    for (why, tool), agg in sorted(matrix.items(), key=lambda item: item[1]["cost"], reverse=True)[:30]:
        lines.append(f"| `{why}` | `{tool}` | {fmt_int(agg['commands'])} | {fmt_money(agg['cost'])} |")
    lines.extend(["", "Generated by `scripts/planning_vs_execution_report.py`."])
    return "\n".join(lines)


def _write_bucket(lines: list[str], name: str, view: dict[str, Any]) -> None:
    t = view["totals"]
    d = view["distributions"]
    lines.append(f"### {name.capitalize()} bucket")
    lines.append("")
    lines.append(f"- Sessions: **{view['session_count']}**, cost: **{fmt_money(t['estimated_cost_usd'])}**, cache-hit: **{fmt_pct(t['cache_hit_pct'])}**")
    lines.append(
        f"- Tokens: input={fmt_int(t['input_tokens'])}, cached={fmt_int(t['cached_input_tokens'])}, output={fmt_int(t['output_tokens'])}"
    )
    lines.append("")
    lines.append("| Metric | Mean | Median | P90 | Max |")
    lines.append("|---|---:|---:|---:|---:|")
    for label, key in [
        ("Estimated cost (USD)", "estimated_cost_usd"),
        ("Effective input (tokens)", "effective_input_10pct"),
        ("Tool calls", "tool_calls"),
    ]:
        stats = d[key]
        fmt = fmt_money if "cost" in key else fmt_int
        lines.append(f"| {label} | {fmt(stats['mean'])} | {fmt(stats['median'])} | {fmt(stats['p90'])} | {fmt(stats['max'])} |")
    lines.append("")
    p = view["pareto80_by_cost"]
    lines.append(f"- 80% cost reached by top **{p['selected_count']}** sessions ({fmt_pct(p['selected_share']*100)} of bucket cost).")
    lines.append("")


def _write_tool(lines: list[str], sec_name: str, tb: dict[str, Any]) -> None:
    lines.append(f"### Tool-call breakdown ({sec_name})")
    lines.append("")
    for bucket in ("planning", "execution"):
        v = tb[bucket]
        lines.append(f"#### {bucket.capitalize()} tools")
        lines.append("")
        lines.append("| Function | Calls | Call share | Projected cost |")
        lines.append("|---|---:|---:|---:|")
        for r in v["by_function_name"][:10]:
            lines.append(f"| `{r['name']}` | {fmt_int(r['calls'])} | {fmt_pct(r['calls_share_pct'])} | {fmt_money(r['projected_cost_usd'])} |")
        lines.append("")
        lines.append("| Shell verb | Calls | Call share | Projected cost |")
        lines.append("|---|---:|---:|---:|")
        for r in v["by_shell_verb"][:12]:
            lines.append(f"| `{r['name']}` | {fmt_int(r['calls'])} | {fmt_pct(r['calls_share_pct'])} | {fmt_money(r['projected_cost_usd'])} |")
        lines.append("")
    lines.append(f"_Methodology: {tb['methodology']}_")
    lines.append("")


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    scope = report["scope"]
    lines.append("# Planning vs Execution Token & Cost Report")
    lines.append("")
    lines.append("## Scope")
    lines.append(f"- Codex sessions: `{scope['codex_sessions_dir']}`")
    lines.append(f"- Claude sessions: `{scope['claude_sessions_dir']}`")
    lines.append(f"- Audit report: `{scope['audit_report']}`")
    lines.append(f"- Planning phrases: {', '.join(f'`{p}`' for p in scope['planning_phrases'])}")
    lines.append("")

    for sec_name in ("combined", "codex", "claude"):
        sec = report[sec_name]
        lines.append(f"## {sec_name.capitalize()} section")
        lines.append("")
        lines.append(
            f"- Session count: **{sec['totals']['session_count']}** "
            f"(planning {sec['totals']['planning_session_count']}, execution {sec['totals']['execution_session_count']})"
        )
        lines.append(
            f"- Estimated cost: **{fmt_money(sec['totals']['estimated_cost_usd'])}**, "
            f"effective input: **{fmt_int(sec['totals']['effective_input_10pct'])}**"
        )
        lines.append("")
        _write_bucket(lines, "planning", sec["planning"])
        _write_bucket(lines, "execution", sec["execution"])
        _write_tool(lines, sec_name, sec["tool_breakdown"])

    lines.append("## Cache-hit drivers")
    lines.append("")
    for sec_name in ("combined", "codex", "claude"):
        c = report["cache_hit_drivers"][sec_name]
        lines.append(f"### {sec_name.capitalize()} source shares")
        lines.append("")
        lines.append("| Source | Estimated repeated tokens | Share |")
        lines.append("|---|---:|---:|")
        for s in c["source_shares"][:10]:
            lines.append(f"| `{s['source']}` | {fmt_int(s['estimated_repeated_tokens'])} | {fmt_pct(s['share_pct'])} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("Generated by `scripts/planning_vs_execution_report.py`.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Planning vs Execution token/cost report (Codex+Claude+combined)")
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "reports"), help="Output directory for report artifacts")
    parser.add_argument("--audit-report", default=str(DEFAULT_AUDIT_REPORT), help="Path to cache-hit-audit-report.json")
    parser.add_argument("--codex-sessions-dir", default=str(DEFAULT_CODEX_DIR), help="Codex sessions directory")
    parser.add_argument("--claude-sessions-dir", default=str(DEFAULT_CLAUDE_DIR), help="Claude sessions directory")
    parser.add_argument(
        "--v4-2-cluster-labels",
        default="",
        help="Optional JSON object keyed by classification_cluster_key with Codex-reviewed v4.2 labels.",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    audit_path = Path(args.audit_report)
    codex_dir = Path(args.codex_sessions_dir)
    claude_dir = Path(args.claude_sessions_dir)
    if not codex_dir.exists():
        print(f"Codex sessions directory does not exist: {codex_dir}", file=sys.stderr)
        return 1
    if not claude_dir.exists():
        print(f"Claude sessions directory does not exist: {claude_dir}", file=sys.stderr)
        return 1

    try:
        v4_2_cluster_labels = load_v4_2_cluster_labels(args.v4_2_cluster_labels)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to load v4.2 cluster labels: {exc}", file=sys.stderr)
        return 1

    report = build_report(out_dir, audit_path, codex_dir, claude_dir, v4_2_cluster_labels)
    print(f"Report written to: {out_dir}")
    for sec_name in ("combined", "codex", "claude"):
        sec = report[sec_name]
        print(
            f"{sec_name}: sessions={sec['totals']['session_count']} "
            f"planning={sec['totals']['planning_session_count']} "
            f"execution={sec['totals']['execution_session_count']} "
            f"cost={fmt_money(sec['totals']['estimated_cost_usd'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
