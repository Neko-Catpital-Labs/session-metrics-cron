#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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


def explorer_fixture_row() -> dict[str, str | float | int]:
    row = {column: "" for column in demo.explorer_commands_columns()}
    row.update(
        {
            "session_date": "2026-05-01",
            "session_id": "rollout-2026-05-01T00-00-00-a",
            "prompt_index": 1,
            "command_index": 1,
            "workflow_phase": "orientation",
            "efficiency_label": "expected_overhead",
            "agent_tool_intention": "implementation_planning_inspection",
            "request_origin": "human_direct_request",
            "work_motivation": "implementation",
            "function_name": "read",
            "shell_verb": "rg",
            "tool_execution_mode": "command_mechanics",
            "model": "codex",
            "provider": "openai",
            "billable_model": "gpt-5.3-codex",
            "allocated_input_tokens": 100,
            "allocated_cache_read_tokens": 20,
            "allocated_cache_creation_tokens": 5,
            "allocated_output_tokens": 10,
            "allocated_reasoning_tokens": 2,
            "allocated_total_tokens": 110,
            "allocated_fresh_input_tokens": 75,
            "allocated_total_cost_usd": 0.01,
            "allocated_fresh_input_cost_usd": 0.004,
            "allocated_cache_read_cost_usd": 0.003,
            "allocated_cache_creation_cost_usd": 0.001,
            "allocated_output_cost_usd": 0.002,
            "prompt_input_tokens": 100,
            "prompt_cache_read_tokens": 20,
            "prompt_cache_creation_tokens": 5,
            "prompt_output_tokens": 10,
            "prompt_reasoning_tokens": 2,
            "prompt_total_tokens": 110,
            "prompt_derived_total_cost_usd": 0.01,
            "origin": "native",
            "bucket": "execution",
            "usage_source": "codex_token_count",
            "billable_model_source": "session_log",
            "file": "/tmp/rollout-2026-05-01T00-00-00-a.jsonl",
            "session_cwd": "/tmp/repo",
            "prompt_preview": "Implement the plan.",
            "previous_prompt_preview": "Previous prompt",
            "first_prompt_preview": "Implement the plan.",
            "final_answer_preview": "Done.",
            "command_preview": "rg foo src",
            "workdir": "/tmp/repo",
            "target_type": "file",
            "target": "src/foo.ts",
            "stdin_preview": "",
            "delegated_agent_action": "",
            "delegated_agent_type": "",
            "delegated_task_preview": "",
            "request_pattern": "implement_plan",
            "request_pattern_path": "implement_plan",
            "request_pattern_depth": 1,
            "request_pattern_rule_id": "regex:implement_plan",
            "request_pattern_confidence": "high",
            "request_pattern_config_version": "patterns-test",
            "diagnosis_version": "request_pattern_layers_v1",
            "task_type": "implementation",
            "task_type_label": "Implementation",
            "task_type_confidence": "high",
            "task_type_classifier": "regex-primary",
            "task_type_reason": "regex_rule:implementation",
            "task_type_source": "prompt_preview",
            "task_type_config_version": "tasks-test",
            "task_label": "implement_the_plan",
            "task_label_source": "prompt_preview",
            "task_label_confidence": "medium",
            "fixing_cause": "Failure diagnosis thrash",
            "headline_context_tokens": 80,
            "headline_context_cost_usd": 0.005,
            "headline_cache_read_tokens": 20,
            "headline_cache_read_cost_usd": 0.003,
            "headline_output_tokens": 10,
            "headline_output_cost_usd": 0.002,
            "window_file": "rollout-2026-05-01T00-00-00-a-p1.json",
        }
    )
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

    def test_explorer_schema_and_validation_are_separate(self) -> None:
        columns = demo.explorer_commands_columns()
        self.assertGreater(len(columns), len(demo.OUTPUT_COLUMNS))
        self.assertIn("request_pattern", columns)
        self.assertIn("prompt_preview", columns)
        self.assertNotIn("command_hash", columns)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            schema_path = temp / "schema.json"
            explorer_path = temp / "explorer.csv"
            demo.write_bigquery_schema(
                schema_path,
                columns=columns,
                date_columns=demo.EXPLORER_DATE_COLUMNS,
                integer_columns=demo.EXPLORER_INTEGER_COLUMNS,
                numeric_columns=demo.EXPLORER_NUMERIC_COLUMNS,
            )
            schema = demo.json.loads(schema_path.read_text(encoding="utf-8"))
            by_name = {field["name"]: field["type"] for field in schema}
            self.assertEqual(by_name["request_pattern_depth"], "INTEGER")
            self.assertEqual(by_name["headline_context_cost_usd"], "FLOAT")
            with explorer_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerow(explorer_fixture_row())
            summary = demo.validate_explorer_local(explorer_path)
        self.assertEqual(summary.rows, 1)
        self.assertAlmostEqual(summary.total_cost, 0.01)
        self.assertAlmostEqual(summary.total_tokens, 110.0)

    def test_loaders_default_to_cost_explorer_commands_table(self) -> None:
        with patch.object(sys, "argv", [str(SCRIPT_PATH), "load-bigquery"]):
            args = demo.parse_args()
        self.assertEqual(args.explorer_table, demo.DEFAULT_EXPLORER_TABLE)
        with patch.object(sys, "argv", [str(SCRIPT_PATH), "load-clickhouse"]):
            args = demo.parse_args()
        self.assertEqual(args.explorer_table, demo.DEFAULT_EXPLORER_TABLE)

    def test_full_source_row_count_when_report_exists(self) -> None:
        source = REPO_ROOT / "reports" / "usage-command-attribution-v4_5.csv"
        if not source.exists():
            self.skipTest("full v4.5 report is not present")
        rows = demo.source_rows(source)
        self.assertEqual(len(rows), demo.EXPECTED_FULL_ROW_COUNT)


if __name__ == "__main__":
    unittest.main()
