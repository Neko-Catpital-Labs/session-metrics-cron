#!/usr/bin/env python3
"""Build benchmark token ledgers and all-in usage rollups."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from usage_costing import (
    build_cost_calculation,
    derive_cost,
    load_pricing_table,
    provider_for_session_family,
    resolve_billable_model,
)


TOKEN_FIELDS = (
    "input_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def number(value: Any) -> float:
    if isinstance(value, (int, float)):
        return value
    return 0


def int_number(value: Any) -> int:
    return int(number(value))


def first_number(payload: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return value
    return 0


def read_json_lines(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return rows
    for line in lines:
        stripped = line.strip()
        if "{" not in stripped:
            continue
        if not stripped.startswith("{"):
            stripped = stripped[stripped.find("{") :]
        try:
            parsed = json.loads(stripped)
        except Exception:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def scenario_key(conversation_file: Path, mode: str, model: str) -> str:
    return f"{conversation_file.stem}/{mode}/{model}"


def parse_plan(plan_path: Path) -> dict[str, Any]:
    text = plan_path.read_text(errors="ignore") if plan_path.exists() else ""
    tasks: dict[str, dict[str, Any]] = {}
    current_id = ""
    in_tasks = False
    in_dep_block = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if re.match(r"^tasks:\s*(?:$|\[)", line):
            in_tasks = True
            continue
        if in_tasks and re.match(r"^[A-Za-z0-9_-]+:", line):
            in_tasks = False
        if not in_tasks:
            continue

        item_id = re.match(r"^\s*-\s+id:\s*['\"]?([^'\"\s#]+)", line)
        if item_id:
            current_id = item_id.group(1)
            tasks.setdefault(current_id, {"dependencies": [], "has_prompt": False, "has_command": False})
            in_dep_block = False
            continue
        if not current_id:
            continue

        inline_id = re.match(r"^\s+id:\s*['\"]?([^'\"\s#]+)", line)
        if inline_id:
            current_id = inline_id.group(1)
            tasks.setdefault(current_id, {"dependencies": [], "has_prompt": False, "has_command": False})
            in_dep_block = False
            continue

        if re.match(r"^\s+prompt:\s*", line):
            tasks[current_id]["has_prompt"] = True
            in_dep_block = False
            continue
        if re.match(r"^\s+command:\s*", line):
            tasks[current_id]["has_command"] = True
            in_dep_block = False
            continue
        dep_match = re.match(r"^\s+dependencies:\s*(.*)$", line)
        if dep_match:
            in_dep_block = True
            deps_text = dep_match.group(1).strip()
            if deps_text.startswith("["):
                deps = [part.strip().strip("'\"") for part in deps_text.strip("[]").split(",") if part.strip()]
                tasks[current_id]["dependencies"].extend(deps)
                in_dep_block = False
            continue
        if in_dep_block:
            dep_item = re.match(r"^\s*-\s*['\"]?([^'\"\s#]+)", line)
            if dep_item:
                tasks[current_id]["dependencies"].append(dep_item.group(1))
                continue
            if re.match(r"^\s+[A-Za-z0-9_-]+:", line):
                in_dep_block = False

    return {"tasks": tasks, "task_ids": list(tasks)}


def dependency_closure(tasks: dict[str, dict[str, Any]], roots: set[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    def visit(task_id: str) -> None:
        if not task_id or task_id in seen:
            return
        seen.add(task_id)
        for dep in tasks.get(task_id, {}).get("dependencies", []):
            visit(dep)
        ordered.append(task_id)

    for root in sorted(roots):
        visit(root)
    return ordered


def find_task_id(text: str, task_ids: list[str]) -> str:
    for task_id in sorted(task_ids, key=len, reverse=True):
        if task_id and task_id in text:
            return task_id
    patterns = (
        r'"task[_-]?id"\s*:\s*"([^"]+)"',
        r'"taskId"\s*:\s*"([^"]+)"',
        r"\btask[_ -]?id[=:]\s*([A-Za-z0-9_.:/-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def nested_value(obj: Any, keys: set[str]) -> str:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and isinstance(value, str) and value.strip():
                return value.strip()
            found = nested_value(value, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = nested_value(item, keys)
            if found:
                return found
    return ""


def normalize_usage(usage: dict[str, Any]) -> dict[str, int]:
    input_tokens = first_number(usage, "input_tokens", "inputTokens", "prompt_tokens", "promptTokens")
    cache_read = first_number(
        usage,
        "cache_read_input_tokens",
        "cacheReadInputTokens",
        "cached_input_tokens",
        "cachedInputTokens",
        "cached_tokens",
    )
    cache_creation = first_number(usage, "cache_creation_input_tokens", "cacheCreationInputTokens")
    output_tokens = first_number(usage, "output_tokens", "outputTokens", "completion_tokens", "completionTokens")
    reasoning = first_number(usage, "reasoning_tokens", "reasoningTokens", "reasoning_output_tokens")
    total = first_number(usage, "total_tokens", "totalTokens")
    if not total:
        total = input_tokens + cache_read + cache_creation + output_tokens + reasoning
    return {
        "input_tokens": int(input_tokens),
        "cache_read_tokens": int(cache_read),
        "cache_creation_tokens": int(cache_creation),
        "output_tokens": int(output_tokens),
        "reasoning_tokens": int(reasoning),
        "total_tokens": int(total),
    }


def row_hash(parts: list[Any]) -> str:
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def phase_for(task_id: str, event_text: str, path_text: str, mode: str) -> str:
    lowered_event = event_text.lower()
    lowered_path = path_text.lower()
    if task_id:
        if "autofix" in lowered_event or "auto-fix" in lowered_event or "fix" in lowered_event or "retry" in lowered_event:
            return "invoker_autofix_retry"
        return "invoker_prompt_task"
    if mode == "baseline_direct":
        return "planning"
    if any(marker in lowered_path for marker in ("invoker-db", "worktrees", "/repos/", "task")):
        return "unknown_model_call"
    return "planning"


def make_row(
    *,
    scenario: str,
    phase: str,
    task_id: str,
    agent_session_id: str,
    provider: str,
    model: str,
    tokens: dict[str, int],
    observed_cost: float | None,
    pricing_table: dict[str, Any],
    source: str,
    source_path: str,
    benchmark_model: str,
) -> dict[str, Any]:
    billable_model, billable_source = resolve_billable_model(provider or benchmark_model, model)
    cost = derive_cost(
        pricing_table,
        billable_model,
        input_tokens=float(tokens["input_tokens"]),
        cache_read_tokens=float(tokens["cache_read_tokens"]),
        cache_creation_tokens=float(tokens["cache_creation_tokens"]),
        output_tokens=float(tokens["output_tokens"]),
        input_includes_cache=provider in {"openai", "codex"},
    )
    pricing_missing = bool(cost.get("pricing_missing"))
    estimated = cost.get("derived_total_cost_usd")
    pricing_source = cost.get("pricing_source")
    if estimated is None and observed_cost is not None:
        estimated = observed_cost
        pricing_missing = False
        pricing_source = "session_log_cost"
    if estimated is None:
        estimated = 0.0
    row = {
        "scenario_key": scenario,
        "phase": phase,
        "task_id": task_id,
        "agent_session_id": agent_session_id,
        "provider": provider,
        "model": model,
        "billable_model": billable_model,
        "billable_model_source": billable_source,
        **tokens,
        "estimated_cost_usd": estimated,
        "derived_total_cost_usd": cost.get("derived_total_cost_usd"),
        "pricing_missing": pricing_missing,
        "pricing_source": pricing_source or "missing",
        "source": source,
        "source_path": source_path,
    }
    row["model_call_id"] = row_hash(
        [
            row["scenario_key"],
            row["phase"],
            row["task_id"],
            row["agent_session_id"],
            row["provider"],
            row["model"],
            [row[field] for field in TOKEN_FIELDS],
        ]
    )
    return row


def rows_from_object(
    obj: dict[str, Any],
    *,
    raw_text: str,
    source_path: Path,
    scenario: str,
    mode: str,
    benchmark_model: str,
    task_ids: list[str],
    pricing_table: dict[str, Any],
) -> list[dict[str, Any]]:
    source_text = f"{source_path} {raw_text}"
    task_id = nested_value(obj, {"task_id", "taskId"}) or find_task_id(source_text, task_ids)
    session_id = nested_value(obj, {"agent_session_id", "agentSessionId", "session_id", "sessionId"}) or source_path.stem
    provider = provider_for_session_family(benchmark_model) or benchmark_model
    phase = phase_for(task_id, raw_text, str(source_path), mode)
    rows: list[dict[str, Any]] = []

    model_usage = obj.get("modelUsage")
    if isinstance(model_usage, dict):
        for model_name, usage in model_usage.items():
            if not isinstance(usage, dict):
                continue
            tokens = normalize_usage(usage)
            if not any(tokens[field] for field in TOKEN_FIELDS):
                continue
            rows.append(
                make_row(
                    scenario=scenario,
                    phase=phase,
                    task_id=task_id,
                    agent_session_id=session_id,
                    provider=provider,
                    model=str(model_name),
                    tokens=tokens,
                    observed_cost=number(usage.get("costUSD")) if isinstance(usage.get("costUSD"), (int, float)) else None,
                    pricing_table=pricing_table,
                    source="session_model_usage",
                    source_path=str(source_path),
                    benchmark_model=benchmark_model,
                )
            )
        if rows:
            return rows

    candidates: list[tuple[dict[str, Any], str]] = []
    payload = obj.get("payload")
    if isinstance(payload, dict) and payload.get("type") == "token_count":
        info = payload.get("info")
        total_usage = info.get("total_token_usage") if isinstance(info, dict) else None
        if isinstance(total_usage, dict):
            candidates.append((total_usage, "codex_token_count"))
    usage = obj.get("usage")
    if isinstance(usage, dict):
        candidates.append((usage, "session_usage"))
    message = obj.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        candidates.append((message["usage"], "session_message_usage"))
    if not candidates and any(key in obj for key in ("input_tokens", "inputTokens", "prompt_tokens", "output_tokens")):
        candidates.append((obj, "stdout_usage"))

    out: list[dict[str, Any]] = []
    for usage_payload, source in candidates:
        tokens = normalize_usage(usage_payload)
        if not any(tokens[field] for field in TOKEN_FIELDS):
            continue
        model_name = (
            nested_value(obj, {"model", "modelName"})
            or nested_value(usage_payload, {"model", "modelName"})
            or ""
        )
        observed = None
        for cost_key in ("estimated_cost_usd", "costUSD", "total_cost_usd", "totalCost"):
            if isinstance(obj.get(cost_key), (int, float)):
                observed = number(obj[cost_key])
                break
            if isinstance(usage_payload.get(cost_key), (int, float)):
                observed = number(usage_payload[cost_key])
                break
        out.append(
            make_row(
                scenario=scenario,
                phase=phase,
                task_id=task_id,
                agent_session_id=session_id,
                provider=provider,
                model=model_name,
                tokens=tokens,
                observed_cost=observed,
                pricing_table=pricing_table,
                source=source,
                source_path=str(source_path),
                benchmark_model=benchmark_model,
            )
        )
    return out


def collect_rows(args: argparse.Namespace, plan: dict[str, Any], pricing_table: dict[str, Any]) -> list[dict[str, Any]]:
    scenario = scenario_key(Path(args.conversation_file), args.mode, args.model)
    rows: list[dict[str, Any]] = []
    for path in sorted(Path(args.raw_sessions_dir).rglob("*")):
        if not path.is_file():
            continue
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except Exception:
            continue
        latest_codex_by_session: dict[str, dict[str, Any]] = {}
        immediate: list[dict[str, Any]] = []
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                obj = json.loads(stripped)
            except Exception:
                continue
            row_group = rows_from_object(
                obj,
                raw_text=stripped,
                source_path=path,
                scenario=scenario,
                mode=args.mode,
                benchmark_model=args.model,
                task_ids=plan["task_ids"],
                pricing_table=pricing_table,
            )
            for row in row_group:
                if row["source"] == "codex_token_count":
                    latest_codex_by_session[row["agent_session_id"]] = row
                else:
                    immediate.append(row)
        rows.extend(immediate)
        rows.extend(latest_codex_by_session.values())

    stdout_path = Path(args.stdout_log)
    if stdout_path.exists():
        for obj in read_json_lines(stdout_path):
            rows.extend(
                rows_from_object(
                    obj,
                    raw_text=json.dumps(obj, sort_keys=True),
                    source_path=stdout_path,
                    scenario=scenario,
                    mode=args.mode,
                    benchmark_model=args.model,
                    task_ids=plan["task_ids"],
                    pricing_table=pricing_table,
                )
            )

    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["model_call_id"]
        if key not in deduped or deduped[key]["source"].startswith("stdout"):
            deduped[key] = row
    return sorted(deduped.values(), key=lambda item: (item["phase"], item["task_id"], item["agent_session_id"], item["model_call_id"]))


def build_summary(args: argparse.Namespace, plan: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    scenario = scenario_key(Path(args.conversation_file), args.mode, args.model)
    totals: dict[str, Any] = {field: 0 for field in TOKEN_FIELDS}
    totals["estimated_cost_usd"] = 0.0
    phase_cost = defaultdict(float)
    phase_tokens: dict[str, dict[str, int]] = defaultdict(lambda: {field: 0 for field in TOKEN_FIELDS})
    prompt_tasks: set[str] = set()
    autofix_tasks: set[str] = set()
    cost_tasks: set[str] = set()
    complete = True

    for row in rows:
        for field in TOKEN_FIELDS:
            value = int_number(row.get(field))
            totals[field] += value
            phase_tokens[row["phase"]][field] += value
        cost_value = number(row.get("estimated_cost_usd"))
        totals["estimated_cost_usd"] += cost_value
        phase_cost[row["phase"]] += cost_value
        if row.get("task_id"):
            cost_tasks.add(str(row["task_id"]))
            if row["phase"] == "invoker_autofix_retry":
                autofix_tasks.add(str(row["task_id"]))
            elif row["phase"] == "invoker_prompt_task":
                prompt_tasks.add(str(row["task_id"]))
        if row.get("pricing_missing") or row.get("phase") == "unknown_model_call":
            complete = False

    totals["fresh_input_tokens"] = max(totals["input_tokens"] - totals["cache_read_tokens"], 0) + totals["cache_creation_tokens"]
    totals["normalized_total_tokens"] = totals["fresh_input_tokens"] + totals["output_tokens"] + totals["reasoning_tokens"]
    dependent_tasks = dependency_closure(plan["tasks"], cost_tasks)
    pricing_missing = any(bool(row.get("pricing_missing")) for row in rows)
    billable_model = ""
    billable_model_source = ""
    pricing_source = ""
    for row in rows:
        if row.get("billable_model") and not billable_model:
            billable_model = row["billable_model"]
            billable_model_source = row.get("billable_model_source", "")
        if row.get("pricing_source") and not pricing_source:
            pricing_source = row["pricing_source"]

    summary = {
        "scenario_key": scenario,
        "phase": "all_in",
        "input_tokens": totals["input_tokens"],
        "cache_read_tokens": totals["cache_read_tokens"],
        "cache_creation_tokens": totals["cache_creation_tokens"],
        "fresh_input_tokens": totals["fresh_input_tokens"],
        "output_tokens": totals["output_tokens"],
        "reasoning_tokens": totals["reasoning_tokens"],
        "total_tokens": totals["total_tokens"],
        "normalized_total_tokens": totals["normalized_total_tokens"],
        "estimated_cost_usd": totals["estimated_cost_usd"],
        "derived_total_cost_usd": totals["estimated_cost_usd"],
        "planning_cost_usd": phase_cost["planning"],
        "invoker_prompt_task_cost_usd": phase_cost["invoker_prompt_task"],
        "autofix_retry_cost_usd": phase_cost["invoker_autofix_retry"],
        "unknown_model_call_cost_usd": phase_cost["unknown_model_call"],
        "planning_tokens": phase_tokens["planning"],
        "invoker_prompt_task_tokens": phase_tokens["invoker_prompt_task"],
        "autofix_retry_tokens": phase_tokens["invoker_autofix_retry"],
        "unknown_model_call_tokens": phase_tokens["unknown_model_call"],
        "model_call_count": len(rows),
        "prompt_model_call_count": sum(1 for row in rows if row["phase"] == "invoker_prompt_task"),
        "autofix_model_call_count": sum(1 for row in rows if row["phase"] == "invoker_autofix_retry"),
        "cost_task_ids": sorted(cost_tasks),
        "dependent_task_ids": dependent_tasks,
        "dependent_prompt_task_ids": sorted(prompt_tasks),
        "dependent_autofix_task_ids": sorted(autofix_tasks),
        "cost_breakdown_complete": complete,
        "pricing_missing": pricing_missing,
        "pricing_source": pricing_source or "missing",
        "billable_model": billable_model,
        "billable_model_source": billable_model_source,
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-sessions-dir", required=True)
    parser.add_argument("--stdout-log", required=True)
    parser.add_argument("--generated-plan", required=True)
    parser.add_argument("--token-usage-out", required=True)
    parser.add_argument("--ledger-out", required=True)
    parser.add_argument("--cost-calculation-out", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--conversation-file", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--pricing-source", default="")
    args = parser.parse_args()

    pricing_table = load_pricing_table(args.pricing_source or None)
    plan = parse_plan(Path(args.generated_plan))
    rows = collect_rows(args, plan, pricing_table)
    summary = build_summary(args, plan, rows)

    ledger_path = Path(args.ledger_out)
    ledger_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    Path(args.token_usage_out).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    aggregate_cost = {
        "derived_total_cost_usd": summary.get("derived_total_cost_usd"),
        "pricing_missing": summary.get("pricing_missing"),
        "pricing_source": summary.get("pricing_source"),
        "billable_non_cache_input_tokens": max(
            0,
            summary.get("input_tokens", 0) - summary.get("cache_read_tokens", 0) - summary.get("cache_creation_tokens", 0),
        ),
        "pricing_input_cost_per_token": None,
        "pricing_cache_read_input_token_cost": None,
        "pricing_cache_creation_input_token_cost": None,
        "pricing_output_cost_per_token": None,
        "derived_non_cache_input_cost_usd": None,
        "derived_cache_read_cost_usd": None,
        "derived_cache_creation_cost_usd": None,
        "derived_output_cost_usd": None,
    }
    cost_calculation = build_cost_calculation(
        batch_id=args.batch_id,
        run_id=args.run_id,
        test_id=args.run_id.split("__", 1)[0] or Path(args.conversation_file).stem,
        model=args.model,
        scenario=args.mode,
        billable_model=summary.get("billable_model") or "",
        billable_model_source=summary.get("billable_model_source") or "",
        token_totals=summary,
        cost=aggregate_cost,
    )
    cost_calculation["scenario_key"] = summary["scenario_key"]
    cost_calculation["ledger_rows"] = len(rows)
    cost_calculation["cost_breakdown_complete"] = summary["cost_breakdown_complete"]
    Path(args.cost_calculation_out).write_text(json.dumps(cost_calculation, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
