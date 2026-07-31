#!/usr/bin/env python3
"""Standalone fixture tests for scripts/langfuse_export.py."""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import langfuse_export as exporter  # noqa: E402
import planning_vs_execution_report as pve  # noqa: E402


def _omp_lines() -> list[dict[str, Any]]:
    return [
        {
            "type": "session",
            "id": "s",
            "timestamp": "2026-06-20T10:00:00Z",
            "cwd": "/work/repo",
        },
        {
            "type": "message",
            "id": "u1",
            "timestamp": "2026-06-20T10:00:01Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "please summarize token usage"}],
            },
        },
        {
            "type": "message",
            "id": "a1",
            "timestamp": "2026-06-20T10:00:05Z",
            "message": {
                "role": "assistant",
                "model": "openai-codex/gpt-5.4",
                "content": [{"type": "text", "text": "done"}],
                "usage": {
                    "input": 100,
                    "output": 30,
                    "cacheRead": 20,
                    "cacheWrite": 10,
                    "reasoningTokens": 5,
                    "totalTokens": 165,
                    "cost": {"input": 1, "output": 1, "cacheRead": 1, "cacheWrite": 1, "total": 4},
                },
            },
        },
    ]


def _write_omp_fixture(root: Path) -> Path:
    session_dir = root / "omp"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "session.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in _omp_lines()) + "\n", encoding="utf-8")
    return path


def _assert_no_cost_key(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert "cost" not in str(key).lower(), value
            _assert_no_cost_key(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_cost_key(child)


def test_generation_payload_fields() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_omp_fixture(Path(tmp))
        session = pve.parse_omp_session(path)
        assert session is not None
        parsed = [exporter.ParsedSession(session=session, host="local")]
        payload = exporter.build_ingestion_payload(parsed)

    generations = [event for event in payload["batch"] if event["type"] == "generation-create"]
    assert len(generations) == 1, generations
    body = generations[0]["body"]
    assert body["model"] == "openai-codex/gpt-5.4", body
    assert body["startTime"] == "2026-06-20T10:00:01Z", body
    assert body["endTime"] == "2026-06-20T10:00:05Z", body
    assert body["usage"] == {
        "input": 100,
        "output": 35,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 10,
        "total": 165,
        "unit": "TOKENS",
    }, body["usage"]
    _assert_no_cost_key(body["usage"])


def test_generation_id_idempotency() -> None:
    parts = ("omp", "/tmp/session.jsonl", 1, "2026-06-20T10:00:01Z", "2026-06-20T10:00:05Z")
    first = exporter.generation_id(*parts)
    second = exporter.generation_id(*parts)
    different_prompt = exporter.generation_id(parts[0], parts[1], 2, parts[3], parts[4])
    assert first == second, (first, second)
    assert first != different_prompt, (first, different_prompt)


def test_dry_run_makes_no_network_call() -> None:
    called = False

    def fail_urlopen(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("urlopen should not be called during --dry-run")

    original = exporter.urllib.request.urlopen
    exporter.urllib.request.urlopen = fail_urlopen
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _write_omp_fixture(Path(tmp))
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                rc = exporter.main(["--stage-dir", tmp, "--no-collect", "--dry-run"])
            summary = json.loads(stdout.getvalue())
        assert rc == 0, rc
        assert summary["generation_events"] == 1, summary
        assert summary["trace_events"] == 1, summary
        assert called is False
    finally:
        exporter.urllib.request.urlopen = original


if __name__ == "__main__":
    test_generation_payload_fields()
    test_generation_id_idempotency()
    test_dry_run_makes_no_network_call()
    print("OK: langfuse export")
