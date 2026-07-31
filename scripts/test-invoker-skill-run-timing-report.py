#!/usr/bin/env python3
"""Fixture tests for invoker_skill_run_timing_report.py."""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

import gen_invoker_plan_to_invoker_fixture as fixtures
import invoker_skill_run_timing_report as timing


def _args(**overrides: object) -> types.SimpleNamespace:
    defaults = dict(
        start_line=None,
        start_uuid=None,
        start_pattern=None,
        start_occurrence="first",
        end_line=None,
        end_uuid=None,
        end_path_glob="*plans/*.yaml",
        end_occurrence="last",
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _write_fixture(scenario: str, tmp_dir: str) -> Path:
    rows = fixtures.build_fixture_lines(scenario)
    path = Path(tmp_dir) / f"{scenario}.jsonl"
    path.write_text(fixtures.render_jsonl(rows))
    return path


class HappyPathTests(unittest.TestCase):
    def test_full_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fixture("happy_path", tmp)
            session = timing.parse_session(path)
            start = timing.resolve_start(session, _args())
            end = timing.resolve_end(session, _args())

            self.assertTrue(end.extra["end_marker_found"])
            self.assertEqual(end.extra["file_path"], "plans/invoker-handoff.yaml")

            segments, anomalies = timing.build_timeline(session, start, end, timing.DEFAULT_SKILL_SCRIPTS)
            total_span = timing.resolve_total_span(start, end, segments)
            totals = timing.compute_totals(segments, total_span)

            self.assertEqual(anomalies, [])
            self.assertEqual(total_span, 104.0)
            self.assertEqual(totals["model_thinking_seconds"], 28.0)
            self.assertEqual(totals["tool_execution_seconds"], 76.0)
            self.assertEqual(totals["unknown_timing_seconds"], 0.0)

            breakdown = timing.build_tool_breakdown(session, start, end, timing.DEFAULT_SKILL_SCRIPTS)
            by_label = {row["label"]: row for row in breakdown}
            self.assertEqual(by_label["bash:skill-doctor.sh"]["busy_seconds_sum"], 30.0)
            self.assertEqual(by_label["bash:skill-doctor.sh"]["calls"], 1)
            self.assertEqual(by_label["bash:extract-assumptions.sh"]["busy_seconds_sum"], 12.0)
            self.assertEqual(by_label["tool:Task"]["busy_seconds_sum"], 30.0)
            self.assertTrue(by_label["tool:Task"]["nested_agent"])
            self.assertEqual(by_label["tool:Read"]["busy_seconds_sum"], 3.0)
            self.assertEqual(by_label["tool:Read"]["calls"], 2)
            self.assertEqual(by_label["tool:Write"]["busy_seconds_sum"], 2.0)

            parallel_segments = [s for s in segments if s.type == "tool_batch" and s.extra.get("parallel")]
            self.assertEqual(len(parallel_segments), 1)
            self.assertEqual(parallel_segments[0].seconds, 2.0)


class UnterminatedTests(unittest.TestCase):
    def test_no_end_marker_and_unterminated_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fixture("unterminated", tmp)
            session = timing.parse_session(path)
            start = timing.resolve_start(session, _args())
            end = timing.resolve_end(session, _args())

            self.assertFalse(end.extra["end_marker_found"])

            segments, anomalies = timing.build_timeline(session, start, end, timing.DEFAULT_SKILL_SCRIPTS)
            kinds = {a["kind"] for a in anomalies}
            self.assertIn("unterminated_tool_call", kinds)

            total_span = timing.resolve_total_span(start, end, segments)
            totals = timing.compute_totals(segments, total_span)
            self.assertEqual(total_span, 17.0)
            self.assertEqual(totals["model_thinking_seconds"], 17.0)
            self.assertEqual(totals["tool_execution_seconds"], 0.0)


class MissingTimestampTests(unittest.TestCase):
    def test_unknown_span_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fixture("missing_timestamp", tmp)
            session = timing.parse_session(path)
            start = timing.resolve_start(session, _args())
            end = timing.resolve_end(session, _args())

            segments, anomalies = timing.build_timeline(session, start, end, timing.DEFAULT_SKILL_SCRIPTS)
            total_span = timing.resolve_total_span(start, end, segments)
            totals = timing.compute_totals(segments, total_span)

            self.assertEqual(total_span, 74.0)
            self.assertEqual(totals["model_thinking_seconds"], 15.0)
            self.assertEqual(totals["tool_execution_seconds"], 14.0)
            self.assertEqual(totals["unknown_timing_seconds"], 45.0)

            missing_kinds = [a for a in anomalies if a["kind"] == "missing_timestamp"]
            self.assertEqual(len(missing_kinds), 2)

            thinking_segments = [s.seconds for s in segments if s.type == "thinking"]
            self.assertEqual(thinking_segments, [5.0, 5.0, 5.0])

            unknown_segments = [s.seconds for s in segments if s.type == "unknown"]
            self.assertEqual(unknown_segments, [45.0])


class TwoStepWriteTests(unittest.TestCase):
    def test_last_yaml_write_wins_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fixture("two_step_write", tmp)
            session = timing.parse_session(path)

            candidates = timing.find_end_candidates(session, "*plans/*.yaml")
            self.assertEqual(len(candidates), 2)

            end_last = timing.resolve_end(session, _args(end_occurrence="last"))
            self.assertEqual(end_last.extra["tool_use_id"], "tu_write_yaml_final")

            end_first = timing.resolve_end(session, _args(end_occurrence="first"))
            self.assertEqual(end_first.extra["tool_use_id"], "tu_write_yaml_draft")


class MultipleInvocationsTests(unittest.TestCase):
    def test_list_candidates_and_default_pick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fixture("multiple_invocations", tmp)
            session = timing.parse_session(path)

            candidates = timing.find_start_candidates(session, timing.START_RE)
            self.assertEqual(len(candidates), 2)
            self.assertIn("first attempt", candidates[0].text)
            self.assertIn("second attempt", candidates[1].text)

            start = timing.resolve_start(session, _args())
            self.assertIn("first attempt", start.extra["text_preview"])


class CodexHappyPathTests(unittest.TestCase):
    def test_full_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fixture("codex_happy_path", tmp)
            session = timing.parse_session(path)
            self.assertEqual(session.provider, "codex")

            start = timing.resolve_start(session, _args())
            end = timing.resolve_end(session, _args())

            self.assertTrue(end.extra["end_marker_found"])
            self.assertEqual(end.extra["file_path"], "plans/invoker-handoff.yaml")

            segments, anomalies = timing.build_timeline(session, start, end, timing.DEFAULT_SKILL_SCRIPTS)
            total_span = timing.resolve_total_span(start, end, segments)
            totals = timing.compute_totals(segments, total_span)

            self.assertEqual(anomalies, [])
            self.assertEqual(total_span, 74.0)
            self.assertEqual(totals["model_thinking_seconds"], 30.0)
            self.assertEqual(totals["tool_execution_seconds"], 44.0)
            self.assertEqual(totals["unknown_timing_seconds"], 0.0)

            breakdown = timing.build_tool_breakdown(session, start, end, timing.DEFAULT_SKILL_SCRIPTS)
            by_label = {row["label"]: row for row in breakdown}
            self.assertEqual(by_label["bash:skill-doctor.sh"]["busy_seconds_sum"], 30.0)
            self.assertEqual(by_label["bash:extract-assumptions.sh"]["busy_seconds_sum"], 12.0)
            self.assertEqual(by_label["tool:apply_patch"]["busy_seconds_sum"], 2.0)


class CodexParallelCallsTests(unittest.TestCase):
    def test_parallel_calls_batch_without_negative_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fixture("codex_parallel_calls", tmp)
            session = timing.parse_session(path)
            start = timing.resolve_start(session, _args())
            end = timing.resolve_end(session, _args())

            segments, anomalies = timing.build_timeline(session, start, end, timing.DEFAULT_SKILL_SCRIPTS)
            total_span = timing.resolve_total_span(start, end, segments)
            totals = timing.compute_totals(segments, total_span)

            self.assertEqual(anomalies, [])
            self.assertEqual(total_span, 24.0)
            self.assertEqual(totals["model_thinking_seconds"], 15.0)
            self.assertEqual(totals["tool_execution_seconds"], 9.0)

            parallel_segments = [s for s in segments if s.type == "tool_batch" and s.extra.get("parallel")]
            self.assertEqual(len(parallel_segments), 1)
            self.assertEqual(parallel_segments[0].seconds, 7.0)
            self.assertEqual(set(parallel_segments[0].extra["tool_use_ids"]), {"call_a", "call_b", "call_c"})

            breakdown = timing.build_tool_breakdown(session, start, end, timing.DEFAULT_SKILL_SCRIPTS)
            by_label = {row["label"]: row for row in breakdown}
            self.assertEqual(by_label["bash:validate-plan.sh"]["busy_seconds_sum"], 2.0)
            self.assertEqual(by_label["bash:check-policy-coverage.sh"]["busy_seconds_sum"], 1.0)
            self.assertEqual(by_label["bash:check-stack-manifest.sh"]["busy_seconds_sum"], 7.0)


class CodexMultiFilePatchTests(unittest.TestCase):
    def test_matches_only_the_yaml_path_in_a_multi_file_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fixture("codex_multi_file_patch", tmp)
            session = timing.parse_session(path)

            candidates = timing.find_end_candidates(session, "*plans/*.yaml")
            self.assertEqual(len(candidates), 1)
            self.assertEqual(
                timing._candidate_file_paths(candidates[0]), ["docs/README.md", "plans/invoker-handoff.yaml"]
            )

            end = timing.resolve_end(session, _args())
            self.assertEqual(end.extra["file_path"], "plans/invoker-handoff.yaml")


class ScriptDiscoveryTests(unittest.TestCase):
    def test_falls_back_to_default_when_dir_missing(self) -> None:
        names = timing.discover_skill_scripts([Path("/nonexistent/path/for/invoker-skill-scripts")])
        self.assertEqual(names, timing.DEFAULT_SKILL_SCRIPTS)

    def test_reads_real_directory_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "custom-check.sh").write_text("#!/bin/sh\n")
            (d / "notes.txt").write_text("ignore me\n")
            names = timing.discover_skill_scripts([d])
            self.assertEqual(names, {"custom-check.sh"})

    def test_unions_across_multiple_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            b = Path(tmp) / "b"
            a.mkdir()
            b.mkdir()
            (a / "check-a.sh").write_text("#!/bin/sh\n")
            (b / "check-b.mjs").write_text("//\n")
            names = timing.discover_skill_scripts([a, b])
            self.assertEqual(names, {"check-a.sh", "check-b.mjs"})


if __name__ == "__main__":
    unittest.main()
