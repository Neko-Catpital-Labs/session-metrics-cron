#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
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
        self.assertIn("effective_weight", history_sql)
        self.assertIn("display_value", latest_sql)
        self.assertIn("diagnostic_value", latest_sql)
        self.assertIn("is_score", latest_sql)
        self.assertIn("display_unit", history_sql)
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
                    "display_value": 0.9522,
                    "display_unit": "score",
                    "is_score": True,
                    "local_weight_pct": 45.0,
                    "effective_weight_pct": 45.0,
                    "description": "Measures whether intended plan nodes became response workflows.",
                    "why": "Low values mean workflow generation changed the planned work shape.",
                }
            ]
        )

        self.assertEqual(rows[0]["score"], 0.9522)
        self.assertEqual(rows[0]["display_value"], 0.9522)
        self.assertTrue(rows[0]["is_score"])
        self.assertEqual(rows[0]["tree_path"], rows[0]["metric_path"])
        self.assertEqual(rows[0]["local_weight_pct"], 45.0)
        self.assertEqual(rows[0]["effective_weight_pct"], 45.0)
        self.assertEqual(rows[0]["short_sha"], "00bc82e8b929")
        self.assertNotIn("weighted_value", rows[0])

    def test_normalize_rows_nests_diagnostics_under_explained_score(self) -> None:
        rows = app.normalize_rows(
            [
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore",
                    "parent_metric_path": "root",
                    "metric_id": "responseStackLinkCorrectnessScore",
                    "kind": "composite",
                    "relative_depth": 0,
                    "is_score": True,
                    "score": 0.63,
                    "display_value": 0.63,
                    "display_unit": "score",
                },
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore.responseCorrectStackLinksScore",
                    "parent_metric_path": "root.responseStackLinkCorrectnessScore",
                    "metric_id": "responseCorrectStackLinksScore",
                    "kind": "leaf",
                    "relative_depth": 1,
                    "is_score": True,
                    "score": 0.63,
                    "display_value": 0.63,
                    "display_unit": "score",
                },
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore.responseCorrectStackLinkCount",
                    "parent_metric_path": "root.responseStackLinkCorrectnessScore",
                    "metric_id": "responseCorrectStackLinkCount",
                    "kind": "diagnostic",
                    "relative_depth": 1,
                    "is_score": False,
                    "score": None,
                    "diagnostic_value": 2.55,
                    "display_value": 2.55,
                    "display_unit": "avg_count",
                },
            ]
        )

        count = next(row for row in rows if row["metric_id"] == "responseCorrectStackLinkCount")
        self.assertEqual(
            count["tree_parent_path"],
            "root.responseStackLinkCorrectnessScore.responseCorrectStackLinksScore",
        )
        self.assertEqual(
            count["tree_path"],
            "root.responseStackLinkCorrectnessScore.responseCorrectStackLinksScore.responseCorrectStackLinkCount",
        )
        self.assertEqual(count["tree_relative_depth"], 2)
        self.assertEqual(count["display_value"], 2.55)
        self.assertEqual(count["display_unit"], "avg_count")

    def test_normalize_rows_adds_stack_link_formula_explanations(self) -> None:
        rows = app.normalize_rows(
            [
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore.responseCorrectStackLinksScore",
                    "parent_metric_path": "root.responseStackLinkCorrectnessScore",
                    "metric_id": "responseCorrectStackLinksScore",
                    "kind": "leaf",
                    "relative_depth": 1,
                    "is_score": True,
                    "score": 0.63,
                    "display_value": 0.63,
                    "display_unit": "score",
                },
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore.responseCorrectStackLinkCount",
                    "parent_metric_path": "root.responseStackLinkCorrectnessScore",
                    "metric_id": "responseCorrectStackLinkCount",
                    "kind": "diagnostic",
                    "relative_depth": 1,
                    "is_score": False,
                    "score": None,
                    "diagnostic_value": 2.55,
                    "display_value": 2.55,
                    "display_unit": "avg_count",
                },
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore.responseExtraStackLinkCount",
                    "parent_metric_path": "root.responseStackLinkCorrectnessScore",
                    "metric_id": "responseExtraStackLinkCount",
                    "kind": "diagnostic",
                    "relative_depth": 1,
                    "is_score": False,
                    "diagnostic_value": 1.95,
                    "display_value": 1.95,
                    "display_unit": "avg_count",
                },
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore.responseMissingStackLinkCount",
                    "parent_metric_path": "root.responseStackLinkCorrectnessScore",
                    "metric_id": "responseMissingStackLinkCount",
                    "kind": "diagnostic",
                    "relative_depth": 1,
                    "is_score": False,
                    "diagnostic_value": 1.95,
                    "display_value": 1.95,
                    "display_unit": "avg_count",
                },
            ]
        )

        score = next(row for row in rows if row["metric_id"] == "responseCorrectStackLinksScore")
        inputs = {item["label"]: item["value"] for item in score["explanation"]["inputs"]}
        self.assertEqual(score["explanation"]["formula"]["numerator"], "correct links")
        self.assertEqual(
            score["explanation"]["formula"]["denominator"],
            "max(expected links, actual links, 1)",
        )
        self.assertEqual(inputs["correct links"], 2.55)
        self.assertEqual(inputs["expected links"], 4.5)
        self.assertEqual(inputs["actual links"], 4.5)
        self.assertIn("per replay case", score["explanation"]["note"])

        count = next(row for row in rows if row["metric_id"] == "responseCorrectStackLinkCount")
        self.assertEqual(count["explanation"]["kind"], "diagnostic")
        self.assertEqual(count["explanation"]["result"]["value"], 2.55)
        self.assertIn("not normalized", count["explanation"]["note"])

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
                    "display_value": 0.5,
                    "display_unit": "score",
                    "is_score": True,
                    "effective_weight_pct": 15.0,
                },
                {
                    "metric_path": "root.a",
                    "variant": "hinted",
                    "collected_at": "2026-06-02T00:00:00Z",
                    "run_id": "20260602T000000Z-fedcba654321",
                    "branch": "main",
                    "head_sha": "fedcba6543217890",
                    "score": 0.75,
                    "display_value": 0.75,
                    "display_unit": "score",
                    "is_score": True,
                    "effective_weight_pct": 30.0,
                },
            ]
        )

        self.assertEqual(len(history["root.a"]), 2)
        self.assertEqual(history["root.a"][1]["score"], 0.75)
        self.assertEqual(history["root.a"][1]["display_value"], 0.75)
        self.assertTrue(history["root.a"][1]["is_score"])
        self.assertEqual(history["root.a"][1]["short_sha"], "fedcba654321")
        self.assertEqual(history["root.a"][1]["effective_weight_pct"], 30.0)

    def test_ready_response_reports_missing_bigquery_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            static_path = root / "index.html"
            rules_static_path = root / "rules.html"
            static_path.write_text("<html></html>")
            rules_static_path.write_text("<html></html>")
            run_dir = root / "target" / "stack-learning" / "runs" / "20260609T000000Z"
            run_dir.mkdir(parents=True)
            (run_dir / "pipeline-run.json").write_text(
                json.dumps(
                    {
                        "runId": "20260609T000000Z",
                        "headSha": "62197da994421e1e5c3ae3014e777ba46862c2b5",
                        "artifacts": {},
                    }
                )
            )

            original_credentials = os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            try:
                payload, status = app.ready_response(
                    static_path=static_path,
                    rules_static_path=rules_static_path,
                    workflow_analysis_root=root,
                    table="project.dataset.table",
                    project_id="project",
                    backend="bigquery",
                    metabase_url="",
                    metabase_api_key="",
                    bigquery_location="US",
                )
            finally:
                if original_credentials is not None:
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = original_credentials

        checks = {item["name"]: item for item in payload["checks"]}
        self.assertEqual(status, app.HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertFalse(payload["ok"])
        self.assertTrue(checks["static_page"]["ok"])
        self.assertTrue(checks["rules_page"]["ok"])
        self.assertTrue(checks["rules_artifacts"]["ok"])
        self.assertFalse(checks["bigquery_credentials"]["ok"])
        self.assertFalse(checks["bigquery_import"]["ok"])
        self.assertIn("GOOGLE_APPLICATION_CREDENTIALS", checks["bigquery_credentials"]["message"])

    def test_static_history_chart_uses_time_axis_and_metadata_popup(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html").read_text()

        self.assertIn("function formatAxisTime", html)
        self.assertIn("formatAxisTime(item.point.collected_at)", html)
        self.assertIn("className = \"point-popup\"", html)
        self.assertIn("pointer-events: none", html)
        self.assertIn("activePopupIndex === pointIndex", html)
        self.assertIn("data-point-index", html)
        self.assertIn("data-point-key", html)
        self.assertIn("<dt>SHA</dt>", html)
        self.assertIn("<dt>Effective</dt>", html)
        self.assertIn("point.head_sha", html)
        self.assertIn("Effective Weight Over Time", html)
        self.assertIn("Avg Count Over Time", html)
        self.assertIn("display_value", html)
        self.assertIn("effective_weight_pct", html)
        self.assertIn("effective-line", html)
        self.assertIn("effective-point", html)
        self.assertIn("historyLineChart(points", html)
        self.assertNotIn("text-anchor=\"middle\">${escapeHtml(point.short_sha", html)

    def test_static_metric_tree_table_omits_kind_column(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html").read_text()

        self.assertNotIn("<th class=\"kind-col\">Kind</th>", html)
        self.assertNotIn("<td class=\"kind-col\">", html)
        self.assertNotIn(".kind-col", html)
        self.assertIn(".node-col { width: 360px; }", html)
        self.assertIn(".desc-col { width: 320px; }", html)
        self.assertIn(".metric-col { width: 150px; }", html)
        self.assertIn("tr[data-depth=\"0\"] .name", html)
        self.assertIn("white-space: nowrap", html)
        self.assertIn("tr[data-depth=\"0\"] .node", html)
        self.assertIn("grid-template-columns: 24px minmax(0, 1fr)", html)

    def test_static_metric_tree_table_ends_at_score_trend(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html").read_text()

        self.assertIn("<th class=\"spark-col\">Score Trend</th>", html)
        self.assertIn("<th class=\"metric-col\">Score / Diagnostic</th>", html)
        self.assertIn("data-label=\"Score / Diagnostic\"", html)
        self.assertIn("diagnostic-value", html)
        self.assertIn("Avg Count", html)
        self.assertIn("info-button", html)
        self.assertIn("metric-info-popup", html)
        self.assertIn("formula-fraction", html)
        self.assertIn("formula.numerator", html)
        self.assertIn("formula.denominator", html)
        self.assertIn("renderMetricInfo", html)
        self.assertIn("row.tree_path", html)
        self.assertIn("row.tree_parent_path", html)
        self.assertNotIn("<th>Metric Path</th>", html)
        self.assertNotIn("<td><div class=\"path\" title=\"${row.metric_path}\">", html)
        self.assertIn(".spark-col { width: 190px; }", html)
        self.assertIn("const width = 170;", html)
        self.assertIn("colspan=\"6\"", html)

    def test_static_metric_tree_table_has_stable_min_width(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html").read_text()

        self.assertIn("overflow-x: auto", html)
        self.assertIn("overflow-y: hidden", html)
        self.assertIn("min-width: 1320px", html)
        self.assertNotIn("min-width: 1560px", html)
        self.assertNotIn("min-width: 1120px", html)

    def test_static_metric_tree_has_mobile_row_layout(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html").read_text()

        self.assertIn("@media (max-width: 760px)", html)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", html)
        self.assertIn("td[data-label]::before", html)
        self.assertIn("data-label=\"Metric Tree\"", html)
        self.assertIn("data-label=\"Score Trend\"", html)
        self.assertIn("table,\n      tbody", html)
        self.assertIn("min-width: 0", html)

    def test_rules_response_attaches_parent_scoped_subrules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "target" / "stack-learning" / "runs" / "20260608T000000Z"
            guidance_dir = run_dir / "guidance"
            reports_dir = run_dir / "reports"
            guidance_dir.mkdir(parents=True)
            reports_dir.mkdir(parents=True)
            (run_dir / "pipeline-run.json").write_text(
                json.dumps(
                    {
                        "runId": "20260608T000000Z",
                        "artifacts": {
                            "generatedRuleCatalog": "target/stack-learning/runs/20260608T000000Z/guidance/rule-catalog.json",
                            "effectiveRuleCatalog": "rules/rule-catalog.json",
                            "subrulesModel": "target/stack-learning/runs/20260608T000000Z/guidance/subrules.json",
                            "subruleCandidates": "target/stack-learning/runs/20260608T000000Z/guidance/subrule-candidates.json",
                            "subruleInvestigationReport": "target/stack-learning/runs/20260608T000000Z/reports/subrule-investigation.md",
                        },
                    }
                )
            )
            catalog = {
                "version": 1,
                "reasons": [{"id": "foundation-before-behavior", "message": "Foundation before behavior."}],
                "rules": [
                    {
                        "id": "foundation-before-behavior",
                        "priority": 100,
                        "relation": "precedes",
                        "reasonId": "foundation-before-behavior",
                        "before": [{"type": "tag", "id": "foundation"}],
                        "after": [{"type": "tag", "id": "behavior"}],
                    }
                ],
            }
            (guidance_dir / "rule-catalog.json").write_text(json.dumps(catalog))
            rules_dir = root / "rules"
            rules_dir.mkdir()
            (rules_dir / "rule-catalog.json").write_text(json.dumps(catalog))
            (guidance_dir / "subrules.json").write_text(
                json.dumps(
                    {
                        "subrules": [
                            {
                                "ruleId": "learned-subrule-foundation-config-reader-before-behavior-config-behavior",
                                "parentRuleId": "foundation-before-behavior",
                                "before": "foundation/config-reader",
                                "after": "behavior/config-behavior",
                                "support": 4,
                                "repoSupport": 2,
                                "repos": ["bazelbuild/bazel"],
                                "confidence": 0.7,
                                "promoted": False,
                            }
                        ]
                    }
                )
            )
            (guidance_dir / "subrule-candidates.json").write_text(
                json.dumps(
                    {
                        "subrules": [
                            {
                                "candidateId": "foundation-before-behavior__foundation-config-reader__before__behavior-config-behavior",
                                "ruleId": "learned-subrule-foundation-config-reader-before-behavior-config-behavior",
                                "parentRuleId": "foundation-before-behavior",
                                "before": "foundation/config-reader",
                                "after": "behavior/config-behavior",
                                "decision": "promoted",
                                "failureBucket": "",
                                "validationSummary": {
                                    "subruleProof": {
                                        "hintsRulesVsHints": {
                                            "pairedCount": 3,
                                            "averagePlannedStackLinkCorrectnessDelta": 0.12,
                                            "regressionCount": 0,
                                        }
                                    }
                                },
                            },
                            {
                                "candidateId": "learned-role-transition-behavior-feature-behavior-before-release",
                                "parentRuleId": "learned-role-transition",
                                "before": "behavior/feature-behavior",
                                "after": "release",
                                "support": 7,
                                "repoSupport": 2,
                                "confidence": 0.59,
                                "decision": "skipped",
                                "failureBucket": "unsupported_selector",
                            },
                        ]
                    }
                )
            )
            (reports_dir / "subrule-investigation.json").write_text(json.dumps({"status": "ok"}))

            response = app.rules_response(root)

        self.assertEqual(response["subrules"]["count"], 2)
        generated = next(catalog for catalog in response["catalogs"] if catalog["kind"] == "generated")
        self.assertEqual(generated["counts"]["rules"], 2)
        rule = next(rule for rule in generated["rules"] if rule["id"] == "foundation-before-behavior")
        self.assertEqual(rule["subrules"][0]["parentRuleId"], "foundation-before-behavior")
        self.assertEqual(rule["subrules"][0]["decision"], "promoted")
        self.assertEqual(rule["subrules"][0]["validation"]["stackLinkDelta"], 0.12)
        uncategorized = next(rule for rule in generated["rules"] if rule["id"] == "uncategorized-subrules")
        self.assertEqual(len(uncategorized["subrules"]), 1)
        self.assertEqual(uncategorized["subrules"][0]["sourceParentRuleId"], "learned-role-transition")
        self.assertEqual(uncategorized["subrules"][0]["before"], "behavior/feature-behavior")

    def test_static_rules_page_is_browse_first_model_view(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-rules.html").read_text()

        self.assertIn("<title>Model Rules</title>", html)
        self.assertIn("<h1>Model Rules</h1>", html)
        self.assertIn("id=\"compare-toggle\"", html)
        self.assertIn("id=\"debug-toggle\"", html)
        self.assertIn("id=\"graph-tab\"", html)
        self.assertIn("id=\"list-tab\"", html)
        self.assertIn("id=\"graph-view\"", html)
        self.assertIn("id=\"list-view\"", html)
        self.assertIn("role=\"tab\"", html)
        self.assertIn("Rule Graph", html)
        self.assertIn("function graphData", html)
        self.assertIn("function renderGraph", html)
        self.assertIn("function renderGraphSvg", html)
        self.assertIn("function renderGraphDetail", html)
        self.assertIn("function semanticRank", html)
        self.assertIn("function graphStageLabels", html)
        self.assertIn("function renderDirectionRuler", html)
        self.assertIn("function selectGraphNode", html)
        self.assertIn("function selectGraphEdge", html)
        self.assertIn("function addGraphItemHandlers", html)
        self.assertIn("function nodeLabelLines", html)
        self.assertIn("Flow direction is left to right", html)
        self.assertIn("direction-ruler", html)
        self.assertIn("graph-edge-hit", html)
        self.assertIn("data-node-id", html)
        self.assertIn("data-edge-id", html)
        self.assertIn("event.key !== \"Enter\"", html)
        self.assertIn("event.key !== \" \"", html)
        self.assertIn("Select a node or edge", html)
        self.assertIn("Categorized Rules", html)
        self.assertIn("Needs Categorization", html)
        self.assertIn("function renderSection", html)
        self.assertIn("function renderRule", html)
        self.assertIn("function renderSubrule", html)
        self.assertIn("Validation delta", html)
        self.assertIn("Test delta", html)
        self.assertIn("activate-one-surface-at-a-time", html)
        self.assertIn("uncategorized-subrules", html)
        self.assertIn("source parent", html)
        self.assertIn("function labelDescription", html)
        self.assertIn("function exampleLink", html)
        self.assertIn("fromCommitUrl", html)
        self.assertIn("target=\"_blank\"", html)
        self.assertIn("debug-only", html)
        self.assertNotIn("id=\"search\"", html)
        self.assertNotIn("catalog-filter", html)
        self.assertNotIn("comparison-grid", html)
        self.assertNotIn("Generated candidate</h3>", html)
        self.assertNotIn("Effective after gate</h3>", html)

    def test_static_rules_d3_poc_page_is_interactive_demo(self) -> None:
        html = (REPO_ROOT / "docs" / "rules-d3-poc.html").read_text()
        app_source = (REPO_ROOT / "scripts" / "splitter_metric_tree_app.py").read_text()

        self.assertIn("<title>D3 Rule Graph POC</title>", html)
        self.assertIn("https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js", html)
        self.assertIn("d3.forceSimulation", html)
        self.assertIn("d3.zoom", html)
        self.assertIn("d3.drag", html)
        self.assertIn("function selectNode", html)
        self.assertIn("function selectEdge", html)
        self.assertIn("Keep compatibility contract", html)
        self.assertIn("Prompt instruction", html)
        self.assertIn("rules-d3-poc.html", app_source)

    def test_splitter_metric_tree_launcher_uses_bigquery_venv(self) -> None:
        launcher = (REPO_ROOT / "scripts" / "run-splitter-metric-tree-app.sh").read_text()
        installer = (REPO_ROOT / "scripts" / "install-splitter-metric-tree-do1.sh").read_text()

        self.assertIn("bigquery.env", launcher)
        self.assertIn("bigquery-venv", launcher)
        self.assertIn("google-cloud-bigquery", launcher)
        self.assertIn("WORKFLOW_ANALYSIS_SERVICE_ROOT", launcher)
        self.assertIn("scripts/splitter_metric_tree_app.py", launcher)
        self.assertIn("pgrep -f \"[s]plitter_metric_tree_app.py\"", installer)
        self.assertIn("pkill -9 -f \"[s]plitter_metric_tree_app.py\"", installer)
        self.assertIn("/healthz", installer)


if __name__ == "__main__":
    unittest.main()
