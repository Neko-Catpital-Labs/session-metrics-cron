#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PATH = REPO_ROOT / "scripts/mixpanel_export_usage.py"
spec = importlib.util.spec_from_file_location("mixpanel_export_usage", EXPORTER_PATH)
assert spec and spec.loader
exporter = importlib.util.module_from_spec(spec)
sys.modules["mixpanel_export_usage"] = exporter
spec.loader.exec_module(exporter)


def row(text: str, cost: str = "1.0") -> dict[str, str]:
    return {
        "prompt_preview": text,
        "previous_prompt_preview": "",
        "first_prompt_preview": "",
        "final_answer_preview": "",
        "session_cwd": "",
        "file": "/tmp/session-1.jsonl",
        "model": "codex",
        "provider": "openai",
        "bucket": "execution",
        "prompt_index": "1",
        "derived_total_cost_usd": cost,
    }


def base_config() -> dict:
    return json.loads(json.dumps(exporter.DEFAULT_REQUEST_PATTERN_CONFIG))


class RequestPatternCategorizationTests(unittest.TestCase):
    def test_yaml_config_loads(self) -> None:
        config = exporter.load_request_pattern_config(str(REPO_ROOT / "config/request-patterns.yaml"))
        exporter.validate_request_pattern_config(config)
        self.assertEqual(config["version"], "request_pattern_layers_v1")

    def test_missing_config_falls_back_to_builtin(self) -> None:
        config = exporter.load_request_pattern_config("/tmp/does-not-exist-request-patterns.yaml")
        self.assertEqual(config["version"], "request_pattern_layers_v1")

    def test_invalid_regex_fails_clearly(self) -> None:
        config = base_config()
        config["layers"][0]["rules"][0]["regex"] = ["["]
        with self.assertRaisesRegex(RuntimeError, "Invalid request pattern regex"):
            exporter.validate_request_pattern_config(config)

    def test_duplicate_rule_ids_fail(self) -> None:
        config = base_config()
        config["layers"][1]["rules"][0]["id"] = config["layers"][0]["rules"][0]["id"]
        with self.assertRaisesRegex(RuntimeError, "Duplicate request pattern rule id"):
            exporter.validate_request_pattern_config(config)

    def test_missing_layer_default_fails(self) -> None:
        config = base_config()
        del config["layers"][0]["default"]
        with self.assertRaisesRegex(RuntimeError, "missing default"):
            exporter.validate_request_pattern_config(config)

    def test_duplicate_layer_ids_fail(self) -> None:
        config = base_config()
        config["layers"][1]["id"] = config["layers"][0]["id"]
        with self.assertRaisesRegex(RuntimeError, "Duplicate request pattern layer id"):
            exporter.validate_request_pattern_config(config)

    def test_unknown_continue_from_fails(self) -> None:
        config = base_config()
        config["layers"][1]["continue_from"] = ["missing_parent"]
        with self.assertRaisesRegex(RuntimeError, "unknown continue_from reference"):
            exporter.validate_request_pattern_config(config)

    def test_malformed_rule_fails(self) -> None:
        config = base_config()
        config["layers"][0]["rules"][0] = {"id": "broken"}
        with self.assertRaisesRegex(RuntimeError, "non-empty regex list"):
            exporter.validate_request_pattern_config(config)

    def test_recursive_default_only_behavior(self) -> None:
        config = {
            "version": "test_layers",
            "layers": [
                {"id": "one", "default": "other", "rules": [{"id": "matched_parent", "regex": ["parent"]}]},
                {"id": "two", "default": "uncategorized", "continue_from": ["other"], "rules": [{"id": "child", "regex": ["child"]}]},
            ],
        }
        categorizer = exporter.RequestPatternCategorizer(config)
        self.assertEqual(categorizer.classify(row("parent child")).request_pattern, "matched_parent")
        self.assertEqual(categorizer.classify(row("child only")).request_pattern, "child")

    def test_fixture_categories_and_uncategorized_cost_gate(self) -> None:
        categorizer = exporter.RequestPatternCategorizer(base_config())
        cases = [
            ("A previous agent produced the plan below. Implement the plan in a fresh context and carry the work through.", "previous_agent_plan_resume", "3.0"),
            ("Capture experiment proof artifacts for this workflow run.", "experiment_proof", "2.0"),
            ("Recreate all workflows and rebase the workflow branch stack.", "workflow_recreate_rebase", "2.0"),
            ("Sync this branch with upstream/master before continuing.", "master_branch_sync", "2.0"),
            ("Run the repro script and do failure diagnosis for the failing test.", "failure_diagnosis", "2.0"),
            ("Fix with agent conflict resolution after the merge conflict.", "fix_with_agent_conflict_resolution", "2.0"),
            ("Implement the refactor and make the code change.", "implementation_refactor", "2.0"),
            ("Analyze Mixpanel usage metrics and token cost share.", "cost_usage_analysis", "2.0"),
            ("Please review this PR body.", "pr_review", "2.0"),
            ("Install pnpm dependencies for the project.", "dependency_setup", "2.0"),
            ("What is a concise name for this utility?", "uncategorized", "0.2"),
        ]
        total_cost = 0.0
        uncategorized_cost = 0.0
        for text, expected, cost in cases:
            with self.subTest(text=text):
                result = categorizer.classify(row(text, cost))
                self.assertEqual(result.request_pattern, expected)
                if expected != "uncategorized":
                    self.assertIn(expected, result.request_pattern_path)
            total_cost += float(cost)
            if expected == "uncategorized":
                uncategorized_cost += float(cost)
        self.assertLess(uncategorized_cost / total_cost, 0.05)

    def test_request_payload_omits_request_subpattern(self) -> None:
        task_categorizer = exporter.TaskCategorizer(exporter.DEFAULT_TASK_CATEGORIZATION_CONFIG)
        request_categorizer = exporter.RequestPatternCategorizer(base_config())
        _key, _event_date, _prompt_index, _pattern, payload = exporter.request_base_payload(
            row("Please review this PR body."),
            "2026-05-25",
            task_categorizer,
            request_categorizer,
        )
        self.assertEqual(payload["diagnosis_version"], "request_pattern_layers_v1")
        self.assertEqual(payload["request_pattern"], "pr_review")
        self.assertIn("request_pattern_path", payload)
        self.assertNotIn("request_subpattern", payload)

    def test_request_tool_attribution_receives_pattern_metadata(self) -> None:
        task_categorizer = exporter.TaskCategorizer(exporter.DEFAULT_TASK_CATEGORIZATION_CONFIG)
        request_categorizer = exporter.RequestPatternCategorizer(base_config())
        tool_row = {
            **row(""),
            "dimension": "function_name",
            "name": "exec_command",
            "calls": "1",
            "allocated_total_tokens": "10",
            "allocated_total_cost_usd": "0.01",
        }
        events = exporter.build_request_tool_attribution_events(
            [tool_row],
            [row("Please review this PR body.")],
            "token",
            "distinct",
            "2026-05-25",
            task_categorizer,
            request_categorizer,
        )
        props = events[0].properties
        self.assertEqual(props["request_pattern"], "pr_review")
        self.assertEqual(props["request_pattern_config_version"], "request_pattern_layers_v1")
        self.assertNotIn("request_subpattern", props)


if __name__ == "__main__":
    unittest.main()
