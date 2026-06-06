#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "splitter_metric_tree_app.py"
spec = importlib.util.spec_from_file_location("splitter_metric_tree_app", SCRIPT_PATH)
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
sys.modules["splitter_metric_tree_app"] = app
spec.loader.exec_module(app)


class SplitterMetricTreeAppTests(unittest.TestCase):
    def test_query_from_params_defaults_and_caps_history(self) -> None:
        query = app.query_from_params({"history_runs": ["9999"]})

        self.assertEqual(query.metric_path, "planToResponseGraphScore")
        self.assertEqual(query.variant, "hinted")
        self.assertEqual(query.history_runs, app.MAX_HISTORY_RUNS)

    def test_query_from_params_rejects_bad_metric_path(self) -> None:
        with self.assertRaises(ValueError):
            app.query_from_params({"metric_path": ["foo; DROP TABLE x"]})

    def test_query_from_params_rejects_bad_variant(self) -> None:
        with self.assertRaises(ValueError):
            app.query_from_params({"variant": ["all"]})

    def test_sql_does_not_select_weighted_contribution_fields(self) -> None:
        latest_sql = app.parameterized_sql(app.latest_tree_sql("project.dataset.table"))
        history_sql = app.parameterized_sql(app.history_sql("project.dataset.table"))

        app.assert_no_weighted_fields(latest_sql)
        app.assert_no_weighted_fields(history_sql)
        self.assertNotIn("weighted_value", latest_sql.lower())
        self.assertNotIn("weighted_contribution", latest_sql.lower())
        self.assertIn("nodes.head_sha", latest_sql)
        self.assertIn("run_id", history_sql)
        self.assertIn("head_sha", history_sql)
        self.assertIn("@metric_path", latest_sql)
        self.assertIn("@variant", latest_sql)

    def test_literal_sql_escapes_params_for_metabase_backend(self) -> None:
        query = app.MetricTreeQuery(metric_path="root.child", variant="hinted", history_runs=20)
        sql = app.literal_sql(app.latest_tree_sql("project.dataset.table"), query)

        self.assertIn("'root.child'", sql)
        self.assertIn("'hinted'", sql)
        self.assertNotIn("@metric_path", sql)

    def test_run_metabase_dataset_shapes_rows(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_request(url: str, api_key: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            calls.append(payload)
            return {
                "data": {
                    "cols": [{"name": "metric_path"}, {"name": "score"}],
                    "rows": [["root", 0.9]],
                }
            }

        original = app.metabase_request
        app.metabase_request = fake_request
        try:
            rows = app.run_metabase_dataset("https://metabase.example", "key", 2, "SELECT 1 AS score")
        finally:
            app.metabase_request = original

        self.assertEqual(rows, [{"metric_path": "root", "score": 0.9}])
        self.assertEqual(calls[0]["database"], 2)

    def test_normalize_rows_keeps_score_and_weights(self) -> None:
        rows = app.normalize_rows(
            [
                {
                    "collected_at": "2026-06-05T14:35:08Z",
                    "run_id": "20260605T143508Z-00bc82e8b929",
                    "branch": "main",
                    "head_sha": "00bc82e8b929c7abeda87e65e66dfa769d5d3213",
                    "variant": "hinted",
                    "root_metric_id": "planToResponseGraphScore",
                    "metric_path": "planToResponseGraphScore.workflowRealizationScore",
                    "parent_metric_path": "planToResponseGraphScore",
                    "parent_metric_id": "planToResponseGraphScore",
                    "metric_id": "workflowRealizationScore",
                    "kind": "composite",
                    "depth": 1,
                    "relative_depth": 1,
                    "score": 0.9522,
                    "local_weight_pct": 45.0,
                    "effective_weight_pct": 45.0,
                    "description": "Measures whether intended plan nodes became response workflows.",
                    "why": "Low values mean workflow generation changed the planned work shape.",
                }
            ]
        )

        self.assertEqual(rows[0]["score"], 0.9522)
        self.assertEqual(rows[0]["local_weight_pct"], 45.0)
        self.assertEqual(rows[0]["effective_weight_pct"], 45.0)
        self.assertEqual(rows[0]["short_sha"], "00bc82e8b929")
        self.assertNotIn("weighted_value", rows[0])

    def test_normalize_history_groups_by_metric_path(self) -> None:
        history = app.normalize_history(
            [
                {
                    "metric_path": "root.a",
                    "variant": "hinted",
                    "collected_at": "2026-06-01T00:00:00Z",
                    "run_id": "20260601T000000Z-abcdef123456",
                    "branch": "main",
                    "head_sha": "abcdef1234567890",
                    "score": 0.5,
                },
                {
                    "metric_path": "root.a",
                    "variant": "hinted",
                    "collected_at": "2026-06-02T00:00:00Z",
                    "run_id": "20260602T000000Z-fedcba654321",
                    "branch": "main",
                    "head_sha": "fedcba6543217890",
                    "score": 0.75,
                },
            ]
        )

        self.assertEqual(len(history["root.a"]), 2)
        self.assertEqual(history["root.a"][1]["score"], 0.75)
        self.assertEqual(history["root.a"][1]["short_sha"], "fedcba654321")

    def test_static_history_chart_uses_time_axis_and_metadata_popup(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html").read_text()

        self.assertIn("function formatAxisTime", html)
        self.assertIn("formatAxisTime(point.collected_at)", html)
        self.assertIn("className = \"point-popup\"", html)
        self.assertIn("pointer-events: none", html)
        self.assertIn("activePopupIndex === pointIndex", html)
        self.assertIn("data-point-index", html)
        self.assertIn("<dt>SHA</dt>", html)
        self.assertIn("point.head_sha", html)
        self.assertNotIn("text-anchor=\"middle\">${escapeHtml(point.short_sha", html)

    def test_static_metric_tree_table_omits_kind_column(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html").read_text()

        self.assertNotIn("<th class=\"kind-col\">Kind</th>", html)
        self.assertNotIn("<td class=\"kind-col\">", html)
        self.assertNotIn(".kind-col", html)
        self.assertIn(".node-col { width: 390px; }", html)
        self.assertIn("tr[data-depth=\"0\"] .name", html)
        self.assertIn("white-space: nowrap", html)

    def test_static_metric_tree_table_ends_at_score_trend(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html").read_text()

        self.assertIn("<th class=\"spark-col\">Score Trend</th>", html)
        self.assertNotIn("<th>Metric Path</th>", html)
        self.assertNotIn("<td><div class=\"path\" title=\"${row.metric_path}\">", html)
        self.assertIn(".spark-col { width: 210px; }", html)
        self.assertIn("const width = 170;", html)
        self.assertIn("colspan=\"6\"", html)


if __name__ == "__main__":
    unittest.main()
