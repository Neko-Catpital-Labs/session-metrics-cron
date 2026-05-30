#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "session_phase_narrative_report.py"
spec = importlib.util.spec_from_file_location("session_phase_narrative_report", SCRIPT_PATH)
assert spec and spec.loader
report = importlib.util.module_from_spec(spec)
sys.modules["session_phase_narrative_report"] = report
spec.loader.exec_module(report)


def row(
    *,
    file: str = "/tmp/session-a.jsonl",
    prompt_index: str = "1",
    command_index: str = "1",
    cost: str = "1.0",
    prompt: str = "Implement the plan.",
    intention: str = "implementation_planning_inspection",
    function: str = "exec_command",
    shell: str = "rg",
    command: str = "rg foo src",
    parent_shell: str = "",
    parent_command: str = "",
) -> dict[str, str]:
    return {
        "schema_version": report.SCHEMA_VERSION,
        "classification_revision": report.CLASSIFICATION_REVISION,
        "session_date": "2026-05-01",
        "file": file,
        "prompt_index": prompt_index,
        "command_index": command_index,
        "allocated_total_cost_usd": cost,
        "allocated_total_tokens": "100",
        "prompt_preview": prompt,
        "first_prompt_preview": prompt,
        "final_answer_preview": "",
        "agent_tool_intention": intention,
        "work_motivation": "implementation",
        "function_name": function,
        "shell_verb": shell,
        "command_preview": command,
        "stdin_preview": "",
        "stdin_input_kind": "",
        "terminal_context_parent_shell_verb": parent_shell,
        "terminal_context_parent_command_preview": parent_command,
        "target": "",
    }


