#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "warehouse_cost_demo.py"
spec = importlib.util.spec_from_file_location("warehouse_cost_demo", SCRIPT_PATH)
assert spec and spec.loader
demo = importlib.util.module_from_spec(spec)
sys.modules["warehouse_cost_demo"] = demo
sys.path.insert(0, str(REPO_ROOT / "scripts"))
spec.loader.exec_module(demo)


def fixture_row(**overrides: str) -> dict[str, str]:
    row = {
        "schema_version": demo.SCHEMA_VERSION,
        "model": "codex",
        "provider": "openai",
        "billable_model": "gpt-5.3-codex",
        "file": "/tmp/rollout-2026-05-01T00-00-00-a.jsonl",
        "session_date": "2026-05-01",
        "prompt_index": "1",
        "command_index": "1",
        "prompt_preview": "Implement the plan.",
        "function_name": "exec_command",
        "shell_verb": "rg",
        "command_preview": "rg foo src",
        "prompt_input_tokens": "100",
        "prompt_cache_read_tokens": "20",
        "prompt_cache_creation_tokens": "5",
        "prompt_output_tokens": "10",
        "prompt_reasoning_tokens": "2",
        "prompt_total_tokens": "110",
        "prompt_derived_total_cost_usd": "0.01",
        "allocated_input_tokens": "100",
        "allocated_cache_read_tokens": "20",
        "allocated_cache_creation_tokens": "5",
        "allocated_output_tokens": "10",
        "allocated_reasoning_tokens": "2",
        "allocated_total_tokens": "110",
        "allocated_total_cost_usd": "0.01",
        "request_origin": "human_direct_request",
        "classification_revision": demo.CLASSIFICATION_REVISION,
        "work_motivation": "implementation",
        "agent_tool_intention": "implementation_planning_inspection",
        "tool_execution_mode": "command_mechanics",
    }
    row.update(overrides)
    return row


class WarehouseCostDemoTests(unittest.TestCase):
    def test_phase_enrichment_and_normalized_schema(self) -> None:
        rows = [
            fixture_row(command_index="1", agent_tool_intention="implementation_planning_inspection", shell_verb="rg"),
            fixture_row(command_index="2", function_name="edit", agent_tool_intention="implementation_edit", shell_verb=""),
            fixture_row(command_index="3", agent_tool_intention="test_execution", shell_verb="pytest", command_preview="pytest tests"),
        ]
        enriched = demo.enrich_phase_fields(rows)
        normalized = [demo.normalize_row(row, {}) for row in enriched]

        self.assertTrue(all(row["workflow_phase"] for row in normalized))
        self.assertTrue(all(row["efficiency_label"] for row in normalized))
        self.assertEqual(normalized[0]["session_id"], "rollout-2026-05-01T00-00-00-a")
        self.assertEqual(set(normalized[0]), set(demo.OUTPUT_COLUMNS))
        self.assertNotIn("prompt_preview", normalized[0])
        self.assertNotIn("command_preview", normalized[0])
        self.assertNotIn("workdir", normalized[0])
        self.assertNotIn("terminal_context_parent_command_preview", normalized[0])
        self.assertEqual(normalized[0]["allocated_fresh_input_tokens"], 75.0)

    def test_export_writes_one_command_row_without_component_fanout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            input_path = temp / "input.csv"
            output_path = temp / "output.csv"
            summary_path = temp / "summary.json"
            rows = [
                fixture_row(command_index="1"),
                fixture_row(command_index="2", function_name="edit", agent_tool_intention="implementation_edit", shell_verb=""),
            ]
            with input_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)

            summary = demo.export_csv(input_path, output_path, summary_path, pricing_table_path="", limit=0)
            self.assertEqual(summary.rows, 2)
            with output_path.open(newline="", encoding="utf-8") as handle:
                output_rows = list(csv.DictReader(handle))
            self.assertEqual(len(output_rows), 2)
            self.assertNotIn("usage_command_cost_component", output_rows[0])
            self.assertEqual(output_rows[0]["session_id"], "rollout-2026-05-01T00-00-00-a")

    def test_full_source_row_count_when_report_exists(self) -> None:
        source = REPO_ROOT / "reports" / "usage-command-attribution-v4_5.csv"
        if not source.exists():
            self.skipTest("full v4.5 report is not present")
        rows = demo.source_rows(source)
        self.assertEqual(len(rows), demo.EXPECTED_FULL_ROW_COUNT)


if __name__ == "__main__":
    unittest.main()
