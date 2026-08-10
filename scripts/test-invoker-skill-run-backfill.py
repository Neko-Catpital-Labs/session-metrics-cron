#!/usr/bin/env python3
"""Tests for invoker_skill_run_backfill.py against synthetic multi-provider corpora."""

from __future__ import annotations

import csv
import tempfile
import types
import unittest
from pathlib import Path

import gen_invoker_plan_to_invoker_fixture as fixtures
import invoker_skill_run_backfill as backfill


def _args(**overrides: object) -> types.SimpleNamespace:
    defaults = dict(start_pattern=None, end_path_glob="*plans/*.yaml")
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _write(scenario: str, path: Path) -> None:
    rows = fixtures.build_fixture_lines(scenario)
    path.write_text(fixtures.render_jsonl(rows))


class BackfillCorpusTests(unittest.TestCase):
    def test_finds_real_runs_and_skips_noise_across_providers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_dir = root / "claude-projects" / "proj-a"
            claude_dir.mkdir(parents=True)
            codex_dir = root / "codex-sessions"
            codex_dir.mkdir(parents=True)
            out_dir = root / "out"

            _write("happy_path", claude_dir / "real-run.jsonl")
            _write("unterminated", claude_dir / "noise-unterminated.jsonl")
            (claude_dir / "irrelevant.jsonl").write_text(
                '{"type":"user","timestamp":"2026-01-01T00:00:00Z","uuid":"x",'
                '"message":{"role":"user","content":[{"type":"text","text":"just chatting"}]}}\n'
            )
            _write("codex_happy_path", codex_dir / "real-run.jsonl")

            files = backfill.discover_session_files(root / "claude-projects", [codex_dir])
            self.assertEqual(len(files), 4)  # 3 claude files + 1 codex file

            rows = []
            for _hint, path in files:
                import invoker_skill_run_timing_report as timing

                session = timing.parse_session(path)
                match = backfill.find_real_run(session, _args())
                if match is None:
                    continue
                start, end = match
                result = timing.build_result(path, session, start, end, timing.DEFAULT_SKILL_SCRIPTS)
                rows.append(result)

            self.assertEqual(len(rows), 2)
            by_provider = {r["session"]["provider"] for r in rows}
            self.assertEqual(by_provider, {"claude", "codex"})

    def test_end_to_end_writes_index_and_detail_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_dir = root / "claude-projects" / "proj-a"
            claude_dir.mkdir(parents=True)
            out_dir = root / "out"
            _write("happy_path", claude_dir / "real-run.jsonl")

            argv = [
                "invoker_skill_run_backfill.py",
                "--claude-dir",
                str(root / "claude-projects"),
                "--codex-dir",
                str(root / "no-such-codex-dir"),
                "--out-dir",
                str(out_dir),
            ]
            import sys

            old_argv = sys.argv
            sys.argv = argv
            try:
                exit_code = backfill.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(exit_code, 0)
            index_path = out_dir / "backfill-index.csv"
            self.assertTrue(index_path.exists())
            with index_path.open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["provider"], "claude")
            self.assertEqual(rows[0]["total_span_seconds"], "104.0")
            self.assertTrue(Path(rows[0]["detail_json"]).exists())


if __name__ == "__main__":
    unittest.main()