class SessionPhaseNarrativeReportTests(unittest.TestCase):
    def test_prompt_windows_group_by_session_id_and_prompt_index(self) -> None:
        rows = [
            row(file="/tmp/a.jsonl", prompt_index="1", cost="2"),
            row(file="/tmp/a.jsonl", prompt_index="2", cost="5"),
            row(file="/tmp/b.jsonl", prompt_index="1", cost="3"),
        ]
        windows = report.prompt_windows(rows)
        self.assertEqual([key for key, _rows in windows], [("a", "2"), ("b", "1"), ("a", "1")])

    def test_classifies_build_repair_and_final_proof(self) -> None:
        rows = [
            row(command_index="1", intention="implementation_planning_inspection", shell="rg", command="rg handler src", cost="1"),
            row(command_index="2", intention="implementation_edit", function="edit", shell="", command="", cost="2"),
            row(command_index="3", intention="test_execution", shell="pnpm", command="pnpm test src/foo.test.ts", cost="3"),
            row(
                command_index="4",
                prompt="A build/test command failed. Fix the code so the command succeeds.",
                intention="failure_diagnosis_inspection",
                shell="tail",
                command="tail -100 test.log",
                cost="4",
            ),
            row(command_index="5", intention="full_validation", shell="pnpm", command="pnpm test --all final", cost="5"),
        ]
        classified = report.classify_prompt_window(rows)
        phases = [item.workflow_phase for item in classified]
        self.assertIn("implementation", phases)
        self.assertIn("local_validation", phases)
        self.assertIn("repair_loop", phases)
        self.assertIn("final_proof", phases)
        repair = [item for item in classified if item.workflow_phase == "repair_loop"][0]
        self.assertEqual(repair.efficiency_label, "thrash")

    def test_git_hygiene_after_tests_is_not_repair_loop(self) -> None:
        rows = [
            row(command_index="1", intention="implementation_edit", function="edit", shell="", command="", cost="1"),
            row(command_index="2", intention="test_execution", shell="pnpm", command="pnpm test src/foo.test.ts", cost="1"),
            row(
                command_index="3",
                intention="branch_stack_orchestration",
                shell="git",
                command="git status --short --branch",
                cost="1",
            ),
            row(command_index="4", intention="diff_review", shell="git", command="git diff --check", cost="1"),
        ]
        classified = report.classify_prompt_window(rows)
        git_items = [item for item in classified if (item.row.get("shell_verb") or "") == "git"]
        self.assertEqual([item.workflow_phase for item in git_items], ["final_proof", "final_proof"])
        self.assertNotIn("repair_loop", [item.workflow_phase for item in git_items])

    def test_wait_for_git_hygiene_uses_parent_command_not_test_context(self) -> None:
        rows = [
            row(command_index="1", intention="implementation_edit", function="edit", shell="", command="", cost="1"),
            row(command_index="2", intention="test_execution", shell="pnpm", command="pnpm test src/foo.test.ts", cost="1"),
            row(
                command_index="3",
                intention="environment_initialization",
                function="write_stdin",
                shell="",
                command="",
                parent_shell="git",
                parent_command="git status --short --branch",
                cost="1",
            ),
        ]
        classified = report.classify_prompt_window(rows)
        self.assertEqual(classified[-1].workflow_phase, "final_proof")
        self.assertEqual(classified[-1].reason, "git_hygiene_or_diff_check")

    def test_post_test_orientation_without_failure_stays_orientation(self) -> None:
        rows = [
            row(command_index="1", intention="implementation_edit", function="edit", shell="", command="", cost="1"),
            row(command_index="2", intention="test_execution", shell="pnpm", command="pnpm test src/foo.test.ts", cost="1"),
            row(
                command_index="3",
                intention="implementation_planning_inspection",
                shell="rg",
                command="rg selectedAttempt src",
                cost="1",
            ),
        ]
        classified = report.classify_prompt_window(rows)
        self.assertEqual(classified[-1].workflow_phase, "orientation")
        self.assertEqual(classified[-1].efficiency_label, "expected_overhead")

    def test_repeated_failure_tests_still_become_repair_loop(self) -> None:
        rows = [
            row(command_index="1", intention="implementation_edit", function="edit", shell="", command="", cost="1"),
            row(
                command_index="2",
                prompt="A build/test command failed. Fix the code so the command succeeds.",
                intention="test_execution",
                shell="pnpm",
                command="pnpm test src/foo.test.ts",
                cost="1",
            ),
            row(
                command_index="3",
                prompt="A build/test command failed. Fix the code so the command succeeds.",
                intention="test_execution",
                shell="pnpm",
                command="pnpm test src/foo.test.ts",
                cost="1",
            ),
        ]
        classified = report.classify_prompt_window(rows)
        self.assertEqual(classified[-1].workflow_phase, "repair_loop")
        self.assertEqual(classified[-1].efficiency_label, "thrash")

    def test_read_command_with_test_intention_is_diagnosis_not_test_rerun(self) -> None:
        rows = [
            row(command_index="1", intention="implementation_edit", function="edit", shell="", command="", cost="1"),
            row(
                command_index="2",
                prompt="A build/test command failed. Fix the code so the command succeeds.",
                intention="test_execution",
                shell="pnpm",
                command="pnpm test src/foo.test.ts",
                cost="1",
            ),
            row(
                command_index="3",
                prompt="A build/test command failed. Fix the code so the command succeeds.",
                intention="test_execution",
                shell="sed",
                command="sed -n '1,220p' vitest.shared.ts",
                cost="1",
            ),
        ]
        classified = report.classify_prompt_window(rows)
        self.assertNotEqual(classified[-1].reason, "repeated_or_failure_driven_test_loop")
        self.assertEqual(classified[-1].workflow_phase, "failure_diagnosis")

    def test_write_window_payload_has_expected_shape(self) -> None:
        rows = [
            row(command_index="1", cost="1"),
            row(command_index="2", intention="ci_monitoring", shell="gh", command="gh pr checks 1 --watch=false", cost="2"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            payload = report.build_window_payload(1, ("session-a", "1"), rows, Path(tmp), False, 1)
        self.assertEqual(payload["rank"], 1)
        self.assertEqual(payload["session_id"], "session-a")
        self.assertEqual(payload["total_cost_usd"], 3.0)
        self.assertEqual(payload["narrative"]["review_status"], "deterministic_only")
        self.assertTrue(payload["phase_rollup"])
        self.assertTrue(payload["timeline"])

    def test_markdown_mentions_phase_and_efficiency_rollups(self) -> None:
        rows = [row(command_index="1", cost="1")]
        with tempfile.TemporaryDirectory() as tmp:
            payload = report.build_window_payload(1, ("session-a", "1"), rows, Path(tmp), False, 1)
        markdown = report.window_markdown(payload)
        self.assertIn("## Phase Rollup", markdown)
        self.assertIn("## Efficiency Rollup", markdown)
        self.assertIn("session-a", markdown)


if __name__ == "__main__":
    unittest.main()
