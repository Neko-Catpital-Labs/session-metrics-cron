#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "invoker_cost_breakdown_report.py"
spec = importlib.util.spec_from_file_location("invoker_cost_breakdown_report", SCRIPT_PATH)
assert spec and spec.loader
report = importlib.util.module_from_spec(spec)
sys.modules["invoker_cost_breakdown_report"] = report
spec.loader.exec_module(report)


class _Task:
    def __init__(self, label: str) -> None:
        self.task_type_label = label


class _Tasks:
    def classify(self, row: dict[str, str]) -> _Task:
        return _Task(row.get("_task_label", "Other"))


class _Pattern:
    request_pattern_path = "other"


class _Patterns:
    def classify(self, row: dict[str, str]) -> _Pattern:
        return _Pattern()


class CostBreakdownScopeTests(unittest.TestCase):
    def test_spend_causes_identify_repeated_ci_log_reads(self) -> None:
        fields = ["model", "bucket", "file", "prompt_index", "command_preview", "target", "primary_why", "output_token_estimate", "allocated_total_cost_usd"]
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            with (reports / "planning-vs-execution-command-attribution.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                for i in (1, 2, 3):
                    writer.writerow({"model": "codex", "bucket": "execution", "file": f"/tmp/ci-{i}.jsonl", "prompt_index": "1", "command_preview": "gh run view 123 --log", "target": "", "primary_why": "ci_log_diagnosis", "output_token_estimate": "500000", "allocated_total_cost_usd": "10"})
            causes = report.spend_cause_rollup(reports, {("codex", "execution", f"ci-{i}", 1) for i in (1, 2, 3)})
        self.assertEqual(causes[0]["cause"], "unbounded_ci_log_read")
        self.assertTrue(causes[0]["recurring"])

    def test_all_rollups_use_selected_task_bucket(self) -> None:
        fields = [
            "model", "origin", "host", "file", "session_date", "bucket", "billable_model",
            "session_cwd", "prompt_index", "prompt_preview", "first_prompt_preview",
            "total_tokens_delta", "cache_read_tokens_delta", "derived_total_cost_usd",
            "estimated_cost_usd", "_task_label",
        ]
        rows = [
            {"model": "codex", "origin": "native", "host": "local", "file": "/tmp/a.jsonl",
             "session_date": "2026-09-01", "bucket": "execution", "prompt_index": "1",
             "prompt_preview": "submit to invoker", "first_prompt_preview": "submit to invoker",
             "total_tokens_delta": "10", "cache_read_tokens_delta": "2", "derived_total_cost_usd": "2",
             "estimated_cost_usd": "2", "_task_label": "Invoker Plan Submission"},
            {"model": "claude", "origin": "omp", "host": "remote", "file": "/tmp/b.jsonl",
             "session_date": "2026-09-01", "bucket": "execution", "prompt_index": "1",
             "prompt_preview": "unrelated work", "first_prompt_preview": "unrelated work",
             "total_tokens_delta": "100", "cache_read_tokens_delta": "20", "derived_total_cost_usd": "100",
             "estimated_cost_usd": "100", "_task_label": "Other"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            with (reports / "planning-vs-execution-prompts.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            with (reports / "planning-vs-execution-tool-attribution.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["dimension", "name", "calls", "allocated_total_cost_usd", "allocated_total_tokens"])
                writer.writeheader()

            original_load = report.load_task_categorizer
            original_patterns = report.exporter.RequestPatternCategorizer
            report.load_task_categorizer = lambda _path: _Tasks()
            report.exporter.RequestPatternCategorizer = lambda _config: _Patterns()
            try:
                data = report.build_hierarchy(Namespace(
                    reports_dir=str(reports),
                    task_categorization_config="unused",
                    request_pattern_config="unused",
                    task_type_label="Invoker Plan Submission",
                ))
            finally:
                report.load_task_categorizer = original_load
                report.exporter.RequestPatternCategorizer = original_patterns

        self.assertEqual(data["total"]["cost_usd"], 2.0)
        self.assertEqual(data["origin_model"]["grand_total"]["cost_usd"], 2.0)
        self.assertEqual(data["over_time"]["rows"][0]["cost_usd"], 2.0)
        self.assertEqual(data["by_host"][0]["cost_usd"], 2.0)


if __name__ == "__main__":
    unittest.main()
