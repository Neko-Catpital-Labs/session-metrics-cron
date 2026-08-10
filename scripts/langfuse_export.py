#!/usr/bin/env python3
"""Export parsed Codex/Claude/omp sessions as Langfuse ingestion payloads.

This intentionally reuses planning_vs_execution_report's session parsers and
does not calculate or send costs. Langfuse should infer cost from model + usage.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from mixpanel_export_usage import (  # noqa: E402
    RequestPatternCategorizer,
    TaskCategorizer,
    derive_task_label,
    load_request_pattern_config,
    load_task_categorization_config,
)
import planning_vs_execution_report as pve  # noqa: E402
from session_phase_narrative_report import (  # noqa: E402
    classify_prompt_window,
    fixing_cause_for,
)


DEFAULT_STAGE_DIR = Path("/tmp/fleet-sessions")
DEFAULT_REQUEST_PATTERN_CONFIG = REPO_ROOT / "config" / "request-patterns.yaml"
DEFAULT_TASK_CATEGORIZATION_CONFIG = REPO_ROOT / "config" / "task-categorization.yaml"
DIAGNOSIS_VERSION = os.getenv("USAGE_DIAGNOSIS_VERSION", "request_pattern_layers_v1")
DRY_RUN_SAMPLE_LIMIT = 5
EXPORT_SOURCE = "codex_session_langfuse_export"
CLASSIFICATION_FIELDS = (
    "fixing_cause",
    "workflow_phase",
    "efficiency_label",
    "task_type",
    "work_motivation",
    "request_pattern",
    "request_origin",
    "agent_tool_intention",
    "origin",
    "host",
    "bucket",
)
OTLP_INGESTION_VERSION = "4"
DEFAULT_OTLP_CHUNK_SIZE = 2_000
PARSERS: dict[str, Callable[[Path], Any | None]] = {
    "codex": pve.parse_codex_session,
    "claude": pve.parse_claude_session,
    "omp": pve.parse_omp_session,
}


@dataclass(frozen=True)
class ParsedSession:
    session: Any
    host: str


def row_hash(parts: list[Any]) -> str:
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def stable_hex(parts: list[Any], length: int) -> str:
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def trace_id_for_session(session: Any) -> str:
    session_key = str(getattr(session, "file", "") or getattr(session, "session_cwd", ""))
    return row_hash(["trace", session_key, str(getattr(session, "session_date", "") or "")])


def otlp_trace_id_for_session(session: Any) -> str:
    session_key = str(getattr(session, "file", "") or getattr(session, "session_cwd", ""))
    return stable_hex(["trace", session_key, str(getattr(session, "session_date", "") or "")], 32)


def otlp_root_span_id(session: Any) -> str:
    return stable_hex(["root-span", trace_id_for_session(session)], 16)


def otlp_generation_span_id(origin: str, file: str, prompt_index: int, start_ts: str, end_ts: str) -> str:
    return stable_hex(["generation-span", origin, file, prompt_index, start_ts, end_ts], 16)


def generation_id(origin: str, file: str, prompt_index: int, start_ts: str, end_ts: str) -> str:
    return row_hash([origin, file, prompt_index, start_ts, end_ts])


def _event_id(event_type: str, body_id: str) -> str:
    return row_hash([event_type, body_id])


def parse_since_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"Invalid --since-date {value!r}; expected YYYY-MM-DD") from exc


def iso8601(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return ""
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.combine(datetime.strptime(raw, "%Y-%m-%d").date(), time.min, tzinfo=timezone.utc)
            except ValueError:
                return ""
    else:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def unix_nanos(value: str) -> str:
    parsed = datetime.fromisoformat((value or "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return str(int(parsed.timestamp() * 1_000_000_000))


def session_date_timestamp(session: Any) -> str:
    return iso8601(str(getattr(session, "session_date", "") or "")) or "1970-01-01T00:00:00Z"


def _line_timestamp(obj: dict[str, Any]) -> str:
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    return iso8601(obj.get("timestamp")) or iso8601(payload.get("timestamp"))


def _codex_new_prompt(obj: dict[str, Any]) -> bool:
    if obj.get("type") != "event_msg":
        return False
    payload = obj.get("payload") or {}
    return payload.get("type") == "user_message" and isinstance(payload.get("message"), str)


def _claude_new_prompt(obj: dict[str, Any]) -> bool:
    if obj.get("type") != "user":
        return False
    msg = obj.get("message") or {}
    return msg.get("role") == "user" and pve._extract_user_text(msg) is not None


def _omp_new_prompt(obj: dict[str, Any]) -> bool:
    if obj.get("type") != "message":
        return False
    msg = obj.get("message") or {}
    return msg.get("role") == "user"


def _infer_session_kind(session: Any) -> str:
    if getattr(session, "origin", "") == "omp":
        return "omp"
    model = str(getattr(session, "model", "") or "")
    if model in {"codex", "claude"}:
        return model
    return ""


def extract_prompt_window_timestamps(path: Path, kind: str) -> dict[int, tuple[str, str]]:
    detectors = {
        "codex": _codex_new_prompt,
        "claude": _claude_new_prompt,
        "omp": _omp_new_prompt,
    }
    is_new_prompt = detectors.get(kind)
    if is_new_prompt is None:
        return {}
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return {}

    windows: dict[int, list[str]] = {}
    current_index = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _line_timestamp(obj)
        if is_new_prompt(obj):
            current_index += 1
            windows[current_index] = [ts, ts]
            continue
        if current_index and ts:
            windows[current_index][1] = ts
    return {idx: (start, end or start) for idx, (start, end) in windows.items() if start}


def _first_window_attr(window: Any, *names: str) -> str:
    for name in names:
        value = getattr(window, name, "")
        iso = iso8601(value)
        if iso:
            return iso
    return ""


def window_start_end(session: Any, window: Any, extracted: dict[int, tuple[str, str]]) -> tuple[str, str]:
    start_ts = _first_window_attr(window, "start_ts", "start_time", "startTime", "timestamp")
    end_ts = _first_window_attr(window, "end_ts", "end_time", "endTime")
    extracted_start, extracted_end = extracted.get(int(getattr(window, "prompt_index", 0) or 0), ("", ""))
    start_ts = start_ts or extracted_start or session_date_timestamp(session)
    end_ts = end_ts or extracted_end or start_ts
    return start_ts, end_ts


def usage_for_window(session: Any, window: Any) -> dict[str, int | str]:
    input_tokens = int(getattr(window, "input_delta", 0) or 0)
    cache_read_tokens = int(getattr(window, "cached_delta", 0) or 0)
    cache_creation_tokens = int(getattr(window, "cache_creation_delta", 0) or 0)
    output_tokens = int(getattr(window, "output_delta", 0) or 0) + int(getattr(window, "reasoning_delta", 0) or 0)
    if bool(getattr(session, "input_includes_cache", False)):
        input_tokens = max(0, input_tokens - cache_read_tokens - cache_creation_tokens)
    total = input_tokens + cache_read_tokens + cache_creation_tokens + output_tokens
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cache_read_input_tokens": cache_read_tokens,
        "cache_creation_input_tokens": cache_creation_tokens,
        "total": total,
        "unit": "TOKENS",
    }


def classification_metadata(session: Any, window: Any, host: str, row: dict[str, Any] | None = None) -> dict[str, Any]:
    row = row or {}
    metadata: dict[str, Any] = {
        "source": EXPORT_SOURCE,
        "diagnosis_version": row.get("diagnosis_version") or DIAGNOSIS_VERSION,
        "origin": row.get("origin") or getattr(session, "origin", ""),
        "host": row.get("host") or host,
        "bucket": row.get("bucket") or getattr(session, "bucket", ""),
        "file": getattr(session, "file", ""),
        "session_cwd": getattr(session, "session_cwd", ""),
        "session_date": getattr(session, "session_date", ""),
        "prompt_index": int(getattr(window, "prompt_index", 0) or 0),
        "usage_source": getattr(session, "usage_source", ""),
        "billable_model_source": getattr(session, "billable_model_source", ""),
    }
    for key in (
        *CLASSIFICATION_FIELDS,
        "request_pattern_path",
        "request_pattern_depth",
        "request_pattern_rule_id",
        "request_pattern_confidence",
        "request_pattern_config_version",
        "task_type_label",
        "task_type_confidence",
        "task_type_classifier",
        "task_type_reason",
        "task_type_source",
        "task_type_config_version",
        "task_label",
        "task_label_source",
        "task_label_confidence",
        "function_name",
        "shell_verb",
    ):
        value = row.get(key)
        if value not in (None, ""):
            metadata[key] = value

    if "task_type" not in metadata:
        task_type = row.get("work_motivation") or row.get("prompt_task_kind")
        if task_type not in (None, ""):
            metadata["task_type"] = task_type
    if "workflow_phase" not in metadata and metadata.get("bucket"):
        metadata["workflow_phase"] = metadata["bucket"]

    return {k: v for k, v in metadata.items() if v not in (None, "")}


def tags_from_metadata(metadata: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for key in CLASSIFICATION_FIELDS:
        value = metadata.get(key)
        if value in (None, ""):
            continue
        tag = f"{key}:{value}"
        if len(tag) <= 200:
            tags.append(tag)
    return tags


def _most_common(values: Iterable[str]) -> str:
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def build_trace_event(session: Any, host: str, row: dict[str, Any] | None = None) -> dict[str, Any]:
    trace_id = trace_id_for_session(session)
    timestamp = session_date_timestamp(session)
    metadata = classification_metadata(session, getattr(session, "prompt_windows", [""])[0] if getattr(session, "prompt_windows", []) else "", host, row)
    return {
        "id": _event_id("trace-create", trace_id),
        "type": "trace-create",
        "timestamp": timestamp,
        "body": {
            "id": trace_id,
            "timestamp": timestamp,
            "name": f"{getattr(session, 'origin', 'native')} {getattr(session, 'model', 'session')} session",
            "sessionId": trace_id,
            "metadata": metadata,
            "tags": tags_from_metadata(metadata),
        },
    }


def build_generation_event(
    session: Any,
    window: Any,
    host: str = "",
    row: dict[str, Any] | None = None,
    timestamps: dict[int, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    start_ts, end_ts = window_start_end(session, window, timestamps or {})
    trace_id = trace_id_for_session(session)
    obs_id = generation_id(
        str(getattr(session, "origin", "") or ""),
        str(getattr(session, "file", "") or ""),
        int(getattr(window, "prompt_index", 0) or 0),
        start_ts,
        end_ts,
    )
    usage = usage_for_window(session, window)
    usage_details = {key: value for key, value in usage.items() if key != "unit"}
    metadata = classification_metadata(session, window, host, row)
    body = {
        "id": obs_id,
        "traceId": trace_id,
        "name": f"prompt-{int(getattr(window, 'prompt_index', 0) or 0)}",
        "startTime": start_ts,
        "endTime": end_ts,
        "model": str(getattr(session, "billable_model", "") or ""),
        "input": getattr(window, "prompt_text", ""),
        "output": getattr(window, "final_answer", ""),
        "usage": usage,
        "usageDetails": usage_details,
        "metadata": metadata,
        "tags": tags_from_metadata(metadata),
    }
    return {
        "id": _event_id("generation-create", obs_id),
        "type": "generation-create",
        "timestamp": start_ts,
        "body": body,
    }


def build_generation_payload(
    session: Any,
    window: Any,
    host: str = "",
    row: dict[str, Any] | None = None,
    timestamps: dict[int, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    return build_generation_event(session, window, host, row, timestamps)["body"]


def _classification_index(
    sessions: list[Any],
    request_pattern_config: str = "",
    task_categorization_config: str = "",
) -> dict[tuple[str, int], dict[str, Any]]:
    command_rows: list[dict[str, Any]] = []
    native = [session for session in sessions if getattr(session, "origin", "") != "omp"]
    omp = [session for session in sessions if getattr(session, "origin", "") == "omp"]
    for family in ("codex", "claude"):
        family_sessions = [session for session in native if getattr(session, "model", "") == family]
        if family_sessions:
            command_rows.extend(pve.build_rows_for_model(family_sessions, {"costUSD": 0.0}, {})[3])
    if omp:
        command_rows.extend(pve.build_omp_rows(omp, {})[3])
    if not command_rows:
        return {}
    classified_rows, _review_rows = pve.build_command_attribution_v4_5_rows(command_rows)
    request_pattern_categorizer = RequestPatternCategorizer(
        load_request_pattern_config(request_pattern_config or str(DEFAULT_REQUEST_PATTERN_CONFIG))
    )
    task_categorizer = TaskCategorizer(load_task_categorization_config(task_categorization_config or str(DEFAULT_TASK_CATEGORIZATION_CONFIG)))
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in classified_rows:
        try:
            prompt_index = int(row.get("prompt_index") or 0)
        except (TypeError, ValueError):
            continue
        grouped.setdefault((str(row.get("file") or ""), prompt_index), []).append(row)

    by_prompt: dict[tuple[str, int], dict[str, Any]] = {}
    for key, rows in grouped.items():
        ordered = sorted(rows, key=lambda row: int(float(row.get("command_index") or 0)))
        first = dict(ordered[0])
        request_pattern = request_pattern_categorizer.classify(first)
        task_classification = task_categorizer.classify(first)
        task_label, task_label_source, task_label_confidence = derive_task_label(first, request_pattern.request_pattern)
        classified_window = classify_prompt_window(ordered)
        fixing_causes = [
            fixing_cause_for(item.workflow_phase, item.efficiency_label) or ""
            for item in classified_window
        ]
        first.update(
            {
                "diagnosis_version": DIAGNOSIS_VERSION,
                "request_pattern": request_pattern.request_pattern,
                "request_pattern_path": request_pattern.request_pattern_path,
                "request_pattern_depth": request_pattern.request_pattern_depth,
                "request_pattern_rule_id": request_pattern.request_pattern_rule_id,
                "request_pattern_confidence": request_pattern.request_pattern_confidence,
                "request_pattern_config_version": request_pattern.request_pattern_config_version,
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
                "workflow_phase": _most_common(item.workflow_phase for item in classified_window),
                "efficiency_label": _most_common(item.efficiency_label for item in classified_window),
                "fixing_cause": _most_common(fixing_causes),
                "agent_tool_intention": _most_common(str(row.get("agent_tool_intention") or "") for row in ordered),
                "function_name": _most_common(str(row.get("function_name") or "") for row in ordered),
                "shell_verb": _most_common(str(row.get("shell_verb") or "") for row in ordered),
            }
        )
        by_prompt[key] = first
    return by_prompt


def build_ingestion_events(
    parsed_sessions: Iterable[ParsedSession],
    request_pattern_config: str = "",
    task_categorization_config: str = "",
) -> list[dict[str, Any]]:
    parsed = list(parsed_sessions)
    sessions = [item.session for item in parsed]
    classified = _classification_index(sessions, request_pattern_config, task_categorization_config)
    events: list[dict[str, Any]] = []
    for item in parsed:
        session = item.session
        host = item.host
        first_row = None
        for window in getattr(session, "prompt_windows", []) or []:
            first_row = classified.get((str(getattr(session, "file", "") or ""), int(getattr(window, "prompt_index", 0) or 0)))
            if first_row:
                break
        events.append(build_trace_event(session, host, first_row))
        kind = _infer_session_kind(session)
        timestamps = extract_prompt_window_timestamps(Path(str(getattr(session, "file", "") or "")), kind)
        for window in getattr(session, "prompt_windows", []) or []:
            row = classified.get((str(getattr(session, "file", "") or ""), int(getattr(window, "prompt_index", 0) or 0)))
            events.append(build_generation_event(session, window, host, row, timestamps))
    return events


def build_ingestion_payload(
    parsed_sessions: Iterable[ParsedSession],
    request_pattern_config: str = "",
    task_categorization_config: str = "",
) -> dict[str, Any]:
    events = build_ingestion_events(parsed_sessions, request_pattern_config, task_categorization_config)
    return {
        "batch": events,
        "metadata": {
            "source": EXPORT_SOURCE,
            "batch_size": len(events),
        },
    }


def _attr_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_attr_value(item) for item in value]}}
    return {"stringValue": str(value)}


def _attr(key: str, value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    return {"key": key, "value": _attr_value(value)}


def _json_attr(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _attributes(values: dict[str, Any]) -> list[dict[str, Any]]:
    attrs: list[dict[str, Any]] = []
    for key, value in values.items():
        item = _attr(key, value)
        if item is not None:
            attrs.append(item)
    return attrs


def _metadata_attrs(prefix: str, metadata: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for key, value in metadata.items():
        if value in (None, ""):
            continue
        normalized = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(key))
        attrs[f"{prefix}.{normalized}"] = str(value)
    return attrs


def otlp_root_span(session: Any, trace_event: dict[str, Any], generation_events: list[dict[str, Any]]) -> dict[str, Any]:
    body = trace_event["body"]
    trace_id = otlp_trace_id_for_session(session)
    span_id = otlp_root_span_id(session)
    start_ts = body.get("timestamp") or session_date_timestamp(session)
    end_ts = generation_events[-1]["body"].get("endTime") if generation_events else start_ts
    metadata = body.get("metadata") or {}
    first_generation = generation_events[0]["body"] if generation_events else {}
    last_generation = generation_events[-1]["body"] if generation_events else {}
    attrs = {
        "langfuse.trace.name": body.get("name", ""),
        "session.id": body.get("sessionId", ""),
        "langfuse.trace.tags": body.get("tags", []),
        "langfuse.observation.type": "span",
        "langfuse.observation.input": first_generation.get("input", ""),
        "langfuse.observation.output": last_generation.get("output", ""),
        "langfuse.observation.metadata.source": EXPORT_SOURCE,
        **_metadata_attrs("langfuse.trace.metadata", metadata),
        **_metadata_attrs("langfuse.observation.metadata", metadata),
    }
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": body.get("name") or "session",
        "kind": "SPAN_KIND_INTERNAL",
        "startTimeUnixNano": unix_nanos(start_ts),
        "endTimeUnixNano": unix_nanos(end_ts or start_ts),
        "attributes": _attributes(attrs),
    }


def otlp_generation_span(session: Any, event: dict[str, Any]) -> dict[str, Any]:
    body = event["body"]
    trace_id = otlp_trace_id_for_session(session)
    start_ts = body.get("startTime") or event.get("timestamp") or session_date_timestamp(session)
    end_ts = body.get("endTime") or start_ts
    usage = body.get("usageDetails") or {}
    metadata = body.get("metadata") or {}
    attrs = {
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": body.get("model", ""),
        "langfuse.observation.input": body.get("input", ""),
        "langfuse.observation.output": body.get("output", ""),
        "langfuse.observation.usage_details": _json_attr(usage),
        "gen_ai.request.model": body.get("model", ""),
        "gen_ai.response.model": body.get("model", ""),
        "gen_ai.usage.input_tokens": int(usage.get("input") or 0),
        "gen_ai.usage.output_tokens": int(usage.get("output") or 0),
        "gen_ai.usage.cache_read.input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "gen_ai.usage.cache_creation.input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "gen_ai.usage.total_tokens": int(usage.get("total") or 0),
        **_metadata_attrs("langfuse.observation.metadata", metadata),
    }
    return {
        "traceId": trace_id,
        "spanId": otlp_generation_span_id(
            str(metadata.get("origin") or getattr(session, "origin", "") or ""),
            str(metadata.get("file") or getattr(session, "file", "") or ""),
            int(metadata.get("prompt_index") or 0),
            start_ts,
            end_ts,
        ),
        "parentSpanId": otlp_root_span_id(session),
        "name": body.get("name") or "generation",
        "kind": "SPAN_KIND_INTERNAL",
        "startTimeUnixNano": unix_nanos(start_ts),
        "endTimeUnixNano": unix_nanos(end_ts),
        "attributes": _attributes(attrs),
    }


def build_otlp_payload(
    parsed_sessions: Iterable[ParsedSession],
    request_pattern_config: str = "",
    task_categorization_config: str = "",
) -> dict[str, Any]:
    parsed = list(parsed_sessions)
    events_by_trace = build_ingestion_events(parsed, request_pattern_config, task_categorization_config)
    session_by_trace = {trace_id_for_session(item.session): item.session for item in parsed}
    traces: dict[str, dict[str, Any]] = {}
    generations: dict[str, list[dict[str, Any]]] = {}
    for event in events_by_trace:
        body = event.get("body") or {}
        trace_id = body.get("id") if event.get("type") == "trace-create" else body.get("traceId")
        if not trace_id:
            continue
        if event.get("type") == "trace-create":
            traces[str(trace_id)] = event
        elif event.get("type") == "generation-create":
            generations.setdefault(str(trace_id), []).append(event)

    spans: list[dict[str, Any]] = []
    for trace_id, trace_event in traces.items():
        session = session_by_trace.get(trace_id)
        if session is None:
            continue
        generation_events = generations.get(trace_id, [])
        spans.append(otlp_root_span(session, trace_event, generation_events))
        spans.extend(otlp_generation_span(session, event) for event in generation_events)

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": _attributes({
                        "service.name": "session-metrics-cron",
                        "service.version": "langfuse-export-otlp-v1",
                    })
                },
                "scopeSpans": [
                    {
                        "scope": {"name": EXPORT_SOURCE},
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def otlp_span_count(payload: dict[str, Any]) -> int:
    total = 0
    for resource_span in payload.get("resourceSpans") or []:
        for scope_span in resource_span.get("scopeSpans") or []:
            total += len(scope_span.get("spans") or [])
    return total


def split_otlp_payload(payload: dict[str, Any], max_spans: int) -> list[dict[str, Any]]:
    if max_spans <= 0:
        return [payload]
    chunks: list[dict[str, Any]] = []
    for resource_span in payload.get("resourceSpans") or []:
        resource = resource_span.get("resource") or {}
        for scope_span in resource_span.get("scopeSpans") or []:
            scope = scope_span.get("scope") or {}
            spans = list(scope_span.get("spans") or [])
            for start in range(0, len(spans), max_spans):
                chunks.append(
                    {
                        "resourceSpans": [
                            {
                                "resource": resource,
                                "scopeSpans": [
                                    {
                                        "scope": scope,
                                        "spans": spans[start : start + max_spans],
                                    }
                                ],
                            }
                        ]
                    }
                )
    return chunks or [payload]


def _parse_staged_sessions(stage: Path, local_only: bool = False) -> list[ParsedSession]:
    parsed: list[ParsedSession] = []
    seen_paths: set[Path] = set()

    def add_dir(kind: str, directory: Path, host: str) -> None:
        parser = PARSERS[kind]
        if not directory.exists():
            return
        for path in sorted(directory.rglob("*.jsonl")):
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            session = parser(path)
            if session is not None:
                parsed.append(ParsedSession(session=session, host=host))

    for kind in PARSERS:
        add_dir(kind, stage / kind, "local")
    for host_dir in sorted(path for path in stage.iterdir() if path.is_dir()) if stage.exists() else []:
        host = host_dir.name
        if local_only and host != "local":
            continue
        for kind in PARSERS:
            add_dir(kind, host_dir / kind, host)
    return parsed


def collect_sessions(stage: Path, local_only: bool, no_collect: bool) -> list[ParsedSession]:
    if no_collect:
        return _parse_staged_sessions(stage, local_only)

    try:
        import fleet_cost_report as fleet  # noqa: WPS433
    except Exception:
        return _parse_staged_sessions(stage, local_only)

    hosts = fleet.load_hosts(include_local=True)
    if local_only:
        hosts = [host for host in hosts if host.get("local")]

    parsed: list[ParsedSession] = []
    seen_content: set[str] = set()
    for host in hosts:
        name = str(host.get("name") or "unknown")
        dirs = fleet.host_dirs(host, stage)
        for kind, parser in PARSERS.items():
            directory = dirs.get(kind)
            if not directory or not Path(directory).exists():
                continue
            for path in sorted(Path(directory).rglob("*.jsonl")):
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
                if digest in seen_content:
                    continue
                seen_content.add(digest)
                session = parser(path)
                if session is not None:
                    parsed.append(ParsedSession(session=session, host=name))
    return parsed


def filter_since(parsed_sessions: Iterable[ParsedSession], since_date: str) -> list[ParsedSession]:
    if not since_date:
        return list(parsed_sessions)
    return [
        item
        for item in parsed_sessions
        if str(getattr(item.session, "session_date", "") or "") >= since_date
    ]


def langfuse_endpoint(host: str) -> str:
    return f"{host.rstrip('/')}/api/public/ingestion"


def langfuse_base_url(args_host: str) -> str:
    return (args_host or os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL") or "").rstrip("/")


def langfuse_otlp_traces_endpoint(host: str) -> str:
    base = host.rstrip("/")
    if base.endswith("/api/public/otel/v1/traces"):
        return base
    if base.endswith("/api/public/otel"):
        return f"{base}/v1/traces"
    return f"{base}/api/public/otel/v1/traces"


def post_ingestion_payload(payload: dict[str, Any], host: str, public_key: str, secret_key: str) -> dict[str, Any]:
    credentials = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        langfuse_endpoint(host),
        data=data,
        method="POST",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        body = response.read().decode("utf-8")
        try:
            parsed_body: Any = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed_body = body
        return {"status": response.status, "body": parsed_body}


def post_otlp_payload(payload: dict[str, Any], host: str, public_key: str, secret_key: str) -> dict[str, Any]:
    credentials = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        langfuse_otlp_traces_endpoint(host),
        data=data,
        method="POST",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
            "x-langfuse-ingestion-version": OTLP_INGESTION_VERSION,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        body = response.read().decode("utf-8")
        try:
            parsed_body: Any = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed_body = body
        return {"status": response.status, "body": parsed_body}


def dry_run_summary(payload: dict[str, Any], sample_limit: int = DRY_RUN_SAMPLE_LIMIT) -> dict[str, Any]:
    batch = payload.get("batch") or []
    trace_count = sum(1 for event in batch if isinstance(event, dict) and event.get("type") == "trace-create")
    generation_count = sum(1 for event in batch if isinstance(event, dict) and event.get("type") == "generation-create")
    return {
        "trace_events": trace_count,
        "generation_events": generation_count,
        "total_events": len(batch),
        "sample": batch[:sample_limit],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-dir", default=str(DEFAULT_STAGE_DIR))
    parser.add_argument("--local-only", action="store_true", help="skip SSH hosts when collecting")
    parser.add_argument("--collect", action="store_true", help="collect fresh sessions even during --dry-run")
    parser.add_argument("--no-collect", action="store_true", help="reuse an existing stage")
    parser.add_argument("--since-date", default="", type=parse_since_date, help="only export sessions on/after YYYY-MM-DD")
    parser.add_argument("--langfuse-host", default=langfuse_base_url(""))
    parser.add_argument("--request-pattern-config", default=str(DEFAULT_REQUEST_PATTERN_CONFIG))
    parser.add_argument("--task-categorization-config", default=str(DEFAULT_TASK_CATEGORIZATION_CONFIG))
    parser.add_argument("--transport", choices=("otlp", "legacy-ingestion"), default="otlp")
    parser.add_argument("--otlp-chunk-size", type=int, default=DEFAULT_OTLP_CHUNK_SIZE)
    parser.add_argument("--dry-run", action="store_true", help="print counts and sample payloads without posting")
    args = parser.parse_args(argv)

    parsed_sessions = filter_since(
        collect_sessions(Path(args.stage_dir), args.local_only, args.no_collect or (args.dry_run and not args.collect)),
        args.since_date,
    )
    payload = (
        build_otlp_payload(parsed_sessions, args.request_pattern_config, args.task_categorization_config)
        if args.transport == "otlp"
        else build_ingestion_payload(parsed_sessions, args.request_pattern_config, args.task_categorization_config)
    )

    if args.dry_run:
        if args.transport == "otlp":
            print(json.dumps({
                "parsed_sessions": len(parsed_sessions),
                "otlp_spans": otlp_span_count(payload),
                "sample": ((payload.get("resourceSpans") or [{}])[0].get("scopeSpans") or [{}])[0].get("spans", [])[:DRY_RUN_SAMPLE_LIMIT],
            }, indent=2, sort_keys=True))
        else:
            print(json.dumps(dry_run_summary(payload), indent=2, sort_keys=True))
        return 0

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    host = langfuse_base_url(args.langfuse_host)
    if not host:
        print("LANGFUSE_HOST or LANGFUSE_BASE_URL is required unless --dry-run is used", file=sys.stderr)
        return 2
    if not public_key or not secret_key:
        print("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required unless --dry-run is used", file=sys.stderr)
        return 2

    if args.transport == "otlp":
        chunks = split_otlp_payload(payload, args.otlp_chunk_size)
        posted_spans = 0
        results: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks, start=1):
            result = post_otlp_payload(chunk, host, public_key, secret_key)
            posted_spans += otlp_span_count(chunk)
            results.append({"chunk": index, "spans": otlp_span_count(chunk), **result})
        print(json.dumps({"posted_spans": posted_spans, "chunks": len(chunks), "results": results}, indent=2, sort_keys=True))
    else:
        result = post_ingestion_payload(payload, host, public_key, secret_key)
        print(json.dumps({"posted_events": len(payload["batch"]), **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
