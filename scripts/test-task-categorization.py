#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PATH = REPO_ROOT / "scripts/mixpanel_export_usage.py"
spec = importlib.util.spec_from_file_location("mixpanel_export_usage", EXPORTER_PATH)
assert spec and spec.loader
exporter = importlib.util.module_from_spec(spec)
sys.modules["mixpanel_export_usage"] = exporter
spec.loader.exec_module(exporter)


def row(text: str, *, session_cwd: str = "") -> dict[str, str]:
    return {
        "prompt_preview": text,
        "previous_prompt_preview": "",
        "first_prompt_preview": "",
        "final_answer_preview": "",
        "session_cwd": session_cwd,
    }


def base_config() -> dict:
    return json.loads(json.dumps(exporter.DEFAULT_TASK_CATEGORIZATION_CONFIG))


class TaskCategorizationTests(unittest.TestCase):
    def test_yaml_config_loads(self) -> None:
        config = exporter.load_task_categorization_config(str(REPO_ROOT / "config/task-categorization.yaml"))
        exporter.validate_task_categorization_config(config)
        self.assertEqual(config["version"], "task_taxonomy_v1")

    def test_missing_config_falls_back_to_builtin(self) -> None:
        config = exporter.load_task_categorization_config("/tmp/does-not-exist-task-categorization.yaml")
        self.assertEqual(config["version"], "builtin_regex_v1")

    def test_invalid_regex_fails_clearly(self) -> None:
        config = base_config()
        config["classifiers"][0]["rules"][0]["regex"] = ["["]
        with self.assertRaisesRegex(RuntimeError, "Invalid task categorization regex"):
            exporter.validate_task_categorization_config(config)

    def test_duplicate_category_ids_fail(self) -> None:
        config = base_config()
        config["categories"].append({"id": "pr_review", "label": "Duplicate"})
        with self.assertRaisesRegex(RuntimeError, "Duplicate task categorization category id"):
            exporter.validate_task_categorization_config(config)

    def test_unknown_rule_category_fails(self) -> None:
        config = base_config()
        config["classifiers"][0]["rules"][0]["id"] = "not_a_category"
        with self.assertRaisesRegex(RuntimeError, "unknown category"):
            exporter.validate_task_categorization_config(config)

    def test_regex_categories(self) -> None:
        categorizer = exporter.TaskCategorizer(base_config())
        cases = {
            "please review this PR body": "pr_review",
            "submit to invoker as a workflow chain": "invoker_plan_submission",
            "rename the Invoker context menu button label": "uncategorized",
            "rebase this branch stack on upstream/master": "git_branch_stack",
            "investigate the root cause with a repro": "debug_repro",
            "capture a Playwright screenshot of the terminal UI": "ui_terminal_visual",
            "install pnpm dependencies": "dependency_setup",
            "CI has a failing test in pytest": "test_ci_failure",
            "prepare the release changelog": "release_packaging",
            "workflow failed and needs autofix": "workflow_repair",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(categorizer.classify(row(text)).task_type, expected)
    def test_invoker_repo_context_alone_does_not_mean_plan_submission(self) -> None:
        categorizer = exporter.TaskCategorizer(base_config())
        result = categorizer.classify(row("Fix the failing command.", session_cwd="/home/invoker/.invoker/worktrees/demo"))
        self.assertEqual(result.task_type, "uncategorized")

    def codex_config(self, command: list[str], timeout_seconds: int = 5) -> dict:
        config = base_config()
        config["version"] = "test_codex_v1"
        config["classifiers"].append(
            {
                "id": "codex_v1",
                "type": "codex",
                "enabled": True,
                "mode": "uncategorized_only",
                "timeout_seconds": timeout_seconds,
                "command": command,
                "prompt": "Categories:\n{categories}\nTask context:\n{context}",
            }
        )
        return config

    def write_mock_classifier(self, directory: Path, payload: dict, counter_path: Path | None = None) -> Path:
        script = directory / "mock_codex.py"
        counter_line = ""
        if counter_path is not None:
            counter_line = (
                f"counter = Path({str(counter_path)!r})\n"
                "counter.write_text(str(int(counter.read_text() or '0') + 1) if counter.exists() else '1')\n"
            )
        script.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            f"{counter_line}"
            "Path(sys.argv[2]).write_text(json.dumps("
            f"{payload!r}"
            "))\n"
        )
        return script

    def test_codex_success_overrides_uncategorized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = self.write_mock_classifier(Path(tmp), {"id": "debug_repro", "confidence": "medium", "reason": "mocked"})
            command = [sys.executable, str(script), "{schema_path}", "{output_path}"]
            categorizer = exporter.TaskCategorizer(self.codex_config(command), exporter.TaskClassificationCache(None))
            result = categorizer.classify(row("ambiguous coding task"))
            self.assertEqual(result.task_type, "debug_repro")
            self.assertEqual(result.task_type_classifier, "codex_v1")

    def test_codex_timeout_falls_back(self) -> None:
        command = [sys.executable, "-c", "import time; time.sleep(2)", "{schema_path}", "{output_path}"]
        categorizer = exporter.TaskCategorizer(self.codex_config(command, timeout_seconds=1), exporter.TaskClassificationCache(None))
        result = categorizer.classify(row("ambiguous coding task"))
        self.assertEqual(result.task_type, "uncategorized")
        self.assertEqual(result.task_type_reason, "codex_failed_fallback")

    def test_codex_invalid_category_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = self.write_mock_classifier(Path(tmp), {"id": "unknown", "confidence": "high", "reason": "bad"})
            command = [sys.executable, str(script), "{schema_path}", "{output_path}"]
            categorizer = exporter.TaskCategorizer(self.codex_config(command), exporter.TaskClassificationCache(None))
            result = categorizer.classify(row("ambiguous coding task"))
            self.assertEqual(result.task_type, "uncategorized")
            self.assertEqual(result.task_type_reason, "codex_failed_fallback")

    def test_codex_cache_avoids_second_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            counter = tmp_path / "counter.txt"
            script = self.write_mock_classifier(tmp_path, {"id": "debug_repro", "confidence": "medium", "reason": "mocked"}, counter)
            command = [sys.executable, str(script), "{schema_path}", "{output_path}"]
            cache = exporter.TaskClassificationCache(tmp_path / "cache.json")
            categorizer = exporter.TaskCategorizer(self.codex_config(command), cache)
            self.assertEqual(categorizer.classify(row("ambiguous coding task")).task_type, "debug_repro")
            self.assertEqual(categorizer.classify(row("ambiguous coding task")).task_type, "debug_repro")
            self.assertEqual(counter.read_text(), "1")


if __name__ == "__main__":
    unittest.main()
