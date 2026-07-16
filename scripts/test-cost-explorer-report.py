#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "cost_explorer_report.py"
spec = importlib.util.spec_from_file_location("cost_explorer_report", SCRIPT_PATH)
assert spec and spec.loader
report = importlib.util.module_from_spec(spec)
sys.modules["cost_explorer_report"] = report
sys.path.insert(0, str(REPO_ROOT / "scripts"))
spec.loader.exec_module(report)


BASE_FIELDS = {
    "schema_version": report.SCHEMA_VERSION,
    "classification_revision": report.CLASSIFICATION_REVISION,
    "model": "codex",
    "origin": "native",
    "provider": "openai",
    "billable_model": "gpt-5.3-codex",
    "billable_model_source": "session_log",
    "usage_source": "codex_token_count",
    "bucket": "execution",
    "request_origin": "human_direct_request",
    "work_motivation": "failure_diagnosis",
    "tool_execution_mode": "direct_tool",
    "session_cwd": "/tmp/repo",
    "previous_prompt_preview": "Previous request",
    "first_prompt_preview": "Fix CI failure in parser",
    "final_answer_preview": "Done.",
    "workdir": "/tmp/repo",
    "target_type": "file",
    "target": "src/parser.py",
    "stdin_preview": "",
    "delegated_agent_action": "",
    "delegated_agent_type": "",
    "delegated_task_preview": "",
}


def fixture_row(
    *,
    file: str,
    prompt_index: str,
    command_index: str,
    prompt_preview: str,
    agent_tool_intention: str,
    function_name: str,
    shell_verb: str,
    command_preview: str,
    allocated_input_tokens: str,
    allocated_cache_read_tokens: str,
    allocated_cache_creation_tokens: str,
    allocated_output_tokens: str,
    allocated_reasoning_tokens: str,
    allocated_total_tokens: str,
    allocated_total_cost_usd: str,
    prompt_input_tokens: str = "100",
    prompt_cache_read_tokens: str = "20",
    prompt_cache_creation_tokens: str = "10",
    prompt_output_tokens: str = "10",
    prompt_reasoning_tokens: str = "3",
    prompt_total_tokens: str = "110",
    prompt_derived_total_cost_usd: str = "1.2",
    session_date: str = "2026-07-15",
) -> dict[str, str]:
    row = {
        **BASE_FIELDS,
        "file": file,
        "session_date": session_date,
        "prompt_index": prompt_index,
        "command_index": command_index,
        "prompt_preview": prompt_preview,
        "agent_tool_intention": agent_tool_intention,
        "function_name": function_name,
        "shell_verb": shell_verb,
        "command_preview": command_preview,
        "allocated_input_tokens": allocated_input_tokens,
        "allocated_cache_read_tokens": allocated_cache_read_tokens,
        "allocated_cache_creation_tokens": allocated_cache_creation_tokens,
        "allocated_output_tokens": allocated_output_tokens,
        "allocated_reasoning_tokens": allocated_reasoning_tokens,
        "allocated_total_tokens": allocated_total_tokens,
        "allocated_total_cost_usd": allocated_total_cost_usd,
        "prompt_input_tokens": prompt_input_tokens,
        "prompt_cache_read_tokens": prompt_cache_read_tokens,
        "prompt_cache_creation_tokens": prompt_cache_creation_tokens,
        "prompt_output_tokens": prompt_output_tokens,
        "prompt_reasoning_tokens": prompt_reasoning_tokens,
        "prompt_total_tokens": prompt_total_tokens,
        "prompt_derived_total_cost_usd": prompt_derived_total_cost_usd,
    }
    return row


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    content = "\n".join(json.dumps(row) for row in rows) + "\n"
    path.write_text(content, encoding="utf-8")




