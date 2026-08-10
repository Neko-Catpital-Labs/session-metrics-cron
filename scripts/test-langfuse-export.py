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


def _decode_attr_value(value: dict[str, Any]) -> Any:
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "arrayValue" in value:
        return [_decode_attr_value(item) for item in value["arrayValue"].get("values", [])]
    return value


def _attr_map(span: dict[str, Any]) -> dict[str, Any]:
    return {item["key"]: _decode_attr_value(item["value"]) for item in span.get("attributes", [])}


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


def test_otlp_payload_fields() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_omp_fixture(Path(tmp))
        session = pve.parse_omp_session(path)
        assert session is not None
        parsed = [exporter.ParsedSession(session=session, host="local")]
        payload = exporter.build_otlp_payload(parsed)

    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert exporter.otlp_span_count(payload) == 2, spans
    root_span, generation_span = spans
    assert len(root_span["traceId"]) == 32, root_span
    assert len(root_span["spanId"]) == 16, root_span
    assert generation_span["traceId"] == root_span["traceId"], generation_span
    assert generation_span["parentSpanId"] == root_span["spanId"], generation_span
    assert len(generation_span["spanId"]) == 16, generation_span

    attrs = _attr_map(generation_span)
    assert attrs["langfuse.observation.type"] == "generation", attrs
    assert attrs["langfuse.observation.model.name"] == "openai-codex/gpt-5.4", attrs
    assert json.loads(attrs["langfuse.observation.usage_details"]) == {
        "input": 100,
        "output": 35,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 10,
        "total": 165,
    }, attrs
    assert attrs["gen_ai.usage.input_tokens"] == 100, attrs
    assert attrs["gen_ai.usage.output_tokens"] == 35, attrs
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 20, attrs
    assert attrs["gen_ai.usage.cache_creation.input_tokens"] == 10, attrs
    assert attrs["gen_ai.usage.total_tokens"] == 165, attrs
    _assert_no_cost_key(payload)


def test_otlp_payload_includes_spend_bucket_metadata() -> None:
    def fake_classification_index(
        _sessions: list[Any],
        _request_pattern_config: str = "",
        _task_categorization_config: str = "",
    ) -> dict[tuple[str, int], dict[str, Any]]:
        assert session is not None
        return {
            (str(session.file), 1): {
                "origin": "omp",
                "host": "local",
                "bucket": "execution",
                "diagnosis_version": "request_pattern_layers_v1",
                "request_origin": "human_direct_request",
                "work_motivation": "failure_diagnosis",
                "agent_tool_intention": "test_execution",
                "workflow_phase": "repair_loop",
                "efficiency_label": "thrash",
                "fixing_cause": "Repeated repair/test loops",
                "request_pattern": "ci_fix",
                "request_pattern_path": "fixing/ci_fix",
                "task_type": "bug_fix",
                "task_type_label": "Bug fix",
                "task_label": "ci_fix",
            }
        }

    with tempfile.TemporaryDirectory() as tmp:
        path = _write_omp_fixture(Path(tmp))
        session = pve.parse_omp_session(path)
        assert session is not None
        parsed = [exporter.ParsedSession(session=session, host="local")]
        original = exporter._classification_index
        exporter._classification_index = fake_classification_index
        try:
            payload = exporter.build_otlp_payload(parsed)
        finally:
            exporter._classification_index = original

    generation_span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][1]
    attrs = _attr_map(generation_span)
    assert attrs["langfuse.observation.metadata.fixing_cause"] == "Repeated repair/test loops", attrs
    assert attrs["langfuse.observation.metadata.work_motivation"] == "failure_diagnosis", attrs
    assert attrs["langfuse.observation.metadata.request_pattern"] == "ci_fix", attrs
    assert attrs["langfuse.observation.metadata.request_pattern_path"] == "fixing/ci_fix", attrs
    assert attrs["langfuse.observation.metadata.agent_tool_intention"] == "test_execution", attrs
    assert attrs["langfuse.observation.metadata.workflow_phase"] == "repair_loop", attrs
    assert attrs["langfuse.observation.metadata.efficiency_label"] == "thrash", attrs


def test_generation_id_idempotency() -> None:
    parts = ("omp", "/tmp/session.jsonl", 1, "2026-06-20T10:00:01Z", "2026-06-20T10:00:05Z")
    first = exporter.generation_id(*parts)
    second = exporter.generation_id(*parts)
    different_prompt = exporter.generation_id(parts[0], parts[1], 2, parts[3], parts[4])
    assert first == second, (first, second)
    assert first != different_prompt, (first, different_prompt)


def test_split_otlp_payload_chunks_spans() -> None:
    payload = {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "x"}}]},
                "scopeSpans": [{"scope": {"name": "test"}, "spans": [{"spanId": str(index)} for index in range(5)]}],
            }
        ]
    }
    chunks = exporter.split_otlp_payload(payload, 2)
    assert [exporter.otlp_span_count(chunk) for chunk in chunks] == [2, 2, 1], chunks
    assert chunks[0]["resourceSpans"][0]["resource"] == payload["resourceSpans"][0]["resource"], chunks
    assert chunks[0]["resourceSpans"][0]["scopeSpans"][0]["scope"] == {"name": "test"}, chunks


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
        assert summary["parsed_sessions"] == 1, summary
        assert summary["otlp_spans"] == 2, summary
        assert called is False
    finally:
        exporter.urllib.request.urlopen = original


def test_dry_run_collect_override() -> None:
    no_collect_values: list[bool] = []

    def fake_collect_sessions(_stage: Path, _local_only: bool, no_collect: bool) -> list[exporter.ParsedSession]:
        no_collect_values.append(no_collect)
        return []

    original = exporter.collect_sessions
    exporter.collect_sessions = fake_collect_sessions
    try:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rc = exporter.main(["--stage-dir", "/tmp/not-used", "--dry-run", "--collect"])
        summary = json.loads(stdout.getvalue())
        assert rc == 0, rc
        assert summary["parsed_sessions"] == 0, summary
        assert no_collect_values == [False], no_collect_values
    finally:
        exporter.collect_sessions = original


def test_otlp_endpoint_mapping() -> None:
    assert exporter.langfuse_otlp_traces_endpoint("https://us.cloud.langfuse.com") == (
        "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    )
    assert exporter.langfuse_otlp_traces_endpoint("https://us.cloud.langfuse.com/api/public/otel") == (
        "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    )
    assert exporter.langfuse_otlp_traces_endpoint("https://us.cloud.langfuse.com/api/public/otel/v1/traces") == (
        "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    )


if __name__ == "__main__":
    test_generation_payload_fields()
    test_otlp_payload_fields()
    test_otlp_payload_includes_spend_bucket_metadata()
    test_generation_id_idempotency()
    test_split_otlp_payload_chunks_spans()
    test_dry_run_makes_no_network_call()
    test_dry_run_collect_override()
    test_otlp_endpoint_mapping()
    print("OK: langfuse export")
