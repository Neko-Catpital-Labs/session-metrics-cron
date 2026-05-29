#!/usr/bin/env python3
"""Fixture tests for v4 command attribution extraction and costing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import mixpanel_export_usage as exporter
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

    def test_v4_2_exact_origin_prefixes(self) -> None:
        cases = [
            ("Generated task for invoker: implement the feature", "generated_invoker_task"),
            ("Invoker autofix: repair the failed tests", "invoker_auto_fix"),
            ("Fix invoker task failure from workflow logs", "invoker_task_failure_fix"),
            ("Resolve merge failure after rebase", "merge_failure_fix"),
            ("Create PR body for invoker workflow", "invoker_create_pr"),
            ("A previous agent produced the plan below. Implement it.", "previous_agent_plan"),
            ("Read /tmp/invoker-agent-prompt-abc123 and continue", "prompt_file_task_needs_review"),
            ("GitHub Actions failed checks need a fix", "ci_failure_fix"),
            ("Rebase the branch stack onto upstream/master", "branch_stack_maintenance"),
            ("Please implement the requested change", "human_direct_request"),
        ]
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                row = {"prompt_preview": prompt}
                self.assertEqual(report.classify_primary_why_v4_2(row)[0], expected)

    def test_v4_2_outputs_new_fields_without_legacy_primary_columns(self) -> None:
        rows = [
            {
                "schema_version": "usage_command_attribution_v4",
                "file": "/tmp/session-5.jsonl",
                "bucket": "execution",
                "prompt_index": 1,
                "command_index": 1,
                "function_name": "exec_command",
                "shell_verb": "pytest",
                "command_preview": "pytest tests/test_api.py",
                "target": "tests/test_api.py",
                "primary_why": "test_or_build_execution",
                "prompt_primary_why": "legacy",
                "row_primary_why": "legacy",
                "prompt_preview": "Run tests for the implementation",
                "allocated_total_cost_usd": 0.1,
            }
        ]

        enriched, review_rows = report.build_command_attribution_v4_2_rows(rows)

        self.assertEqual(review_rows, [])
        self.assertEqual(enriched[0]["schema_version"], "usage_command_attribution_v4_2")
        self.assertEqual(enriched[0]["primary_why"], "human_direct_request")
        self.assertEqual(enriched[0]["prompt_task_kind"], "test_validation")
        self.assertEqual(enriched[0]["agent_tool_intention"], "test_execution")
        self.assertEqual(enriched[0]["primary_why_confidence"], "high")
        self.assertEqual(enriched[0]["prompt_task_kind_confidence"], "high")
        self.assertEqual(enriched[0]["agent_tool_intention_confidence"], "high")
        self.assertEqual(enriched[0]["classification_agreement"], "agree")
        self.assertNotIn("prompt_primary_why", enriched[0])
        self.assertNotIn("row_primary_why", enriched[0])

    def test_v4_2_purpose_oriented_edit_intentions(self) -> None:
        cases = [
            ("Fix the bug causing failed tests", "apply_patch", "apply_patch", "src/app.py", "bug_fix_edit"),
            ("Implement the new dashboard feature", "apply_patch", "apply_patch", "src/app.py", "feature_implementation_edit"),
            ("Refactor the helper module", "apply_patch", "apply_patch", "src/helpers.py", "refactor_edit"),
            ("Update documentation for setup", "apply_patch", "apply_patch", "README.md", "documentation_edit"),
            ("Add golden fixture tests", "apply_patch", "apply_patch", "scripts/test-command-attribution.py", "test_or_proof_edit"),
        ]
        for prompt, fn, verb, target, expected in cases:
            with self.subTest(expected=expected):
                row = {
                    "prompt_preview": prompt,
                    "function_name": fn,
                    "shell_verb": verb,
                    "command_preview": verb,
                    "target": target,
                }
                self.assertEqual(report.classify_agent_tool_intention_v4_2(row)[0], expected)

    def test_v4_2_cluster_disagreement_routes_to_review(self) -> None:
        row = {
            "schema_version": "usage_command_attribution_v4",
            "file": "/tmp/session-6.jsonl",
            "bucket": "execution",
            "prompt_index": 1,
            "command_index": 1,
            "function_name": "exec_command",
            "shell_verb": "pytest",
            "command_preview": "pytest tests/test_api.py",
            "target": "tests/test_api.py",
            "prompt_preview": "Run tests for the implementation",
            "allocated_total_cost_usd": 0.2,
        }
        cluster_key = report._cluster_key_v4_2(row)

        enriched, review_rows = report.build_command_attribution_v4_2_rows(
            [row],
            {
                cluster_key: {
                    "primary_why": "human_direct_request",
                    "prompt_task_kind": "implementation",
                    "agent_tool_intention": "test_execution",
                }
            },
        )

        self.assertEqual(enriched[0]["prompt_task_kind"], "needs_review")
        self.assertEqual(enriched[0]["prompt_task_kind_confidence"], "needs_review")
        self.assertEqual(enriched[0]["classification_agreement"], "needs_review")
        self.assertIn("deterministic_codex_disagreement", enriched[0]["review_reason"])
        self.assertEqual(len(review_rows), 1)
        self.assertEqual(review_rows[0]["classification_cluster_key"], cluster_key)

    def test_v4_2_unapproved_cluster_bucket_stops_finalization(self) -> None:
        row = {
            "schema_version": "usage_command_attribution_v4",
            "file": "/tmp/session-7.jsonl",
            "bucket": "execution",
            "prompt_index": 1,
            "command_index": 1,
            "function_name": "apply_patch",
            "shell_verb": "apply_patch",
            "command_preview": "apply_patch",
            "target": "src/app.py",
            "prompt_preview": "Implement the requested change",
            "allocated_total_cost_usd": 0.001,
        }
        cluster_key = report._cluster_key_v4_2(row)

        with self.assertRaisesRegex(ValueError, "Unapproved v4.2 classifier bucket"):
            report.build_command_attribution_v4_2_rows(
                [row],
                {
                    cluster_key: {
                        "primary_why": "new_unapproved_origin",
                        "prompt_task_kind": "implementation",
                        "agent_tool_intention": "feature_implementation_edit",
                    }
                },
            )

    def test_v4_2_mixpanel_props_omit_legacy_classifier_fields(self) -> None:
        task_categorizer = exporter.TaskCategorizer(exporter.DEFAULT_TASK_CATEGORIZATION_CONFIG)
        request_categorizer = exporter.RequestPatternCategorizer(exporter.DEFAULT_REQUEST_PATTERN_CONFIG)
        rows = [
            {
                "schema_version": "usage_command_attribution_v4_2",
                "classification_revision": "classifier_v4_2",
                "file": "/tmp/session-8.jsonl",
                "model": "codex",
                "bucket": "execution",
                "prompt_index": "1",
                "command_index": "1",
                "function_name": "apply_patch",
                "shell_verb": "apply_patch",
                "command_preview": "apply_patch",
                "command_hash": "abc",
                "target": "src/app.py",
                "primary_why": "human_direct_request",
                "prompt_task_kind": "implementation",
                "agent_tool_intention": "feature_implementation_edit",
                "agent_tool_intention_source": "command_mechanics",
                "primary_why_confidence": "high",
                "prompt_task_kind_confidence": "high",
                "agent_tool_intention_confidence": "high",
                "classification_agreement": "agree",
                "review_reason": "",
                "prompt_primary_why": "legacy",
                "row_primary_why": "legacy",
            }
        ]

        events = exporter.build_command_attribution_events(
            rows,
            [],
            "token",
            "distinct",
            "2026-05-28",
            task_categorizer,
            request_categorizer,
        )

        props = events[0].properties
        self.assertEqual(props["schema_version"], "usage_command_attribution_v4_2")
        self.assertEqual(props["prompt_task_kind"], "implementation")
        self.assertEqual(props["agent_tool_intention"], "feature_implementation_edit")
        self.assertNotIn("prompt_primary_why", props)
        self.assertNotIn("row_primary_why", props)
        self.assertNotIn("why_tags", props)
        self.assertNotIn("tool_action", props)

    def test_v4_3_spawn_agent_extracts_and_classifies_branch_stack_task(self) -> None:
        window = report.PromptWindow(prompt_index=1, prompt_text="Delegate branch stack work")
        call = report.add_command_call(
            window,
            "spawn_agent",
            {
                "message": "Rebase the branch stack onto upstream/master and cherry-pick the fix.",
                "agent_type": "worker",
            },
            "call-1",
        )
        self.assertIsNotNone(call)
        report.attach_command_output(window, "call-1", '{"agent_id":"agent-123","nickname":"Stacker"}')
        row = {
            "schema_version": "usage_command_attribution_v4",
            "file": "/tmp/session-9.jsonl",
            "bucket": "execution",
            "prompt_index": 1,
            "command_index": 1,
            "function_name": call.function_name,
            "shell_verb": call.shell_verb,
            "command_preview": call.command_preview,
            "command_hash": call.command_hash,
            "target": call.target,
            "prompt_preview": "Delegate branch stack work",
            "delegated_agent_action": call.delegated_agent_action,
            "delegated_agent_id": call.delegated_agent_id,
            "delegated_agent_type": call.delegated_agent_type,
            "delegated_agent_nickname": call.delegated_agent_nickname,
            "delegated_task_preview": call.delegated_task_preview,
            "delegated_task_hash": call.delegated_task_hash,
            "allocated_total_cost_usd": 0.1,
        }

        enriched, review = report.build_command_attribution_v4_3_rows([row])

        self.assertEqual(review, [])
        self.assertEqual(enriched[0]["schema_version"], "usage_command_attribution_v4_3")
        self.assertEqual(enriched[0]["classification_revision"], "classifier_v4_3")
        self.assertEqual(enriched[0]["agent_tool_intention"], "branch_stack_orchestration")
        self.assertEqual(enriched[0]["tool_execution_mode"], "agent_delegated")
        self.assertEqual(enriched[0]["delegated_agent_action"], "spawn")
        self.assertEqual(enriched[0]["delegated_agent_id"], "agent-123")
        self.assertEqual(enriched[0]["delegated_agent_nickname"], "Stacker")
        self.assertEqual(enriched[0]["agent_tool_intention_source"], "delegated_task_message")

    def test_v4_3_spawn_agent_classifies_bug_fix_task(self) -> None:
        rows = [
            {
                "schema_version": "usage_command_attribution_v4",
                "file": "/tmp/session-10.jsonl",
                "bucket": "execution",
                "prompt_index": 1,
                "command_index": 1,
                "function_name": "spawn_agent",
                "command_preview": "Fix the regression causing failed tests",
                "prompt_preview": "Delegate the bug fix",
                "delegated_agent_action": "spawn",
                "delegated_agent_id": "agent-bug",
                "delegated_agent_type": "worker",
                "delegated_task_preview": "Fix the regression causing failed tests",
                "delegated_task_hash": report.digest_text("Fix the regression causing failed tests"),
                "allocated_total_cost_usd": 0.1,
            }
        ]

        enriched, _review = report.build_command_attribution_v4_3_rows(rows)

        self.assertEqual(enriched[0]["agent_tool_intention"], "bug_fix_edit")
        self.assertEqual(enriched[0]["tool_execution_mode"], "agent_delegated")

    def test_v4_3_agent_control_inherits_target_context(self) -> None:
        rows = [
            {
                "schema_version": "usage_command_attribution_v4",
                "file": "/tmp/session-11.jsonl",
                "bucket": "execution",
                "prompt_index": 1,
                "command_index": 1,
                "function_name": "spawn_agent",
                "command_preview": "Implement a new dashboard feature",
                "prompt_preview": "Delegate implementation",
                "delegated_agent_action": "spawn",
                "delegated_agent_id": "agent-impl",
                "delegated_agent_type": "worker",
                "delegated_task_preview": "Implement a new dashboard feature",
                "delegated_task_hash": report.digest_text("Implement a new dashboard feature"),
                "allocated_total_cost_usd": 0.1,
            },
            {
                "schema_version": "usage_command_attribution_v4",
                "file": "/tmp/session-11.jsonl",
                "bucket": "execution",
                "prompt_index": 1,
                "command_index": 2,
                "function_name": "wait_agent",
                "target": "agent-impl",
                "delegated_agent_action": "wait",
                "delegated_agent_id": "agent-impl",
                "prompt_preview": "Delegate implementation",
                "allocated_total_cost_usd": 0.1,
            },
        ]

        enriched, review = report.build_command_attribution_v4_3_rows(rows)

        self.assertEqual(review, [])
        self.assertEqual(enriched[1]["agent_tool_intention"], "feature_implementation_edit")
        self.assertEqual(enriched[1]["agent_tool_intention_source"], "delegated_agent_context")
        self.assertEqual(enriched[1]["prompt_task_kind"], enriched[0]["prompt_task_kind"])

    def test_v4_3_agent_control_missing_context_needs_review(self) -> None:
        rows = [
            {
                "schema_version": "usage_command_attribution_v4",
                "file": "/tmp/session-12.jsonl",
                "bucket": "execution",
                "prompt_index": 1,
                "command_index": 1,
                "function_name": "close_agent",
                "target": "agent-missing",
                "delegated_agent_action": "close",
                "delegated_agent_id": "agent-missing",
                "prompt_preview": "Close a delegated agent",
                "allocated_total_cost_usd": 0.1,
            }
        ]

        enriched, review = report.build_command_attribution_v4_3_rows(rows)

        self.assertEqual(enriched[0]["agent_tool_intention"], "needs_review")
        self.assertEqual(enriched[0]["tool_execution_mode"], "agent_delegated")
        self.assertEqual(enriched[0]["classification_agreement"], "needs_review")
        self.assertEqual(len(review), 1)

    def test_v4_3_direct_git_cherry_pick_is_branch_stack_direct_tool(self) -> None:
        rows = [
            {
                "schema_version": "usage_command_attribution_v4",
                "file": "/tmp/session-13.jsonl",
                "bucket": "execution",
                "prompt_index": 1,
                "command_index": 1,
                "function_name": "exec_command",
                "shell_verb": "git",
                "command_preview": "git cherry-pick abc123",
                "prompt_preview": "Cherry-pick the stack fix",
                "allocated_total_cost_usd": 0.1,
            }
        ]

        enriched, _review = report.build_command_attribution_v4_3_rows(rows)

        self.assertEqual(enriched[0]["agent_tool_intention"], "branch_stack_orchestration")
        self.assertEqual(enriched[0]["tool_execution_mode"], "direct_tool")

    def test_v4_3_ssh_retains_remote_execution_mode(self) -> None:
        rows = [
            {
                "schema_version": "usage_command_attribution_v4",
                "file": "/tmp/session-14.jsonl",
                "bucket": "execution",
                "prompt_index": 1,
                "command_index": 1,
                "function_name": "exec_command",
                "shell_verb": "ssh",
                "command_preview": "ssh builder 'pnpm run test:all'",
                "prompt_preview": "Run remote validation",
                "allocated_total_cost_usd": 0.1,
            }
        ]

        enriched, _review = report.build_command_attribution_v4_3_rows(rows)

        self.assertEqual(enriched[0]["tool_execution_mode"], "remote_command")
        self.assertNotEqual(enriched[0]["agent_tool_intention"], "branch_stack_orchestration")

    def test_v4_3_mixpanel_props_include_delegated_fields(self) -> None:
        task_categorizer = exporter.TaskCategorizer(exporter.DEFAULT_TASK_CATEGORIZATION_CONFIG)
        request_categorizer = exporter.RequestPatternCategorizer(exporter.DEFAULT_REQUEST_PATTERN_CONFIG)
        rows = [
            {
                "schema_version": "usage_command_attribution_v4_3",
                "classification_revision": "classifier_v4_3",
                "file": "/tmp/session-15.jsonl",
                "model": "codex",
                "bucket": "execution",
                "prompt_index": "1",
                "command_index": "1",
                "function_name": "spawn_agent",
                "command_preview": "Rebase the branch stack",
                "command_hash": "def",
                "primary_why": "branch_stack_maintenance",
                "prompt_task_kind": "branch_stack",
                "agent_tool_intention": "branch_stack_orchestration",
                "agent_tool_intention_source": "delegated_task_message",
                "tool_execution_mode": "agent_delegated",
                "delegated_agent_action": "spawn",
                "delegated_agent_id": "agent-123",
                "delegated_agent_type": "worker",
                "delegated_agent_nickname": "Stacker",
                "delegated_task_preview": "Rebase the branch stack",
                "delegated_task_hash": "hash",
            }
        ]

        events = exporter.build_command_attribution_events(
            rows,
            [],
            "token",
            "distinct",
            "2026-05-28",
            task_categorizer,
            request_categorizer,
        )

        props = events[0].properties
        self.assertEqual(props["schema_version"], "usage_command_attribution_v4_3")
        self.assertEqual(props["tool_execution_mode"], "agent_delegated")
        self.assertEqual(props["delegated_agent_action"], "spawn")
        self.assertEqual(props["delegated_agent_id"], "agent-123")

    def test_v4_2_agent_tools_remain_remote_orchestration(self) -> None:
        row = {
            "prompt_preview": "Delegate branch stack work",
            "function_name": "spawn_agent",
            "command_preview": "Rebase the branch stack",
            "delegated_task_preview": "Rebase the branch stack",
        }

        self.assertEqual(report.classify_agent_tool_intention_v4_2(row)[0], "remote_orchestration")


if __name__ == "__main__":
    unittest.main()