class CostExplorerReportTests(unittest.TestCase):
    def test_builder_writes_required_fields_and_hybrid_buckets(self) -> None:
        rows = [
            fixture_row(
                file="/tmp/session-a.jsonl",
                prompt_index="1",
                command_index="1",
                prompt_preview="Fix CI failure in parser",
                agent_tool_intention="failure_diagnosis_inspection",
                function_name="read",
                shell_verb="tail",
                command_preview="tail -100 test.log",
                allocated_input_tokens="60",
                allocated_cache_read_tokens="10",
                allocated_cache_creation_tokens="10",
                allocated_output_tokens="5",
                allocated_reasoning_tokens="2",
                allocated_total_tokens="65",
                allocated_total_cost_usd="0.7",
            ),
            fixture_row(
                file="/tmp/session-a.jsonl",
                prompt_index="1",
                command_index="2",
                prompt_preview="Fix CI failure in parser",
                agent_tool_intention="test_execution",
                function_name="bash",
                shell_verb="pytest",
                command_preview="pytest tests/test_parser.py",
                allocated_input_tokens="40",
                allocated_cache_read_tokens="10",
                allocated_cache_creation_tokens="0",
                allocated_output_tokens="5",
                allocated_reasoning_tokens="1",
                allocated_total_tokens="45",
                allocated_total_cost_usd="0.5",
            ),
            fixture_row(
                file="/tmp/session-b.jsonl",
                prompt_index="2",
                command_index="1",
                prompt_preview="Implement parser feature",
                agent_tool_intention="feature_implementation_edit",
                function_name="edit",
                shell_verb="",
                command_preview="",
                allocated_input_tokens="30",
                allocated_cache_read_tokens="0",
                allocated_cache_creation_tokens="5",
                allocated_output_tokens="5",
                allocated_reasoning_tokens="1",
                allocated_total_tokens="35",
                allocated_total_cost_usd="0.4",
                prompt_input_tokens="40",
                prompt_cache_read_tokens="0",
                prompt_cache_creation_tokens="5",
                prompt_output_tokens="5",
                prompt_reasoning_tokens="1",
                prompt_total_tokens="45",
                prompt_derived_total_cost_usd="0.4",
            ),
        ]
        request_pattern_config = {
            "version": "patterns-test",
            "layers": [
                {
                    "id": "base",
                    "default": "other",
                    "rules": [
                        {"id": "ci_fix", "regex": ["fix ci failure"], "confidence": "high"},
                        {"id": "feature_build", "regex": ["implement parser feature"], "confidence": "high"},
                    ],
                }
            ],
        }
        task_config = {
            "version": "tasks-test",
            "categories": [
                {"id": "uncategorized", "label": "Uncategorized"},
                {"id": "ci_repair", "label": "CI Repair"},
                {"id": "feature_build", "label": "Feature Build"},
            ],
            "defaults": {"id": "uncategorized", "confidence": "low", "reason": "default"},
            "context": {"fields": [{"name": "prompt_preview", "weight": 1.0}]},
            "classifiers": [
                {
                    "id": "regex-primary",
                    "type": "regex",
                    "enabled": True,
                    "rules": [
                        {"id": "ci_repair", "regex": ["fix ci failure"], "confidence": "high"},
                        {"id": "feature_build", "regex": ["implement parser feature"], "confidence": "high"},
                    ],
                },
                {
                    "id": "codex-fallback",
                    "type": "codex",
                    "enabled": True,
                    "prompt": "ignored",
                    "command": [],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "usage.csv"
            output_dir = root / "out"
            write_csv(input_path, rows)
            write_json(root / "request-patterns.json", request_pattern_config)
            write_json(root / "task-categories.json", task_config)
            argv = sys.argv[:]
            try:
                sys.argv = [
                    str(SCRIPT_PATH),
                    "--input",
                    str(input_path),
                    "--output-dir",
                    str(output_dir),
                    "--request-pattern-config",
                    str(root / "request-patterns.json"),
                    "--task-categorization-config",
                    str(root / "task-categories.json"),
                ]
                self.assertEqual(report.main(), 0)
            finally:
                sys.argv = argv

            summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["headline_totals"]["command_count"], 3)
            self.assertEqual(summary["headline_totals"]["prompt_window_count"], 2)

            with (output_dir / "windows.csv").open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                window_rows = list(reader)
                self.assertEqual(reader.fieldnames, report.WINDOWS_COLUMNS)
            self.assertEqual(window_rows[0]["window_file"], "session-a-p1.json")
            self.assertEqual(window_rows[1]["window_file"], "session-b-p2.json")
            self.assertEqual(float(window_rows[0]["headline_context_tokens"]), 80.0)
            self.assertEqual(float(window_rows[0]["headline_cache_read_tokens"]), 20.0)
            self.assertEqual(float(window_rows[0]["headline_output_tokens"]), 10.0)

            with (output_dir / "commands.csv").open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                command_rows = list(reader)
                self.assertEqual(reader.fieldnames, report.COMMANDS_COLUMNS)
            self.assertEqual(command_rows[0]["request_pattern"], "ci_fix")
            self.assertEqual(command_rows[0]["task_type_label"], "CI Repair")
            self.assertIn("fixing_cause", command_rows[0])
            self.assertEqual(float(command_rows[0]["headline_context_tokens"]), 50.0)
            self.assertEqual(float(command_rows[0]["allocated_reasoning_tokens"]), 2.0)

            payload = json.loads((output_dir / "windows" / "session-a-p1.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["request_pattern"], "ci_fix")
            self.assertEqual(payload["request_pattern_rule_id"], "base:ci_fix:prompt_preview")
            self.assertEqual(payload["task_type"], "ci_repair")
            self.assertEqual(payload["task_type_reason"], "regex_rule:ci_repair")
            self.assertEqual(payload["headline_context_tokens"], 80.0)
            self.assertEqual(payload["headline_cache_read_tokens"], 20.0)
            self.assertEqual(payload["headline_output_tokens"], 10.0)
            self.assertEqual(payload["reasoning_tokens_tracked"], 3.0)
            self.assertEqual(payload["window_file"], "session-a-p1.json")
            self.assertEqual(payload["commands"][0]["headline_context_tokens"], 50.0)
            self.assertEqual(payload["commands"][0]["request_pattern"], "ci_fix")
            self.assertTrue(payload["timeline"])
            self.assertIn("headline_context_cost_usd", payload["timeline"][0])
            self.assertIn("conversation_entries", payload["timeline"][0])
            self.assertIn("message_preview", payload["timeline"][0])


            expected_payload_fields = {
                "window_file",
                "session_id",
                "prompt_index",
                "session_date",
                "source_file",
                "short_title",
                "prompt_preview",
                "previous_prompt_preview",
                "first_prompt_preview",
                "final_answer_preview",
                "session_cwd",
                "request_pattern",
                "request_pattern_path",
                "request_pattern_depth",
                "request_pattern_rule_id",
                "request_pattern_confidence",
                "task_type",
                "task_type_label",
                "task_type_confidence",
                "task_type_classifier",
                "task_type_reason",
                "task_type_source",
                "task_label",
                "dominant_fixing_cause",
                "fixing_cause_rollup",
                "phase_rollup",
                "efficiency_rollup",
                "timeline",
                "prompt_input_tokens",
                "prompt_cache_read_tokens",
                "prompt_cache_creation_tokens",
                "prompt_output_tokens",
                "prompt_reasoning_tokens",
                "prompt_total_tokens",
                "prompt_derived_total_cost_usd",
                "headline_context_tokens",
                "headline_context_cost_usd",
                "headline_cache_read_tokens",
                "headline_cache_read_cost_usd",
                "headline_output_tokens",
                "headline_output_cost_usd",
                "reasoning_tokens_tracked",
                "command_count",
                "tool_count",
                "total_cost_usd",
                "commands",
            }
            self.assertEqual(set(payload.keys()), expected_payload_fields)
            explorer_html = (output_dir / "explorer.html").read_text(encoding="utf-8")
            self.assertIn("function loadStaticBootstrap()", explorer_html)
            self.assertIn("window.__COST_EXPLORER_STATIC_SUMMARY__", (output_dir / "summary.js").read_text(encoding="utf-8"))
            self.assertIn("window.__COST_EXPLORER_STATIC_WINDOW_ROWS__", (output_dir / "window-rows.js").read_text(encoding="utf-8"))
            self.assertIn("window.__COST_EXPLORER_STATIC_COMMAND_ROWS__", (output_dir / "command-rows.js").read_text(encoding="utf-8"))
            self.assertIn(
                'window.__COST_EXPLORER_STATIC_WINDOWS__["session-a-p1.json"]',
                (output_dir / "windows-js" / "session-a-p1.json.js").read_text(encoding="utf-8"),
            )


            with (output_dir / "commands.csv").open(newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle))
            self.assertEqual(header, report.COMMANDS_COLUMNS)
            with (output_dir / "windows.csv").open(newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle))
            self.assertEqual(header, report.WINDOWS_COLUMNS)

    def test_builder_buckets_conversation_entries_by_chunk(self) -> None:
        request_pattern_config = {
            "version": "patterns-test",
            "layers": [
                {
                    "id": "base",
                    "default": "other",
                    "rules": [
                        {"id": "ci_fix", "regex": ["fix ci failure"], "confidence": "high"},
                    ],
                }
            ],
        }
        task_config = {
            "version": "tasks-test",
            "categories": [
                {"id": "uncategorized", "label": "Uncategorized"},
                {"id": "ci_repair", "label": "CI Repair"},
            ],
            "defaults": {"id": "uncategorized", "confidence": "low", "reason": "default"},
            "context": {"fields": [{"name": "prompt_preview", "weight": 1.0}]},
            "classifiers": [
                {
                    "id": "regex-primary",
                    "type": "regex",
                    "enabled": True,
                    "rules": [
                        {"id": "ci_repair", "regex": ["fix ci failure"], "confidence": "high"},
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_path = root / "session-a.jsonl"
            write_jsonl(
                session_path,
                [
                    {"message": {"role": "user", "content": [{"type": "input_text", "text": "Please fix the parser tests."}]}},
                    {"payload": {"type": "function_call", "name": "edit", "arguments": "{\"path\":\"src/parser.py\"}"}},
                    {"payload": {"type": "function_call_output", "output": "patched src/parser.py"}},
                    {"payload": {"type": "message", "role": "assistant", "content": [{"text": "Updated the parser guard and am about to run tests."}]}},
                    {"payload": {"type": "function_call", "name": "bash", "arguments": "pytest tests/test_parser.py"}},
                    {"payload": {"type": "message", "role": "assistant", "content": [{"text": "Tests are green now."}]}},
                ],
            )
            rows = [
                fixture_row(
                    file=str(session_path),
                    prompt_index="1",
                    command_index="1",
                    prompt_preview="Fix CI failure in parser",
                    agent_tool_intention="feature_implementation_edit",
                    function_name="edit",
                    shell_verb="",
                    command_preview="edit src/parser.py",
                    allocated_input_tokens="30",
                    allocated_cache_read_tokens="5",
                    allocated_cache_creation_tokens="0",
                    allocated_output_tokens="5",
                    allocated_reasoning_tokens="1",
                    allocated_total_tokens="35",
                    allocated_total_cost_usd="0.4",
                ),
                fixture_row(
                    file=str(session_path),
                    prompt_index="1",
                    command_index="2",
                    prompt_preview="Fix CI failure in parser",
                    agent_tool_intention="test_execution",
                    function_name="bash",
                    shell_verb="pytest",
                    command_preview="pytest tests/test_parser.py",
                    allocated_input_tokens="20",
                    allocated_cache_read_tokens="5",
                    allocated_cache_creation_tokens="0",
                    allocated_output_tokens="5",
                    allocated_reasoning_tokens="1",
                    allocated_total_tokens="25",
                    allocated_total_cost_usd="0.3",
                ),
            ]
            input_path = root / "usage.csv"
            output_dir = root / "out"
            write_csv(input_path, rows)
            write_json(root / "request-patterns.json", request_pattern_config)
            write_json(root / "task-categories.json", task_config)
            argv = sys.argv[:]
            try:
                sys.argv = [
                    str(SCRIPT_PATH),
                    "--input",
                    str(input_path),
                    "--output-dir",
                    str(output_dir),
                    "--request-pattern-config",
                    str(root / "request-patterns.json"),
                    "--task-categorization-config",
                    str(root / "task-categories.json"),
                ]
                self.assertEqual(report.main(), 0)
            finally:
                sys.argv = argv

            payload = json.loads((output_dir / "windows" / "session-a-p1.json").read_text(encoding="utf-8"))
        timeline = payload["timeline"]
        self.assertEqual(len(timeline), 2)
        self.assertEqual(timeline[0]["display_title"], "Step 1 · implementation")
        self.assertEqual(timeline[0]["message_preview"], "Please fix the parser tests.")
        self.assertEqual([entry["line_number"] for entry in timeline[0]["conversation_entries"]], [1, 2, 3, 4])
        self.assertEqual([entry["role_label"] for entry in timeline[0]["conversation_entries"]], ["User", "Tool", "Tool", "Assistant"])
        self.assertEqual([entry["command_index"] for entry in timeline[0]["conversation_entries"]], [1, 1, 1, 1])
        self.assertEqual([entry["line_number"] for entry in timeline[1]["conversation_entries"]], [5, 6])
        self.assertEqual(timeline[1]["conversation_entries"][1]["text"], "Tests are green now.")
        self.assertTrue(timeline[0]["examples"])

    def test_load_task_categorizer_disables_codex_classifiers(self) -> None:
        config = {
            "version": "tasks-test",
            "categories": [
                {"id": "uncategorized", "label": "Uncategorized"},
                {"id": "ci_repair", "label": "CI Repair"},
            ],
            "defaults": {"id": "uncategorized", "confidence": "low", "reason": "default"},
            "context": {"fields": [{"name": "prompt_preview", "weight": 1.0}]},
            "classifiers": [
                {"id": "regex-primary", "type": "regex", "enabled": True, "rules": [{"id": "ci_repair", "regex": ["fix ci failure"], "confidence": "high"}]},
                {"id": "codex-fallback", "type": "codex", "enabled": True, "prompt": "ignored", "command": []},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "task-config.json"
            write_json(path, config)
            categorizer = report.load_task_categorizer(str(path))
        codex = next(classifier for classifier in categorizer.config["classifiers"] if classifier["type"] == "codex")
        self.assertFalse(codex["enabled"])
        classified = categorizer.classify({"prompt_preview": "Fix CI failure in parser"})
        self.assertEqual(classified.task_type, "ci_repair")
        self.assertEqual(classified.task_type_classifier, "regex-primary")


if __name__ == "__main__":
    unittest.main()
