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

import planning_vs_execution_report as pve  # noqa: E402


DEFAULT_STAGE_DIR = Path("/tmp/fleet-sessions")
DRY_RUN_SAMPLE_LIMIT = 5
EXPORT_SOURCE = "codex_session_langfuse_export"
CLASSIFICATION_FIELDS = ("workflow_phase", "task_type", "request_pattern", "origin", "host", "bucket")
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


def trace_id_for_session(session: Any) -> str:
    session_key = str(getattr(session, "file", "") or getattr(session, "session_cwd", ""))
    return row_hash(["trace", session_key, str(getattr(session, "session_date", "") or "")])


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
    for key in CLASSIFICATION_FIELDS:
        value = row.get(key)
        if value not in (None, ""):
            metadata[key] = value

    if "task_type" not in metadata:
        task_type = row.get("work_motivation") or row.get("prompt_task_kind")
        if task_type not in (None, ""):
            metadata["task_type"] = task_type
    if "request_pattern" not in metadata:
        request_pattern = row.get("request_origin") or row.get("primary_why")
        if request_pattern not in (None, ""):
            metadata["request_pattern"] = request_pattern
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


def _classification_index(sessions: list[Any]) -> dict[tuple[str, int], dict[str, Any]]:
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
    by_prompt: dict[tuple[str, int], dict[str, Any]] = {}
    for row in classified_rows:
        try:
            prompt_index = int(row.get("prompt_index") or 0)
        except (TypeError, ValueError):
            continue
        by_prompt.setdefault((str(row.get("file") or ""), prompt_index), row)
    return by_prompt


def build_ingestion_events(parsed_sessions: Iterable[ParsedSession]) -> list[dict[str, Any]]:
    parsed = list(parsed_sessions)
    sessions = [item.session for item in parsed]
    classified = _classification_index(sessions)
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


def build_ingestion_payload(parsed_sessions: Iterable[ParsedSession]) -> dict[str, Any]:
    events = build_ingestion_events(parsed_sessions)
    return {
        "batch": events,
        "metadata": {
            "source": EXPORT_SOURCE,
            "batch_size": len(events),
        },
    }


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
    parser.add_argument("--no-collect", action="store_true", help="reuse an existing stage")
    parser.add_argument("--since-date", default="", type=parse_since_date, help="only export sessions on/after YYYY-MM-DD")
    parser.add_argument("--langfuse-host", default=os.environ.get("LANGFUSE_HOST", ""))
    parser.add_argument("--dry-run", action="store_true", help="print counts and sample payloads without posting")
    args = parser.parse_args(argv)

    parsed_sessions = filter_since(
        collect_sessions(Path(args.stage_dir), args.local_only, args.no_collect or args.dry_run),
        args.since_date,
    )
    payload = build_ingestion_payload(parsed_sessions)

    if args.dry_run:
        print(json.dumps(dry_run_summary(payload), indent=2, sort_keys=True))
        return 0

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not args.langfuse_host:
        print("LANGFUSE_HOST is required unless --dry-run is used", file=sys.stderr)
        return 2
    if not public_key or not secret_key:
        print("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required unless --dry-run is used", file=sys.stderr)
        return 2

    result = post_ingestion_payload(payload, args.langfuse_host, public_key, secret_key)
    print(json.dumps({"posted_events": len(payload["batch"]), **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
