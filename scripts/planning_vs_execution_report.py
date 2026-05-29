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
COMMAND_ATTRIBUTION_SERVICE_CLASSIFIER_REVISION = "service_context_v2"
COMMAND_COST_ALLOCATION_METHOD = "prompt_cost_output_weighted_v1"


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


def add_command_call(window: PromptWindow | None, function_name: str, arguments: Any, call_id: str = "") -> CommandCall | None:
    if window is None:
        return None
    command = command_text_from_arguments(arguments)
    workdir = workdir_from_arguments(arguments)
    shell_verb = extract_shell_verb(arguments) or ""
    target_type, target = command_target(command, workdir)
    if not target:
        target_type, target = target_from_tool_arguments(arguments)
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


def build_report(out_dir: Path, audit_path: Path, codex_dir: Path, claude_dir: Path) -> dict[str, Any]:
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
    return {
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

    report = build_report(out_dir, audit_path, codex_dir, claude_dir)
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
