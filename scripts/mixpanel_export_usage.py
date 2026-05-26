#!/usr/bin/env python3
"""Export usage report artifacts to Mixpanel."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def to_optional_float(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def normalize_preview(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text[:limit]


def normalize_label(text: str, limit: int = 72) -> str:
    text = " ".join(text.split())
    text = re.sub(r"^#+\s*", "", text).strip(" :-#`'\"")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text.lower()).strip("_")
    return text[:limit].strip("_") or "uncategorized"


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_report_date() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def report_epoch(report_date: str) -> int:
    parsed = datetime.strptime(report_date, "%Y-%m-%d").date()
    return int(datetime.combine(parsed, dt_time(12, 0), tzinfo=timezone.utc).timestamp())


def row_report_date(row: dict[str, str], fallback: str) -> str:
    value = (row.get("session_date") or "").strip()
    if not value:
        return fallback
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return fallback
    return value


def is_after_report_date(value: str, report_date: str) -> bool:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() > datetime.strptime(report_date, "%Y-%m-%d").date()
    except ValueError:
        return False


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON at {path}: {exc}") from exc


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def insert_id(report_date: str, family: str, key: str) -> str:
    return f"u3-{digest_text(f'{report_date}|{family}|{key}')[:32]}"


def session_identity(file_path: str) -> str:
    """Return a stable session identifier across different machine paths."""
    if not file_path:
        return "unknown-session"
    name = Path(file_path).name
    # Most session files are <id>.jsonl; stem gives stable ID.
    stem = Path(name).stem
    if stem:
        return stem
    return digest_text(file_path)[:24]


@dataclass
class ExportEvent:
    family: str
    event: str
    insert_id: str
    properties: dict[str, Any]


class StateStore:
    def __init__(self, path: Path, max_ids: int) -> None:
        self.path = path
        self.max_ids = max_ids
        self.sent: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return
        raw = payload.get("sent_insert_ids", {})
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            if isinstance(key, str):
                self.sent[key] = to_int(value, int(time.time()))

    def has(self, row_id: str) -> bool:
        return row_id in self.sent

    def add_many(self, row_ids: Iterable[str]) -> None:
        now = int(time.time())
        for row_id in row_ids:
            self.sent[row_id] = now
        if len(self.sent) <= self.max_ids:
            return
        ordered = sorted(self.sent.items(), key=lambda item: item[1], reverse=True)
        self.sent = dict(ordered[: self.max_ids])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"sent_insert_ids": self.sent}, indent=2, sort_keys=True))


def with_common(
    token: str,
    distinct_id: str,
    epoch: int,
    row_id: str,
    report_date: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    base = {
        "token": token,
        "distinct_id": distinct_id,
        "time": epoch,
        "$insert_id": row_id,
        "report_date": report_date,
        "export_version": os.getenv("USAGE_EXPORT_VERSION", "session_date_v3"),
    }
    base.update(extra)
    return base


def build_daily_rollups(report: dict[str, Any], audit: dict[str, Any], token: str, distinct_id: str, epoch: int, report_date: str) -> list[ExportEvent]:
    events: list[ExportEvent] = []
    for section_name in ("combined", "codex", "claude"):
        section = report.get(section_name, {})
        totals = section.get("totals", {})
        row_id = insert_id(report_date, "usage_daily_rollup", f"{section_name}:all")
        props = with_common(token, distinct_id, epoch, row_id, report_date, {
            "section": section_name,
            "bucket": "all",
            "estimated_cost_usd": to_float(totals.get("estimated_cost_usd")),
            "session_count": to_int(totals.get("session_count")),
            "planning_session_count": to_int(totals.get("planning_session_count")),
            "execution_session_count": to_int(totals.get("execution_session_count")),
            "effective_input_10pct": to_float(totals.get("effective_input_10pct")),
            "source": "planning_vs_execution_report",
        })
        events.append(ExportEvent("usage_daily_rollup", "usage_daily_rollup", row_id, props))

        for bucket_name in ("planning", "execution"):
            bucket = section.get(bucket_name, {}).get("totals", {})
            row_id = insert_id(report_date, "usage_daily_rollup", f"{section_name}:{bucket_name}")
            props = with_common(token, distinct_id, epoch, row_id, report_date, {
                "section": section_name,
                "bucket": bucket_name,
                "estimated_cost_usd": to_float(bucket.get("estimated_cost_usd")),
                "input_tokens": to_float(bucket.get("input_tokens")),
                "cached_input_tokens": to_float(bucket.get("cached_input_tokens")),
                "output_tokens": to_float(bucket.get("output_tokens")),
                "total_tokens": to_float(bucket.get("total_tokens")),
                "cache_hit_pct": to_float(bucket.get("cache_hit_pct")),
                "source": "planning_vs_execution_report",
            })
            events.append(ExportEvent("usage_daily_rollup", "usage_daily_rollup", row_id, props))

    for provider in ("codex", "claude"):
        dedup_daily = ((audit.get("dedup", {}) or {}).get(provider, {}) or {}).get("ccusageDaily", {})
        row_id = insert_id(report_date, "usage_daily_rollup", f"audit:{provider}:dedup")
        props = with_common(token, distinct_id, epoch, row_id, report_date, {
            "section": provider,
            "bucket": "dedup_daily",
            "input_tokens": to_float(dedup_daily.get("inputTokens")),
            "cached_input_tokens": to_float(dedup_daily.get("cachedInputTokens") or dedup_daily.get("cacheReadTokens")),
            "output_tokens": to_float(dedup_daily.get("outputTokens")),
            "total_tokens": to_float(dedup_daily.get("totalTokens")),
            "estimated_cost_usd": to_float(dedup_daily.get("costUSD") or dedup_daily.get("totalCost")),
            "cache_hit_pct": to_float(dedup_daily.get("cacheHitPct")),
            "source": "cache_hit_audit_report",
        })
        events.append(ExportEvent("usage_daily_rollup", "usage_daily_rollup", row_id, props))
    return events


def build_session_events(rows: list[dict[str, str]], token: str, distinct_id: str, epoch: int, report_date: str) -> list[ExportEvent]:
    events: list[ExportEvent] = []
    for row in rows:
        session_file = row.get("file", "")
        session_id = session_identity(session_file)
        model = row.get("model", "")
        bucket = row.get("bucket", "")
        canonical_key = f"{model}:{bucket}:{session_id}"
        event_date = row_report_date(row, report_date)
        if is_after_report_date(event_date, report_date):
            continue
        row_id = insert_id(event_date, "usage_session", canonical_key)
        props = with_common(token, distinct_id, report_epoch(event_date), row_id, event_date, {
            "model": model,
            "provider": row.get("provider", ""),
            "billable_model": row.get("billable_model", ""),
            "billable_model_source": row.get("billable_model_source", ""),
            "usage_source": row.get("usage_source", ""),
            "bucket": bucket,
            "batch_report_date": report_date,
            "session_id": session_id,
            "session_file": session_file,
            "canonical_key": canonical_key,
            "user_prompts": to_int(row.get("user_prompts")),
            "agent_messages": to_int(row.get("agent_messages")),
            "tool_calls": to_int(row.get("tool_calls")),
            "function_outputs": to_int(row.get("function_outputs")),
            "input_tokens": to_float(row.get("input_tokens")),
            "cache_read_input_tokens": to_float(row.get("cache_read_input_tokens") or row.get("cached_input_tokens")),
            "cached_input_tokens": to_float(row.get("cached_input_tokens")),
            "cache_creation_input_tokens": to_float(row.get("cache_creation_input_tokens")),
            "output_tokens": to_float(row.get("output_tokens")),
            "reasoning_output_tokens": to_float(row.get("reasoning_output_tokens")),
            "total_tokens": to_float(row.get("total_tokens")),
            "cache_hit_pct": to_float(row.get("cache_hit_pct")),
            "estimated_cost_usd": to_float(row.get("estimated_cost_usd")),
            "derived_input_cost_usd": to_optional_float(row.get("derived_input_cost_usd")),
            "derived_non_cache_input_cost_usd": to_optional_float(row.get("derived_non_cache_input_cost_usd")),
            "derived_cache_read_cost_usd": to_optional_float(row.get("derived_cache_read_cost_usd")),
            "derived_cache_creation_cost_usd": to_optional_float(row.get("derived_cache_creation_cost_usd")),
            "derived_output_cost_usd": to_optional_float(row.get("derived_output_cost_usd")),
            "derived_total_cost_usd": to_optional_float(row.get("derived_total_cost_usd")),
            "pricing_missing": to_bool(row.get("pricing_missing")),
            "pricing_source": row.get("pricing_source", ""),
            "session_cwd": row.get("session_cwd", ""),
            "first_prompt_preview": normalize_preview(row.get("first_prompt_preview", ""), 120),
        })
        events.append(ExportEvent("usage_session", "usage_session", row_id, props))
    return events


def build_prompt_events(
    rows: list[dict[str, str]],
    token: str,
    distinct_id: str,
    epoch: int,
    report_date: str,
    max_unique_prompt_hashes: int,
) -> tuple[list[ExportEvent], int]:
    events: list[ExportEvent] = []
    hashes: set[str] = set()
    skipped = 0
    for row in rows:
        preview = row.get("prompt_preview", "")
        preview_hash = digest_text(preview)
        if preview_hash not in hashes and len(hashes) >= max_unique_prompt_hashes:
            skipped += 1
            continue
        hashes.add(preview_hash)

        session_id = session_identity(row.get("file", ""))
        prompt_index = to_int(row.get("prompt_index"))
        canonical_key = f"{row.get('model','')}:{row.get('bucket','')}:{session_id}:{prompt_index}"
        event_date = row_report_date(row, report_date)
        if is_after_report_date(event_date, report_date):
            continue
        row_id = insert_id(
            event_date,
            "usage_prompt",
            canonical_key,
        )
        props = with_common(token, distinct_id, report_epoch(event_date), row_id, event_date, {
            "model": row.get("model", ""),
            "provider": row.get("provider", ""),
            "billable_model": row.get("billable_model", ""),
            "billable_model_source": row.get("billable_model_source", ""),
            "usage_source": row.get("usage_source", ""),
            "bucket": row.get("bucket", ""),
            "batch_report_date": report_date,
            "session_id": session_id,
            "session_file": row.get("file", ""),
            "prompt_index": prompt_index,
            "prompt_hash": preview_hash,
            "prompt_preview": normalize_preview(preview),
            "session_cwd": row.get("session_cwd", ""),
            "previous_prompt_preview": normalize_preview(row.get("previous_prompt_preview", ""), 160),
            "first_prompt_preview": normalize_preview(row.get("first_prompt_preview", ""), 160),
            "final_answer_preview": normalize_preview(row.get("final_answer_preview", ""), 160),
            "canonical_key": canonical_key,
            "tool_calls": to_int(row.get("tool_calls")),
            "agent_messages": to_int(row.get("agent_messages")),
            "response_messages": to_int(row.get("response_messages")),
            "function_outputs": to_int(row.get("function_outputs")),
            "input_tokens_delta": to_float(row.get("input_tokens_delta")),
            "cache_read_tokens_delta": to_float(row.get("cache_read_tokens_delta") or row.get("cached_tokens_delta")),
            "cached_tokens_delta": to_float(row.get("cached_tokens_delta")),
            "cache_creation_tokens_delta": to_float(row.get("cache_creation_tokens_delta")),
            "output_tokens_delta": to_float(row.get("output_tokens_delta")),
            "reasoning_tokens_delta": to_float(row.get("reasoning_tokens_delta")),
            "total_tokens_delta": to_float(row.get("total_tokens_delta")),
            "cache_hit_pct": to_float(row.get("cache_hit_pct")),
            "estimated_cost_usd": to_float(row.get("estimated_cost_usd")),
            "derived_input_cost_usd": to_optional_float(row.get("derived_input_cost_usd")),
            "derived_non_cache_input_cost_usd": to_optional_float(row.get("derived_non_cache_input_cost_usd")),
            "derived_cache_read_cost_usd": to_optional_float(row.get("derived_cache_read_cost_usd")),
            "derived_cache_creation_cost_usd": to_optional_float(row.get("derived_cache_creation_cost_usd")),
            "derived_output_cost_usd": to_optional_float(row.get("derived_output_cost_usd")),
            "derived_total_cost_usd": to_optional_float(row.get("derived_total_cost_usd")),
            "pricing_missing": to_bool(row.get("pricing_missing")),
            "pricing_source": row.get("pricing_source", ""),
        })
        events.append(ExportEvent("usage_prompt", "usage_prompt", row_id, props))
    return events, skipped


def classify_request_pattern(preview: str) -> str:
    normalized = " ".join(preview.split())
    lower = normalized.lower()
    if "a previous agent produced the plan below" in lower:
        return "previous_agent_plan"
    if lower.startswith("implement the plan"):
        return "implement_plan"
    if "[upstream task:" in lower:
        return "upstream_task_handoff"
    if "auto-stamp" in lower or re.search(r"\b(ci|rebase|workflow|mergify)\b", lower):
        return "auto_stamp_ci_loop"
    if any(marker in lower for marker in ("another worktree", "worktree", "ssh machine", "ssh machines")):
        return "worktree_ssh_delegation"
    if any(marker in lower for marker in ("keep going", "continue fixing", "fix ci", "run ./run.sh", "root cause", "repro script")):
        return "run_fix_repro_loop"
    return "other"


def classify_request_subpattern(preview: str, pattern: str) -> str:
    lower = " ".join(preview.split()).lower()
    checks = (
        ("release_packaging", (r"\brelease\b", r"\bpackage\b", r"\bversion\b", r"\bchangelog\b")),
        ("dependency_setup", (r"\bdependency\b", r"\bdependencies\b", r"\binstall\b", r"\bpnpm\b", r"\bnpm\b", r"\bpip\b", r"\bbundler\b")),
        ("pr_review", (r"\breview\b", r"\bpr\b", r"\bpull request\b")),
        ("invoker_plan_submission", (r"\binvoker\b", r"\bplan-to-invoker\b", r"\bsubmit to invoker\b")),
        ("ui_terminal_visual", (r"\bterminal\b", r"\bscreenshot\b", r"\bvisual\b", r"\bplaywright\b", r"\bui\b")),
        ("git_branch_stack", (r"\bgit\b", r"\bbranch\b", r"\bstack\b", r"\bmerge\b", r"\brebase\b")),
        ("debug_repro", (r"\brepro\b", r"\broot cause\b", r"\bdebug\b", r"\binvestigate\b")),
        ("test_ci_failure", (r"\bci\b", r"\btest failure\b", r"\bfailing test\b", r"\bmake test\b", r"\bpytest\b")),
    )
    for label, patterns in checks:
        if any(re.search(expr, lower) for expr in patterns):
            return label
    if pattern in {"auto_stamp_ci_loop", "run_fix_repro_loop"}:
        return "test_ci_failure" if re.search(r"\bci\b|\btest\b", lower) else "debug_repro"
    if pattern == "worktree_ssh_delegation":
        return "git_branch_stack"
    return "uncategorized"


def first_markdown_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            candidate = stripped.lstrip("#").strip()
            if candidate:
                return candidate
    return ""


def first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        candidate = line.strip(" -#`:\t")
        if not candidate:
            continue
        lower = candidate.lower()
        if lower.startswith(("<environment_context>", "summary", "key changes", "tests and validation", "assumptions")):
            continue
        if len(candidate) > 3:
            return candidate
    return ""


def cwd_label(cwd: str) -> str:
    if not cwd:
        return ""
    path = Path(cwd)
    parts = [part for part in path.parts if part and part not in {"/", "Users", "edbertchan", ".invoker", "worktrees", "merge-clones"}]
    if not parts:
        return path.name
    return parts[-1]


def derive_task_label(row: dict[str, str], pattern: str) -> tuple[str, str, str]:
    preview = row.get("prompt_preview", "")
    previous = row.get("previous_prompt_preview", "")
    first = row.get("first_prompt_preview", "")
    final = row.get("final_answer_preview", "")
    cwd = row.get("session_cwd", "")

    candidates: list[tuple[str, str, str]] = []
    if pattern == "implement_plan" and previous:
        candidates.append((previous, "previous_prompt", "high"))
    if first:
        candidates.append((first, "first_prompt", "high" if pattern != "other" else "medium"))
    if previous:
        candidates.append((previous, "previous_prompt", "medium"))
    if cwd:
        candidates.append((cwd_label(cwd), "session_cwd", "medium"))
    if final:
        candidates.append((final, "final_answer", "low"))
    candidates.append((preview, "prompt_preview", "low"))

    for text, source, confidence in candidates:
        label_text = first_markdown_heading(text) or first_meaningful_line(text) or text
        label = normalize_label(label_text)
        if label and label not in {"implement_the_plan", "other", "continue", "keep_going"}:
            return label, source, confidence
    return "uncategorized", "prompt_preview", "low"


def repeated_value_rows(audit: dict[str, Any], model: str) -> list[dict[str, Any]]:
    key = "topCodexRepeatedValues" if model == "codex" else "topClaudeRepeatedValues" if model == "claude" else ""
    if not key:
        return []
    rows = ((audit.get("repeatBreakdown", {}) or {}).get(key, []) or [])
    return sorted(rows, key=lambda row: to_float(row.get("tokenEstimateTotal")), reverse=True)


def pattern_source(model: str, pattern: str) -> str:
    prompt_context_patterns = {
        "previous_agent_plan",
        "implement_plan",
        "upstream_task_handoff",
        "worktree_ssh_delegation",
        "run_fix_repro_loop",
    }
    if pattern not in prompt_context_patterns:
        return ""
    if model == "codex":
        return "codex.user_message_prefix180"
    if model == "claude":
        return "claude.enqueue_content"
    return ""


def source_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw_value = str(row.get("value", ""))
    return {
        "source": str(row.get("source", "")),
        "value_hash": digest_text(raw_value),
        "source_preview": normalize_preview(raw_value, 120),
        "source_occurrence_count": to_int(row.get("count")),
        "source_chars": to_int(row.get("chars")),
        "estimated_source_tokens_per_request": to_float(row.get("tokenEstimatePerValue")),
        "source_token_estimate_total": to_float(row.get("tokenEstimateTotal")),
        "source_shrinkability_score": to_float(row.get("shrinkabilityScore")),
    }


def choose_primary_cache_source(audit: dict[str, Any], model: str, pattern: str) -> tuple[dict[str, Any], str, str]:
    rows = repeated_value_rows(audit, model)
    preferred_source = pattern_source(model, pattern)
    if preferred_source:
        for row in rows:
            if str(row.get("source", "")) == preferred_source:
                return source_payload(row), "prompt_pattern", "medium"
    if rows:
        return source_payload(rows[0]), "global_repeated_context", "low"
    return {}, "none", "low"


def request_base_payload(row: dict[str, str], report_date: str) -> tuple[str, str, int, str, dict[str, Any]]:
    preview = row.get("prompt_preview", "")
    session_id = session_identity(row.get("file", ""))
    prompt_index = to_int(row.get("prompt_index"))
    canonical_key = f"{row.get('model','')}:{row.get('bucket','')}:{session_id}:{prompt_index}"
    event_date = row_report_date(row, report_date)
    pattern = classify_request_pattern(preview)
    subpattern = classify_request_subpattern(preview, pattern)
    task_label, task_label_source, task_label_confidence = derive_task_label(row, pattern)
    payload = {
        "model": row.get("model", ""),
        "provider": row.get("provider", ""),
        "billable_model": row.get("billable_model", ""),
        "billable_model_source": row.get("billable_model_source", ""),
        "usage_source": row.get("usage_source", ""),
        "bucket": row.get("bucket", ""),
        "batch_report_date": report_date,
        "session_id": session_id,
        "session_file": row.get("file", ""),
        "prompt_index": prompt_index,
        "prompt_hash": digest_text(preview),
        "prompt_preview": normalize_preview(preview),
        "session_cwd": row.get("session_cwd", ""),
        "previous_prompt_preview": normalize_preview(row.get("previous_prompt_preview", ""), 160),
        "first_prompt_preview": normalize_preview(row.get("first_prompt_preview", ""), 160),
        "final_answer_preview": normalize_preview(row.get("final_answer_preview", ""), 160),
        "canonical_key": canonical_key,
        "request_pattern": pattern,
        "request_subpattern": subpattern,
        "task_label": task_label,
        "task_label_source": task_label_source,
        "task_label_confidence": task_label_confidence,
        "tool_calls": to_int(row.get("tool_calls")),
        "agent_messages": to_int(row.get("agent_messages")),
        "response_messages": to_int(row.get("response_messages")),
        "function_outputs": to_int(row.get("function_outputs")),
        "input_tokens_delta": to_float(row.get("input_tokens_delta")),
        "cache_read_tokens_delta": to_float(row.get("cache_read_tokens_delta") or row.get("cached_tokens_delta")),
        "cached_tokens_delta": to_float(row.get("cached_tokens_delta")),
        "cache_creation_tokens_delta": to_float(row.get("cache_creation_tokens_delta")),
        "output_tokens_delta": to_float(row.get("output_tokens_delta")),
        "reasoning_tokens_delta": to_float(row.get("reasoning_tokens_delta")),
        "total_tokens_delta": to_float(row.get("total_tokens_delta")),
        "cache_hit_pct": to_float(row.get("cache_hit_pct")),
        "estimated_cost_usd": to_float(row.get("estimated_cost_usd")),
        "derived_input_cost_usd": to_optional_float(row.get("derived_input_cost_usd")),
        "derived_non_cache_input_cost_usd": to_optional_float(row.get("derived_non_cache_input_cost_usd")),
        "derived_cache_read_cost_usd": to_optional_float(row.get("derived_cache_read_cost_usd")),
        "derived_cache_creation_cost_usd": to_optional_float(row.get("derived_cache_creation_cost_usd")),
        "derived_output_cost_usd": to_optional_float(row.get("derived_output_cost_usd")),
        "derived_total_cost_usd": to_optional_float(row.get("derived_total_cost_usd")),
        "pricing_missing": to_bool(row.get("pricing_missing")),
        "pricing_source": row.get("pricing_source", ""),
        "diagnosis_version": os.getenv("USAGE_DIAGNOSIS_VERSION", "request_cache_sources_v3"),
        "source_attribution_method": "provider_metric_exact_source_estimated",
    }
    return canonical_key, event_date, prompt_index, pattern, payload


def build_request_cache_diagnosis_events(
    rows: list[dict[str, str]],
    audit: dict[str, Any],
    token: str,
    distinct_id: str,
    report_date: str,
    max_unique_prompt_hashes: int,
) -> tuple[list[ExportEvent], int]:
    events: list[ExportEvent] = []
    hashes: set[str] = set()
    skipped = 0
    for row in rows:
        preview_hash = digest_text(row.get("prompt_preview", ""))
        if preview_hash not in hashes and len(hashes) >= max_unique_prompt_hashes:
            skipped += 1
            continue
        hashes.add(preview_hash)

        canonical_key, event_date, _prompt_index, pattern, payload = request_base_payload(row, report_date)
        if is_after_report_date(event_date, report_date):
            continue
        primary, kind, confidence = choose_primary_cache_source(audit, row.get("model", ""), pattern)
        row_id = insert_id(event_date, "usage_request_cache_diagnosis", f"{canonical_key}:{payload['diagnosis_version']}")
        props = with_common(token, distinct_id, report_epoch(event_date), row_id, event_date, {
            **payload,
            "primary_cache_driver_source": primary.get("source", ""),
            "primary_cache_driver_kind": kind,
            "source_attribution_confidence": confidence,
            "estimated_source_tokens_per_request": primary.get("estimated_source_tokens_per_request", 0.0),
            "source_token_estimate_total": primary.get("source_token_estimate_total", 0.0),
            "source_occurrence_count": primary.get("source_occurrence_count", 0),
            "source_shrinkability_score": primary.get("source_shrinkability_score", 0.0),
            "source_value_hash": primary.get("value_hash", ""),
            "source_preview": primary.get("source_preview", ""),
        })
        events.append(ExportEvent("usage_request_cache_diagnosis", "usage_request_cache_diagnosis", row_id, props))
    return events, skipped


def build_request_cache_source_events(
    rows: list[dict[str, str]],
    audit: dict[str, Any],
    token: str,
    distinct_id: str,
    report_date: str,
    max_unique_prompt_hashes: int,
    max_sources_per_request: int,
) -> tuple[list[ExportEvent], int]:
    events: list[ExportEvent] = []
    hashes: set[str] = set()
    skipped = 0
    for row in rows:
        preview_hash = digest_text(row.get("prompt_preview", ""))
        if preview_hash not in hashes and len(hashes) >= max_unique_prompt_hashes:
            skipped += 1
            continue
        hashes.add(preview_hash)

        canonical_key, event_date, _prompt_index, pattern, payload = request_base_payload(row, report_date)
        if is_after_report_date(event_date, report_date):
            continue
        candidates = repeated_value_rows(audit, row.get("model", ""))
        preferred_source = pattern_source(row.get("model", ""), pattern)
        selected: list[dict[str, Any]] = []
        if preferred_source:
            selected.extend(row for row in candidates if str(row.get("source", "")) == preferred_source)
        for candidate in candidates:
            if len(selected) >= max_sources_per_request:
                break
            candidate_hash = digest_text(str(candidate.get("value", "")))
            if any(digest_text(str(existing.get("value", ""))) == candidate_hash for existing in selected):
                continue
            selected.append(candidate)

        for source_rank, candidate in enumerate(selected[:max_sources_per_request], start=1):
            source = source_payload(candidate)
            kind = "prompt_pattern" if preferred_source and source["source"] == preferred_source else "global_repeated_context"
            confidence = "medium" if kind == "prompt_pattern" else "low"
            source_key = f"{canonical_key}:{payload['diagnosis_version']}:{source['source']}:{source['value_hash']}"
            row_id = insert_id(event_date, "usage_request_cache_source", source_key)
            props = with_common(token, distinct_id, report_epoch(event_date), row_id, event_date, {
                **payload,
                "source_rank": source_rank,
                "cache_driver_source": source["source"],
                "source_attribution_kind": kind,
                "source_attribution_confidence": confidence,
                "estimated_source_tokens_per_request": source["estimated_source_tokens_per_request"],
                "source_token_estimate_total": source["source_token_estimate_total"],
                "source_occurrence_count": source["source_occurrence_count"],
                "source_chars": source["source_chars"],
                "source_shrinkability_score": source["source_shrinkability_score"],
                "source_value_hash": source["value_hash"],
                "source_preview": source["source_preview"],
            })
            events.append(ExportEvent("usage_request_cache_source", "usage_request_cache_source", row_id, props))
    return events, skipped


def build_tool_events(rows: list[dict[str, str]], token: str, distinct_id: str, epoch: int, report_date: str) -> list[ExportEvent]:
    events: list[ExportEvent] = []
    for row in rows:
        section = row.get("section", "")
        bucket = row.get("bucket", "")
        dimension = row.get("dimension", "")
        name = row.get("name", "")
        canonical_key = f"{section}:{bucket}:{dimension}:{name}"
        row_id = insert_id(report_date, "usage_tool_breakdown", canonical_key)
        props = with_common(token, distinct_id, epoch, row_id, report_date, {
            "section": section,
            "bucket": bucket,
            "dimension": dimension,
            "name": name,
            "canonical_key": canonical_key,
            "calls": to_int(row.get("calls")),
            "calls_share_pct": to_float(row.get("calls_share_pct")),
            "sessions_with_tool": to_int(row.get("sessions_with_tool")),
            "avg_calls_per_using_session": to_float(row.get("avg_calls_per_using_session")),
            "projected_cost_usd": to_float(row.get("projected_cost_usd")),
            "projected_cost_share_pct": to_float(row.get("projected_cost_share_pct")),
        })
        events.append(ExportEvent("usage_tool_breakdown", "usage_tool_breakdown", row_id, props))
    return events


def build_tool_attribution_events(rows: list[dict[str, str]], token: str, distinct_id: str, epoch: int, report_date: str) -> list[ExportEvent]:
    events: list[ExportEvent] = []
    for row in rows:
        session_id = session_identity(row.get("file", ""))
        prompt_index = to_int(row.get("prompt_index"))
        dimension = row.get("dimension", "")
        name = row.get("name", "")
        canonical_key = f"{row.get('model','')}:{row.get('bucket','')}:{session_id}:{prompt_index}:{dimension}:{name}"
        event_date = row_report_date(row, report_date)
        if is_after_report_date(event_date, report_date):
            continue
        row_id = insert_id(event_date, "usage_tool_attribution", canonical_key)
        props = with_common(token, distinct_id, report_epoch(event_date), row_id, event_date, {
            "model": row.get("model", ""),
            "provider": row.get("provider", ""),
            "billable_model": row.get("billable_model", ""),
            "billable_model_source": row.get("billable_model_source", ""),
            "usage_source": row.get("usage_source", ""),
            "bucket": row.get("bucket", ""),
            "batch_report_date": report_date,
            "session_id": session_id,
            "session_file": row.get("file", ""),
            "prompt_index": prompt_index,
            "dimension": dimension,
            "name": name,
            "canonical_key": canonical_key,
            "calls": to_int(row.get("calls")),
            "prompt_input_tokens": to_float(row.get("prompt_input_tokens")),
            "prompt_cache_read_tokens": to_float(row.get("prompt_cache_read_tokens")),
            "prompt_cache_creation_tokens": to_float(row.get("prompt_cache_creation_tokens")),
            "prompt_output_tokens": to_float(row.get("prompt_output_tokens")),
            "prompt_reasoning_tokens": to_float(row.get("prompt_reasoning_tokens")),
            "prompt_total_tokens": to_float(row.get("prompt_total_tokens")),
            "session_input_tokens": to_float(row.get("session_input_tokens")),
            "session_cache_read_tokens": to_float(row.get("session_cache_read_tokens")),
            "session_cache_creation_tokens": to_float(row.get("session_cache_creation_tokens")),
            "session_output_tokens": to_float(row.get("session_output_tokens")),
            "session_reasoning_tokens": to_float(row.get("session_reasoning_tokens")),
            "session_total_tokens": to_float(row.get("session_total_tokens")),
            "prompt_derived_total_cost_usd": to_optional_float(row.get("prompt_derived_total_cost_usd")),
            "session_derived_total_cost_usd": to_optional_float(row.get("session_derived_total_cost_usd")),
            "allocated_input_tokens": to_float(row.get("allocated_input_tokens")),
            "allocated_cache_read_tokens": to_float(row.get("allocated_cache_read_tokens")),
            "allocated_cache_creation_tokens": to_float(row.get("allocated_cache_creation_tokens")),
            "allocated_output_tokens": to_float(row.get("allocated_output_tokens")),
            "allocated_reasoning_tokens": to_float(row.get("allocated_reasoning_tokens")),
            "allocated_total_tokens": to_float(row.get("allocated_total_tokens")),
            "allocated_total_cost_usd": to_optional_float(row.get("allocated_total_cost_usd")),
            "call_share_pct": to_float(row.get("call_share_pct")),
            "allocation_method": row.get("allocation_method", "prompt_window_even_split"),
            "pricing_missing": to_bool(row.get("pricing_missing")),
        })
        events.append(ExportEvent("usage_tool_attribution", "usage_tool_attribution", row_id, props))
    return events


def build_request_tool_attribution_events(
    rows: list[dict[str, str]],
    prompt_rows: list[dict[str, str]],
    token: str,
    distinct_id: str,
    report_date: str,
) -> list[ExportEvent]:
    events: list[ExportEvent] = []
    prompt_context: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for prompt in prompt_rows:
        canonical_key, event_date, prompt_index, _pattern, payload = request_base_payload(prompt, report_date)
        if is_after_report_date(event_date, report_date):
            continue
        prompt_context[(prompt.get("model", ""), prompt.get("bucket", ""), session_identity(prompt.get("file", "")), prompt_index)] = {
            **payload,
            "request_canonical_key": canonical_key,
        }

    for row in rows:
        session_id = session_identity(row.get("file", ""))
        prompt_index = to_int(row.get("prompt_index"))
        dimension = row.get("dimension", "")
        name = row.get("name", "")
        event_date = row_report_date(row, report_date)
        if is_after_report_date(event_date, report_date):
            continue
        key = (row.get("model", ""), row.get("bucket", ""), session_id, prompt_index)
        context = prompt_context.get(key, {})
        canonical_key = f"{row.get('model','')}:{row.get('bucket','')}:{session_id}:{prompt_index}:{dimension}:{name}"
        diagnosis_version = context.get("diagnosis_version", os.getenv("USAGE_DIAGNOSIS_VERSION", "request_cache_sources_v3"))
        row_id = insert_id(event_date, "usage_request_tool_attribution", f"{canonical_key}:{diagnosis_version}")
        props = with_common(token, distinct_id, report_epoch(event_date), row_id, event_date, {
            "model": row.get("model", ""),
            "provider": row.get("provider", ""),
            "billable_model": row.get("billable_model", ""),
            "billable_model_source": row.get("billable_model_source", ""),
            "usage_source": row.get("usage_source", ""),
            "bucket": row.get("bucket", ""),
            "batch_report_date": report_date,
            "session_id": session_id,
            "session_file": row.get("file", ""),
            "prompt_index": prompt_index,
            "prompt_preview": context.get("prompt_preview", normalize_preview(row.get("prompt_preview", ""))),
            "request_pattern": context.get("request_pattern", "other"),
            "request_subpattern": context.get("request_subpattern", "uncategorized"),
            "task_label": context.get("task_label", "uncategorized"),
            "task_label_source": context.get("task_label_source", ""),
            "task_label_confidence": context.get("task_label_confidence", "low"),
            "diagnosis_version": diagnosis_version,
            "dimension": dimension,
            "name": name,
            "canonical_key": canonical_key,
            "request_canonical_key": context.get("request_canonical_key", ""),
            "calls": to_int(row.get("calls")),
            "prompt_input_tokens": to_float(row.get("prompt_input_tokens")),
            "prompt_cache_read_tokens": to_float(row.get("prompt_cache_read_tokens")),
            "prompt_cache_creation_tokens": to_float(row.get("prompt_cache_creation_tokens")),
            "prompt_output_tokens": to_float(row.get("prompt_output_tokens")),
            "prompt_reasoning_tokens": to_float(row.get("prompt_reasoning_tokens")),
            "prompt_total_tokens": to_float(row.get("prompt_total_tokens")),
            "prompt_derived_total_cost_usd": to_optional_float(row.get("prompt_derived_total_cost_usd")),
            "allocated_input_tokens": to_float(row.get("allocated_input_tokens")),
            "allocated_cache_read_tokens": to_float(row.get("allocated_cache_read_tokens")),
            "allocated_cache_creation_tokens": to_float(row.get("allocated_cache_creation_tokens")),
            "allocated_output_tokens": to_float(row.get("allocated_output_tokens")),
            "allocated_reasoning_tokens": to_float(row.get("allocated_reasoning_tokens")),
            "allocated_total_tokens": to_float(row.get("allocated_total_tokens")),
            "allocated_total_cost_usd": to_optional_float(row.get("allocated_total_cost_usd")),
            "allocation_method": row.get("allocation_method", "prompt_window_even_split"),
        })
        events.append(ExportEvent("usage_request_tool_attribution", "usage_request_tool_attribution", row_id, props))
    return events


def build_cache_driver_events(report: dict[str, Any], audit: dict[str, Any], token: str, distinct_id: str, epoch: int, report_date: str) -> list[ExportEvent]:
    events: list[ExportEvent] = []
    drivers = report.get("cache_hit_drivers", {}) or {}
    for section_name, detail in drivers.items():
        for row in detail.get("source_shares", []) or []:
            source = str(row.get("source", ""))
            canonical_key = f"{section_name}:source_share:{source}"
            row_id = insert_id(report_date, "usage_cache_driver", canonical_key)
            props = with_common(token, distinct_id, epoch, row_id, report_date, {
                "section": section_name,
                "driver_kind": "source_share",
                "source": source,
                "canonical_key": canonical_key,
                "estimated_repeated_tokens": to_float(row.get("estimated_repeated_tokens")),
                "share_pct": to_float(row.get("share_pct")),
            })
            events.append(ExportEvent("usage_cache_driver", "usage_cache_driver", row_id, props))

    for provider, key in (("codex", "topCodexRepeatedValues"), ("claude", "topClaudeRepeatedValues")):
        for row in ((audit.get("repeatBreakdown", {}) or {}).get(key, []) or []):
            raw_value = str(row.get("value", ""))
            source = str(row.get("source", ""))
            value_hash = digest_text(raw_value)
            canonical_key = f"{provider}:repeated_value:{source}:{value_hash}"
            row_id = insert_id(report_date, "usage_cache_driver", canonical_key)
            props = with_common(token, distinct_id, epoch, row_id, report_date, {
                "section": provider,
                "driver_kind": "repeated_value",
                "source": source,
                "canonical_key": canonical_key,
                "occurrence_count": to_int(row.get("count")),
                "chars": to_int(row.get("chars")),
                "token_estimate_per_value": to_float(row.get("tokenEstimatePerValue")),
                "token_estimate_total": to_float(row.get("tokenEstimateTotal")),
                "shrinkability_score": to_float(row.get("shrinkabilityScore")),
                "value_hash": value_hash,
                "value_preview": normalize_preview(raw_value, 120),
            })
            events.append(ExportEvent("usage_cache_driver", "usage_cache_driver", row_id, props))
    return events


def limit_family(events: list[ExportEvent], cap: int) -> tuple[list[ExportEvent], int]:
    if cap > 0 and len(events) > cap:
        return events[:cap], len(events) - cap
    return events, 0


def auth_header() -> str:
    user = os.getenv("MIXPANEL_SERVICE_ACCOUNT_USER", "")
    password = os.getenv("MIXPANEL_SERVICE_ACCOUNT_PASS", "")
    api_secret = os.getenv("MIXPANEL_API_SECRET", "")
    if user and password:
        raw = f"{user}:{password}".encode("utf-8")
    elif api_secret:
        raw = f"{api_secret}:".encode("utf-8")
    else:
        raise RuntimeError(
            "Missing Mixpanel auth. Set MIXPANEL_API_SECRET or MIXPANEL_SERVICE_ACCOUNT_USER + MIXPANEL_SERVICE_ACCOUNT_PASS"
        )
    return "Basic " + base64.b64encode(raw).decode("ascii")


def import_url(endpoint: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["strict"] = "1"
    project_id = os.getenv("MIXPANEL_PROJECT_ID", "")
    if project_id:
        query["project_id"] = project_id
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def send_import_batch(endpoint: str, headers: dict[str, str], batch: list[dict[str, Any]]) -> None:
    body = json.dumps(batch).encode("utf-8")
    request = urllib.request.Request(import_url(endpoint), data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        payload = response.read().decode("utf-8", errors="replace").strip()
        if response.status >= 400:
            raise RuntimeError(f"Mixpanel import failed status={response.status} body={payload}")


def emit_batches(
    events: list[ExportEvent],
    endpoint: str,
    batch_size: int,
    dry_run: bool,
) -> int:
    if dry_run:
        return len(events)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": auth_header(),
    }
    sent = 0
    rows = [{"event": event.event, "properties": event.properties} for event in events]
    for idx in range(0, len(rows), batch_size):
        batch = rows[idx : idx + batch_size]
        try:
            send_import_batch(endpoint, headers, batch)
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Mixpanel import failed status={exc.code} body={payload}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Network error while sending Mixpanel batch: {exc}") from exc
        sent += len(batch)
    return sent


def build_all_events(
    input_root: Path,
    report_date: str,
    token: str,
    distinct_id: str,
    max_events_per_family: int,
    max_unique_prompt_hashes: int,
    max_cache_sources_per_request: int,
) -> tuple[dict[str, list[ExportEvent]], dict[str, int]]:
    audit = read_json(input_root / "cache-hit-audit-report.json")
    report = read_json(input_root / "reports/planning-vs-execution-report.json")
    sessions = read_csv(input_root / "reports/planning-vs-execution-sessions.csv")
    prompts = read_csv(input_root / "reports/planning-vs-execution-prompts.csv")
    tools = read_csv(input_root / "reports/planning-vs-execution-tool-breakdown.csv")
    attribution_path = input_root / "reports/planning-vs-execution-tool-attribution.csv"
    tool_attribution = read_csv(attribution_path) if attribution_path.exists() else []

    epoch = report_epoch(report_date)
    families: dict[str, list[ExportEvent]] = {}
    capped: dict[str, int] = {}

    families["usage_daily_rollup"] = build_daily_rollups(report, audit, token, distinct_id, epoch, report_date)
    families["usage_session"] = build_session_events(sessions, token, distinct_id, epoch, report_date)
    prompt_events, prompt_skipped = build_prompt_events(
        prompts, token, distinct_id, epoch, report_date, max_unique_prompt_hashes
    )
    families["usage_prompt"] = prompt_events
    diagnosis_events, diagnosis_skipped = build_request_cache_diagnosis_events(
        prompts, audit, token, distinct_id, report_date, max_unique_prompt_hashes
    )
    families["usage_request_cache_diagnosis"] = diagnosis_events
    source_events, source_skipped = build_request_cache_source_events(
        prompts, audit, token, distinct_id, report_date, max_unique_prompt_hashes, max_cache_sources_per_request
    )
    families["usage_request_cache_source"] = source_events
    families["usage_tool_breakdown"] = build_tool_events(tools, token, distinct_id, epoch, report_date)
    families["usage_tool_attribution"] = build_tool_attribution_events(tool_attribution, token, distinct_id, epoch, report_date)
    families["usage_request_tool_attribution"] = build_request_tool_attribution_events(
        tool_attribution, prompts, token, distinct_id, report_date
    )
    families["usage_cache_driver"] = build_cache_driver_events(report, audit, token, distinct_id, epoch, report_date)
    if prompt_skipped:
        capped["usage_prompt_prompt_hash_cap"] = prompt_skipped
    if diagnosis_skipped:
        capped["usage_request_cache_diagnosis_prompt_hash_cap"] = diagnosis_skipped
    if source_skipped:
        capped["usage_request_cache_source_prompt_hash_cap"] = source_skipped

    for name, rows in list(families.items()):
        limited, dropped = limit_family(rows, max_events_per_family)
        families[name] = limited
        if dropped:
            capped[f"{name}_family_cap"] = dropped
    return families, capped


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export usage artifacts to Mixpanel.")
    parser.add_argument("--input-root", default=".", help="Repository root containing cache-hit report and reports/ outputs.")
    parser.add_argument("--date", default=default_report_date(), help="Report date (YYYY-MM-DD).")
    parser.add_argument("--state-file", default=os.path.expanduser("~/.session-metrics-cron/usage-metrics/send_state.json"))
    parser.add_argument("--ignore-local-state", action="store_true", help="Do not suppress events using local state file; rely on deterministic $insert_id for Mixpanel dedupe.")
    parser.add_argument("--summary-path", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-events-per-family", type=int, default=to_int(os.getenv("MAX_EVENTS_PER_FAMILY"), 100000))
    parser.add_argument(
        "--max-unique-prompt-hashes",
        type=int,
        default=to_int(os.getenv("MAX_UNIQUE_PROMPT_HASHES_PER_DAY"), 50000),
    )
    parser.add_argument(
        "--max-cache-sources-per-request",
        type=int,
        default=to_int(os.getenv("MAX_CACHE_SOURCES_PER_REQUEST"), 3),
    )
    parser.add_argument("--max-state-ids", type=int, default=to_int(os.getenv("MAX_STATE_IDS"), 500000))
    parser.add_argument("--batch-size", type=int, default=to_int(os.getenv("MIXPANEL_BATCH_SIZE"), 2000))
    parser.add_argument("--endpoint", default=os.getenv("MIXPANEL_ENDPOINT", "https://api.mixpanel.com/import"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = os.getenv("MIXPANEL_TOKEN", "")
    if not token:
        print("Missing required env: MIXPANEL_TOKEN", file=sys.stderr)
        return 1

    distinct_id = os.getenv("MIXPANEL_DISTINCT_ID", "session-metrics-cron")
    input_root = Path(args.input_root).resolve()
    for required in (
        input_root / "cache-hit-audit-report.json",
        input_root / "reports/planning-vs-execution-report.json",
        input_root / "reports/planning-vs-execution-sessions.csv",
        input_root / "reports/planning-vs-execution-prompts.csv",
        input_root / "reports/planning-vs-execution-tool-breakdown.csv",
    ):
        if not required.exists():
            print(f"Missing required input: {required}", file=sys.stderr)
            return 1

    families, capped = build_all_events(
        input_root=input_root,
        report_date=args.date,
        token=token,
        distinct_id=distinct_id,
        max_events_per_family=args.max_events_per_family,
        max_unique_prompt_hashes=args.max_unique_prompt_hashes,
        max_cache_sources_per_request=args.max_cache_sources_per_request,
    )

    state = StateStore(Path(args.state_file).expanduser(), args.max_state_ids)
    to_send: dict[str, list[ExportEvent]] = {}
    duplicate_counts: dict[str, int] = {}
    for family, events in families.items():
        fresh: list[ExportEvent] = []
        dupes = 0
        if args.ignore_local_state:
            fresh = events
        else:
            for event in events:
                if state.has(event.insert_id):
                    dupes += 1
                    continue
                fresh.append(event)
        to_send[family] = fresh
        duplicate_counts[family] = dupes

    ordered = []
    for family in (
        "usage_daily_rollup",
        "usage_session",
        "usage_prompt",
        "usage_request_cache_diagnosis",
        "usage_request_cache_source",
        "usage_tool_breakdown",
        "usage_tool_attribution",
        "usage_request_tool_attribution",
        "usage_cache_driver",
    ):
        ordered.extend(to_send.get(family, []))

    try:
        sent_count = emit_batches(ordered, args.endpoint, args.batch_size, args.dry_run)
    except RuntimeError as exc:
        print(f"Failed to export Mixpanel events: {exc}", file=sys.stderr)
        return 1

    if not args.dry_run:
        state.add_many(event.insert_id for event in ordered)
        state.save()

    summary = {
        "report_date": args.date,
        "dry_run": args.dry_run,
        "total_events_after_dedupe": sent_count,
        "families": {family: len(rows) for family, rows in to_send.items()},
        "duplicates_suppressed": duplicate_counts,
        "capped": capped,
        "ignore_local_state": args.ignore_local_state,
        "state_file": str(Path(args.state_file).expanduser()),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.summary_path:
        Path(args.summary_path).write_text(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
