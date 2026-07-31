#!/usr/bin/env python3
"""Export parsed Codex/Claude/omp session logs to Langfuse ingestion payloads."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import planning_vs_execution_report as pve  # noqa: E402


INVOKER_CONFIG = Path.home() / ".invoker" / "config.json"
DEFAULT_KEY = Path.home() / ".ssh" / "id_ed25519"
DEFAULT_STAGE_DIR = "/tmp/fleet-sessions"
DRY_RUN_PAYLOAD_LIMIT = 5
CLASSIFICATION_FIELDS = ("workflow_phase", "task_type", "request_pattern", "origin", "host", "bucket")


@dataclass(frozen=True)
class SessionSource:
    kind: str
    host: str
    path: Path


def row_hash(parts: list[Any]) -> str:
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return __import__("hashlib").sha256(raw.encode()).hexdigest()[:24]


def trace_id_for_session(session: pve.SessionStats) -> str:
    source = session.file or session.session_cwd
    return row_hash(["trace", session.origin, source, session.session_date])


def generation_id(origin: str, file: str, prompt_index: int, start_ts: str, end_ts: str) -> str:
    return row_hash([origin, file, prompt_index, start_ts, end_ts])


def event_id(event_type: str, body_id: str) -> str:
    return row_hash(["event", event_type, body_id])


def int_tokens(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_iso8601(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp_from_obj(obj: dict[str, Any]) -> str:
    for value in (
        obj.get("timestamp"),
        (obj.get("payload") or {}).get("timestamp") if isinstance(obj.get("payload"), dict) else None,
        (obj.get("message") or {}).get("timestamp") if isinstance(obj.get("message"), dict) else None,
    ):
        normalized = normalize_iso8601(value)
        if normalized:
            return normalized
    return ""


def session_date_timestamp(session_date: str) -> str:
    if session_date:
        return f"{session_date}T00:00:00Z"
    return "1970-01-01T00:00:00Z"


def _claude_user_prompt_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str) and item.strip():
            parts.append(item.strip())
        elif isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            text = item["text"].strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def is_prompt_start(kind: str, obj: dict[str, Any]) -> bool:
    if kind == "codex":
        payload = obj.get("payload") or {}
        return obj.get("type") == "event_msg" and payload.get("type") == "user_message" and isinstance(payload.get("message"), str)
    if kind == "claude":
        message = obj.get("message") or {}
        return obj.get("type") == "user" and message.get("role") == "user" and bool(_claude_user_prompt_text(message))
    if kind == "omp":
        message = obj.get("message") or {}
        return obj.get("type") == "message" and message.get("role") == "user"
    return False


def extract_prompt_window_timestamps(path: Path, kind: str) -> dict[int, tuple[str, str]]:
    starts: dict[int, str] = {}
    ends: dict[int, str] = {}
    current_index = 0
    last_ts = ""
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return {}

    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = timestamp_from_obj(obj)
        if is_prompt_start(kind, obj):
            if current_index:
                ends[current_index] = last_ts or ts or starts.get(current_index, "")
            current_index += 1
            starts[current_index] = ts or last_ts
        if ts:
            last_ts = ts

    if current_index:
        ends[current_index] = last_ts or starts.get(current_index, "")
    return {idx: (starts.get(idx, ""), ends.get(idx, starts.get(idx, ""))) for idx in starts}


def parser_kind_for_session(session: pve.SessionStats) -> str:
    if session.origin == "omp":
        return "omp"
    if session.model in {"codex", "claude"}:
        return session.model
    return ""


def prompt_window_times(session: pve.SessionStats, window: pve.PromptWindow, extracted: dict[int, tuple[str, str]]) -> tuple[str, str]:
    start_ts = normalize_iso8601(getattr(window, "start_ts", "")) or extracted.get(window.prompt_index, ("", ""))[0]
    end_ts = normalize_iso8601(getattr(window, "end_ts", "")) or extracted.get(window.prompt_index, ("", ""))[1]
    fallback = session_date_timestamp(session.session_date)
    start_ts = start_ts or end_ts or fallback
    end_ts = end_ts or start_ts
    return start_ts, end_ts


def _metadata_field(field: str, session: pve.SessionStats, window: pve.PromptWindow, host: str) -> str:
    if field == "origin":
        return str(session.origin or "")
    if field == "bucket":
        return str(session.bucket or "")
    if field == "host":
        return str(host or "")
    for source in (getattr(window, "row", None), getattr(session, "row", None), getattr(window, "metadata", None), getattr(session, "metadata", None)):
        if isinstance(source, dict) and source.get(field) not in (None, ""):
            return str(source[field])
    for source in (window, session):
        value = getattr(source, field, "")
        if value not in (None, ""):
            return str(value)
    return ""


def classification_metadata(session: pve.SessionStats, window: pve.PromptWindow, host: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for field in CLASSIFICATION_FIELDS:
        value = _metadata_field(field, session, window, host)
        if value:
            metadata[field] = value
    return metadata


def classification_tags(metadata: dict[str, str]) -> list[str]:
    return [f"{field}:{metadata[field]}" for field in CLASSIFICATION_FIELDS if metadata.get(field)]


def usage_for_window(session: pve.SessionStats, window: pve.PromptWindow) -> dict[str, int | str]:
    input_tokens = int_tokens(window.input_delta)
    cache_read_tokens = int_tokens(window.cached_delta)
    cache_creation_tokens = int_tokens(window.cache_creation_delta)
    output_tokens = int_tokens(window.output_delta)
    reasoning_tokens = int_tokens(window.reasoning_delta)
    output_with_reasoning = output_tokens + reasoning_tokens
    total_tokens = pve.raw_total_tokens(
        session.model,
        input_tokens=input_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        provider_total_tokens=int_tokens(window.total_delta),
    )
    return {
        "input": input_tokens,
        "output": output_with_reasoning,
        "total": total_tokens,
        "unit": "TOKENS",
        "cacheReadInputTokens": cache_read_tokens,
        "cacheCreationInputTokens": cache_creation_tokens,
    }


def usage_details_for_window(window: pve.PromptWindow) -> dict[str, int]:
    output_tokens = int_tokens(window.output_delta)
    reasoning_tokens = int_tokens(window.reasoning_delta)
    return {
        "input_tokens": int_tokens(window.input_delta),
        "output_tokens": output_tokens + reasoning_tokens,
        "raw_output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_input_tokens": int_tokens(window.cached_delta),
        "cache_creation_input_tokens": int_tokens(window.cache_creation_delta),
    }


def build_trace_event(session: pve.SessionStats, trace_id: str, timestamp: str, host: str, tags: list[str]) -> dict[str, Any]:
    metadata = {
        "source": "session_log_export",
        "model_family": session.model,
        "origin": session.origin,
        "provider": session.provider,
        "billable_model_source": session.billable_model_source,
        "usage_source": session.usage_source,
        "file": session.file,
        "session_cwd": session.session_cwd,
        "session_date": session.session_date,
        "host": host,
        "bucket": session.bucket,
    }
    return {
        "id": event_id("trace-create", trace_id),
        "timestamp": timestamp,
        "type": "trace-create",
        "body": {
            "id": trace_id,
            "timestamp": timestamp,
            "name": f"{session.origin}+{session.model} session",
            "sessionId": session.session_cwd or session.file or trace_id,
            "metadata": {k: v for k, v in metadata.items() if v not in (None, "")},
            "tags": tags,
        },
    }


def build_generation_event(
    session: pve.SessionStats,
    window: pve.PromptWindow,
    trace_id: str,
    start_ts: str,
    end_ts: str,
    host: str,
) -> dict[str, Any]:
    metadata = classification_metadata(session, window, host)
    tags = classification_tags(metadata)
    metadata.update(
        {
            "source": "session_log_export",
            "prompt_index": str(window.prompt_index),
            "model_family": session.model,
            "provider": session.provider,
            "usage_source": session.usage_source,
            "billable_model_source": session.billable_model_source,
            "file": session.file,
            "session_cwd": session.session_cwd,
            "session_date": session.session_date,
            "tags": tags,
        }
    )
    observation_id = generation_id(session.origin, session.file, window.prompt_index, start_ts, end_ts)
    return {
        "id": event_id("generation-create", observation_id),
        "timestamp": start_ts,
        "type": "generation-create",
        "body": {
            "id": observation_id,
            "traceId": trace_id,
            "name": f"prompt-{window.prompt_index}",
            "startTime": start_ts,
            "endTime": end_ts,
            "model": session.billable_model or session.model,
            "usage": usage_for_window(session, window),
            "usageDetails": usage_details_for_window(window),
            "metadata": {k: v for k, v in metadata.items() if v not in (None, "")},
        },
    }


def build_session_events(session: pve.SessionStats, host: str = "") -> list[dict[str, Any]]:
    kind = parser_kind_for_session(session)
    extracted = extract_prompt_window_timestamps(Path(session.file), kind) if session.file and kind else {}
    trace_id = trace_id_for_session(session)
    generation_events: list[dict[str, Any]] = []
    trace_tags: list[str] = []

    for window in session.prompt_windows:
        start_ts, end_ts = prompt_window_times(session, window, extracted)
        generation = build_generation_event(session, window, trace_id, start_ts, end_ts, host)
        generation_events.append(generation)
        for tag in generation["body"]["metadata"].get("tags", []):
            if tag not in trace_tags:
                trace_tags.append(tag)

    trace_ts = generation_events[0]["body"]["startTime"] if generation_events else session_date_timestamp(session.session_date)
    return [build_trace_event(session, trace_id, trace_ts, host, trace_tags)] + generation_events


def parse_session_file(source: SessionSource) -> pve.SessionStats | None:
    parsers: dict[str, Callable[[Path], pve.SessionStats | None]] = {
        "codex": pve.parse_codex_session,
        "claude": pve.parse_claude_session,
        "omp": pve.parse_omp_session,
    }
    parser = parsers[source.kind]
    return parser(source.path)


def load_hosts(include_local: bool) -> list[dict[str, Any]]:
    hosts: list[dict[str, Any]] = []
    if include_local:
        hosts.append({"name": "local", "local": True})
    try:
        cfg = json.loads(INVOKER_CONFIG.read_text())
    except OSError:
        cfg = {}
    for name, target in (cfg.get("remoteTargets") or {}).items():
        host = target.get("host") or target.get("hostname")
        if not host:
            continue
        hosts.append(
            {
                "name": name,
                "local": False,
                "host": host,
                "user": target.get("user") or target.get("username") or "invoker",
                "key": target.get("sshKeyPath") or target.get("identityFile") or str(DEFAULT_KEY),
            }
        )
    return hosts


def rsync_dir(host: dict[str, Any], remote_dir: str, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    ssh = f"ssh -i {host['key']} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes"
    src = f"{host['user']}@{host['host']}:{remote_dir}/"
    cmd = ["rsync", "-az", "--include", "*/", "--include", "*.jsonl", "--exclude", "*", "-e", ssh, src, str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode not in (0, 23, 24):
        print(f"  rsync {host['name']} {remote_dir} rc={proc.returncode}: {proc.stderr.strip()[:160]}", file=sys.stderr)
    return len(list(dest.rglob("*.jsonl")))


def host_dirs(host: dict[str, Any], stage: Path) -> dict[str, Path | None]:
    if host.get("local"):
        return {
            "codex": Path.home() / ".codex" / "sessions",
            "claude": Path.home() / ".claude" / "projects",
            "omp": Path.home() / ".omp" / "agent" / "sessions",
        }
    base = stage / host["name"]
    out: dict[str, Path | None] = {}
    for kind, remote in (("codex", "~/.codex/sessions"), ("claude", "~/.claude/projects"), ("omp", "~/.omp/agent/sessions")):
        remote_dir = remote.replace("~", f"/home/{host['user']}")
        dest = base / kind
        out[kind] = dest if rsync_dir(host, remote_dir, dest) else None
    return out


def staged_sources(stage: Path) -> list[SessionSource]:
    sources: list[SessionSource] = []
    for kind in ("codex", "claude", "omp"):
        direct = stage / kind
        if direct.exists():
            sources.extend(SessionSource(kind, "stage", fp) for fp in sorted(direct.rglob("*.jsonl")))
    if stage.exists():
        for child in sorted(p for p in stage.iterdir() if p.is_dir()):
            for kind in ("codex", "claude", "omp"):
                root = child / kind
                if root.exists():
                    sources.extend(SessionSource(kind, child.name, fp) for fp in sorted(root.rglob("*.jsonl")))
    return sources


def collected_sources(stage: Path, local_only: bool, no_collect: bool) -> list[SessionSource]:
    if no_collect:
        return staged_sources(stage)

    sources: list[SessionSource] = []
    for host in load_hosts(include_local=True):
        if local_only and not host.get("local"):
            continue
        dirs = host_dirs(host, stage)
        for kind, root in dirs.items():
            if root and Path(root).exists():
                sources.extend(SessionSource(kind, host["name"], fp) for fp in sorted(Path(root).rglob("*.jsonl")))
    return sources


def session_passes_since_date(session: pve.SessionStats, since_date: str) -> bool:
    if not since_date:
        return True
    return bool(session.session_date) and session.session_date >= since_date


def build_export_events(stage: Path, local_only: bool, no_collect: bool, since_date: str = "") -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for source in collected_sources(stage, local_only, no_collect):
        session = parse_session_file(source)
        if not session or not session_passes_since_date(session, since_date):
            continue
        events.extend(build_session_events(session, host=source.host))
    return events


def langfuse_ingestion_url(host: str) -> str:
    normalized = host.rstrip("/")
    if not normalized:
        raise RuntimeError("Missing Langfuse host. Set LANGFUSE_HOST or pass --langfuse-host.")
    return f"{normalized}/api/public/ingestion"


def basic_auth_header(public_key: str, secret_key: str) -> str:
    if not public_key or not secret_key:
        raise RuntimeError("Missing Langfuse credentials. Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.")
    raw = f"{public_key}:{secret_key}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def post_ingestion(host: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {"batch": events}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Authorization": basic_auth_header(os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", "")),
        "Content-Type": "application/json",
    }
    request = urllib.request.Request(langfuse_ingestion_url(host), data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        response_body = response.read().decode("utf-8", errors="replace")
        if response.status >= 400:
            raise RuntimeError(f"Langfuse ingestion failed status={response.status} body={response_body}")
        try:
            return json.loads(response_body) if response_body.strip() else {}
        except json.JSONDecodeError:
            return {"raw": response_body}


def dry_run_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        "event_count": len(events),
        "trace_count": sum(1 for event in events if event.get("type") == "trace-create"),
        "generation_count": sum(1 for event in events if event.get("type") == "generation-create"),
    }
    return {**counts, "first_payloads": events[:DRY_RUN_PAYLOAD_LIMIT]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-dir", default=DEFAULT_STAGE_DIR)
    parser.add_argument("--local-only", action="store_true", help="skip SSH hosts")
    parser.add_argument("--no-collect", action="store_true", help="reuse an existing stage directory")
    parser.add_argument("--since-date", default="", help="only export sessions on or after YYYY-MM-DD")
    parser.add_argument("--langfuse-host", default=os.environ.get("LANGFUSE_HOST", ""))
    parser.add_argument("--dry-run", action="store_true", help="print counts and sample payloads without HTTP")
    args = parser.parse_args(argv)

    events = build_export_events(
        Path(args.stage_dir),
        local_only=bool(args.local_only),
        no_collect=bool(args.no_collect),
        since_date=str(args.since_date or ""),
    )
    if args.dry_run:
        print(json.dumps(dry_run_summary(events), indent=2, sort_keys=True))
        return 0

    response = post_ingestion(str(args.langfuse_host or ""), events)
    print(json.dumps({"sent": len(events), "response": response}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
