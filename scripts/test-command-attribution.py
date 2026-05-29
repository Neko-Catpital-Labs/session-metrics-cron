#!/usr/bin/env python3
"""Fixture tests for v4 command attribution extraction and costing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import planning_vs_execution_report as report


class CommandAttributionTests(unittest.TestCase):
    def test_codex_command_extraction_target_and_cost_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex.jsonl"
            rows = [
                {"timestamp": "2026-05-28T10:00:00Z", "type": "session_meta", "payload": {"cwd": "/repo"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "Investigate test failure"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "id": "call_1",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "pytest tests/test_api.py", "workdir": "/repo"}),
                    },
                },
                {"type": "response_item", "payload": {"type": "function_call_output", "id": "call_1", "output": "failed output"}},
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20, "total_tokens": 120}},
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows))
            session = report.parse_codex_session(path)

        self.assertIsNotNone(session)
        window = session.prompt_windows[0]  # type: ignore[union-attr]
        self.assertEqual(len(window.command_calls), 1)
        call = window.command_calls[0]
        self.assertEqual(call.shell_verb, "pytest")
        self.assertEqual(call.target_type, "path")
        self.assertEqual(call.target, "tests/test_api.py")
        self.assertGreater(call.output_token_estimate, 0)

        _sessions, _prompts, _tools, commands = report.build_rows_for_model(
            [session],  # type: ignore[list-item]
            {"costUSD": 1.0},
            {},
        )
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["schema_version"], "usage_command_attribution_v4")
        self.assertEqual(commands[0]["primary_why"], "test_or_build_execution")
        self.assertEqual(commands[0]["cost_is_estimated"], True)
        self.assertEqual(commands[0]["cost_allocation_method"], "prompt_cost_output_weighted_v1")

    def test_claude_command_extraction_output_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude.jsonl"
            rows = [
                {
                    "timestamp": "2026-05-28T10:00:00Z",
                    "type": "user",
                    "cwd": "/repo",
                    "message": {"role": "user", "content": "Read source"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "claude-sonnet-4-5-20250929",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                        "content": [{"type": "tool_use", "id": "tool_1", "name": "bash", "input": {"command": "rg foo src/main.ts", "cwd": "/repo"}}],
                    },
                },
                {
                    "type": "user",
                    "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool_1", "content": "src/main.ts:foo"}]},
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows))
            session = report.parse_claude_session(path)

        self.assertIsNotNone(session)
        call = session.prompt_windows[0].command_calls[0]  # type: ignore[union-attr]
        self.assertEqual(call.shell_verb, "rg")
        self.assertEqual(call.workdir, "/repo")
        self.assertGreater(call.output_chars, 0)
        why, classifier = report.classify_command_why("bash", "rg", "rg foo src/main.ts", "src/main.ts", "")
        self.assertEqual((why, classifier), ("source_inspection", "rules_v1"))

    def test_v4_1_read_in_failure_fix_prompt_serves_autofix(self) -> None:
        rows = [
            {
                "schema_version": "usage_command_attribution_v4",
                "file": "/tmp/session-1.jsonl",
                "bucket": "execution",
                "prompt_index": 1,
                "command_index": 1,
                "function_name": "read",
                "shell_verb": "",
                "command_preview": "",
                "target": "src/app.py",
                "primary_why": "uncategorized",
                "prompt_preview": "A build/test command failed. Fix the code and rerun the test.",
                "allocated_total_cost_usd": 0.1,
            }
        ]

        enriched = report.build_command_attribution_v4_1_rows(rows)

        self.assertEqual(enriched[0]["schema_version"], "usage_command_attribution_v4_1")
        self.assertEqual(enriched[0]["service_classifier_revision"], "service_context_v2")
        self.assertEqual(enriched[0]["tool_action"], "file_read")
        self.assertEqual(enriched[0]["service_of_why"], "autofix_or_failure_repair")
        self.assertEqual(enriched[0]["service_of_confidence"], "high")

    def test_v4_1_read_in_pr_body_prompt_serves_review(self) -> None:
        rows = [
            {
                "schema_version": "usage_command_attribution_v4",
                "file": "/tmp/session-2.jsonl",
                "bucket": "execution",
                "prompt_index": 1,
                "command_index": 1,
                "function_name": "read",
                "shell_verb": "",
                "command_preview": "",
                "target": "README.md",
                "primary_why": "uncategorized",
                "prompt_preview": "Generate a PR body for these changes.",
                "allocated_total_cost_usd": 0.1,
            }
        ]

        enriched = report.build_command_attribution_v4_1_rows(rows)

        self.assertEqual(enriched[0]["tool_action"], "file_read")
        self.assertEqual(enriched[0]["service_of_why"], "pr_review")

    def test_v4_1_write_stdin_inherits_previous_terminal_command(self) -> None:
        rows = [
            {
                "schema_version": "usage_command_attribution_v4",
                "file": "/tmp/session-3.jsonl",
                "bucket": "execution",
                "prompt_index": 1,
                "command_index": 1,
                "function_name": "exec_command",
                "shell_verb": "pytest",
                "command_preview": "pytest tests/test_api.py",
                "target": "tests/test_api.py",
                "primary_why": "test_or_build_execution",
                "prompt_preview": "Run tests",
                "allocated_total_cost_usd": 0.1,
            },
            {
                "schema_version": "usage_command_attribution_v4",
                "file": "/tmp/session-3.jsonl",
                "bucket": "execution",
                "prompt_index": 1,
                "command_index": 2,
                "function_name": "write_stdin",
                "shell_verb": "",
                "command_preview": "",
                "target": "",
                "primary_why": "uncategorized",
                "prompt_preview": "Run tests",
                "allocated_total_cost_usd": 0.1,
            },
        ]

        enriched = report.build_command_attribution_v4_1_rows(rows)

        self.assertEqual(enriched[1]["tool_action"], "terminal_input")
        self.assertEqual(enriched[1]["service_of_why"], "test_or_build_execution")
        self.assertEqual(enriched[1]["service_of_source"], "previous_command")

    def test_v4_1_analytics_and_trivial_process_control(self) -> None:
        rows = [
            {
                "schema_version": "usage_command_attribution_v4",
                "file": "/tmp/session-4.jsonl",
                "bucket": "execution",
                "prompt_index": 1,
                "command_index": 1,
                "function_name": "run_query",
                "shell_verb": "",
                "command_preview": "",
                "target": "",
                "primary_why": "uncategorized",
                "prompt_preview": "",
                "allocated_total_cost_usd": 0.1,
            },
            {
                "schema_version": "usage_command_attribution_v4",
                "file": "/tmp/session-4.jsonl",
                "bucket": "execution",
                "prompt_index": 1,
                "command_index": 2,
                "function_name": "exec_command",
                "shell_verb": "pwd",
                "command_preview": "pwd",
                "target": "pwd",
                "primary_why": "uncategorized",
                "prompt_preview": "",
                "allocated_total_cost_usd": 0.1,
            },
        ]

        enriched = report.build_command_attribution_v4_1_rows(rows)

        self.assertEqual(enriched[0]["service_of_why"], "reporting_or_analytics")
        self.assertEqual(enriched[0]["tool_action"], "analytics_query")
        self.assertEqual(enriched[1]["service_of_why"], "environment_or_process_control")
        self.assertEqual(enriched[1]["tool_action"], "environment_or_process_control")


if __name__ == "__main__":
    unittest.main()
