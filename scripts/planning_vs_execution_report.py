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
import json
import re
import statistics
import sys
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
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
DEFAULT_PRICING_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"


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


def none_if_missing(value: float | None) -> float | None:
    return value if value is not None else None


def shorten(text: str, n: int = 220) -> str:
    return " ".join(text.split())[:n]


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


def normalize_model_name(value: str) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def default_billable_model(provider: str) -> tuple[str, str]:
    if provider == "codex":
        return "gpt-5.5", "default_codex"
    if provider == "claude":
        return "claude-sonnet-4-5-20250929", "default_claude"
    return "", "missing"


def provider_for_session_family(model: str) -> str:
    if model == "codex":
        return "openai"
    if model == "claude":
        return "anthropic"
    return ""


def resolve_billable_model(provider: str, observed: str) -> tuple[str, str]:
    normalized = normalize_model_name(observed)
    if normalized:
        return normalized, "session_log"
    return default_billable_model(provider)


def load_pricing_table(path_or_url: str | None) -> dict[str, Any]:
    source = path_or_url or DEFAULT_PRICING_URL
    try:
        if source.startswith(("http://", "https://")):
            with urllib.request.urlopen(source, timeout=20) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        path = Path(source).expanduser()
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        return {}
    return {}


def pricing_for_model(pricing_table: dict[str, Any], model: str) -> dict[str, Any] | None:
    if not model:
        return None
    candidates = [model, model.lower(), model.replace("openai/", ""), model.replace("anthropic/", "")]
    for candidate in candidates:
        row = pricing_table.get(candidate)
        if isinstance(row, dict):
            return row
    return None


def price_component(pricing: dict[str, Any] | None, *keys: str) -> float | None:
    if not pricing:
        return None
    for key in keys:
        value = pricing.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def derive_cost(
    pricing_table: dict[str, Any],
    billable_model: str,
    *,
    input_tokens: float,
    cache_read_tokens: float,
    cache_creation_tokens: float,
    output_tokens: float,
    input_includes_cache: bool = False,
) -> dict[str, Any]:
    pricing = pricing_for_model(pricing_table, billable_model)
    input_price = price_component(pricing, "input_cost_per_token", "prompt_cost_per_token")
    output_price = price_component(pricing, "output_cost_per_token", "completion_cost_per_token")
    cache_read_price = price_component(pricing, "cache_read_input_token_cost", "cache_read_cost_per_token")
    cache_creation_price = price_component(pricing, "cache_creation_input_token_cost", "cache_creation_cost_per_token")
    pricing_missing = input_price is None or output_price is None
    billable_input_tokens = max(0.0, input_tokens - cache_read_tokens - cache_creation_tokens) if input_includes_cache else input_tokens
    input_cost = billable_input_tokens * input_price if input_price is not None else None
    cache_read_cost = cache_read_tokens * (cache_read_price if cache_read_price is not None else input_price) if input_price is not None else None
    cache_creation_cost = (
        cache_creation_tokens * (cache_creation_price if cache_creation_price is not None else input_price)
        if input_price is not None
        else None
    )
    output_cost = output_tokens * output_price if output_price is not None else None
    parts = [input_cost, cache_read_cost, cache_creation_cost, output_cost]
    return {
        "derived_input_cost_usd": none_if_missing(input_cost),
        "derived_non_cache_input_cost_usd": none_if_missing(input_cost),
        "derived_cache_read_cost_usd": none_if_missing(cache_read_cost),
        "derived_cache_creation_cost_usd": none_if_missing(cache_creation_cost),
        "derived_output_cost_usd": none_if_missing(output_cost),
        "derived_total_cost_usd": sum(p for p in parts if p is not None) if not pricing_missing else None,
        "pricing_missing": pricing_missing,
        "pricing_source": "litellm_model_prices" if pricing else "missing",
    }


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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    eff_corpus = sum(s.final_input + 0.1 * s.final_cached for s in sessions) or 0.0
    cost_total = float(model_totals.get("costUSD", 0.0))
    cost_per_eff = safe_div(cost_total, eff_corpus)

    session_rows: list[dict[str, Any]] = []
    prompt_rows: list[dict[str, Any]] = []
    attribution_rows: list[dict[str, Any]] = []

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
    return session_rows, prompt_rows, attribution_rows


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


def build_report(out_dir: Path, audit_path: Path, codex_dir: Path, claude_dir: Path) -> dict[str, Any]:
    model_totals = load_model_totals(audit_path)
    pricing_table = load_pricing_table(None)
    repeat_breakdown = load_repeat_breakdown(audit_path)

    codex_sessions = [s for fp in sorted(codex_dir.rglob("*.jsonl")) if (s := parse_codex_session(fp))]
    claude_sessions = [s for fp in sorted(claude_dir.rglob("*.jsonl")) if (s := parse_claude_session(fp))]

    codex_session_rows, codex_prompt_rows, codex_attribution_rows = build_rows_for_model(codex_sessions, model_totals["codex"], pricing_table)
    claude_session_rows, claude_prompt_rows, claude_attribution_rows = build_rows_for_model(claude_sessions, model_totals["claude"], pricing_table)

    all_sessions = codex_sessions + claude_sessions
    all_session_rows = codex_session_rows + claude_session_rows
    all_prompt_rows = codex_prompt_rows + claude_prompt_rows
    all_attribution_rows = codex_attribution_rows + claude_attribution_rows

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

    (out_dir / "planning-vs-execution-summary.md").write_text(render_markdown(report))
    return report


def fmt_int(n: float) -> str:
    return f"{int(n):,}"


def fmt_money(n: float) -> str:
    return f"${n:,.2f}"


def fmt_pct(n: float) -> str:
    return f"{n:.2f}%"


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
